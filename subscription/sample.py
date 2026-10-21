# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
#    Copyright 2026 (c) o6 Automation GmbH (Author: Daniel Opitz)

"""Capacity searches and named-run persistence; each candidate is measured once."""

import asyncio
from contextlib import closing
import json

from common.bench_db import BenchDB, benchmark_scope
from common import open62541
from common.progress import Progress
from subscription.options import OPTIONS, measurement_window
from subscription.amend import amended_plan, saved_cases
from subscription.run import fingerprint, measure, SetupTimeout, SETUP_TIMEOUT_SECONDS
from subscription.search import MIN_PROGRESS
from subscription import strategy

SUITE = "subscription"


def cmd_new(database):
    database.parent.mkdir(parents=True, exist_ok=True)
    # Seed/migrate only future-run settings, never saved named configurations.
    with benchmark_scope(None), closing(BenchDB(database, SUITE)) as store:
        stored = dict(store._connection.execute("SELECT option, value FROM config WHERE suite = ?", (SUITE,)))
        values = {key: json.loads(value) for key, value in stored.items()}
        legacy = any(key in values for key in ("duration", "duration_ms", "items"))
        with store.transaction(protect=legacy):
            if isinstance(values.get("measurement"), str):
                values["measurement"] = [values["measurement"]]
                store.set_config(SUITE, "measurement", json.dumps(values["measurement"]))
            old_items = values.get("items")
            if isinstance(old_items, list) and old_items:
                for name, value in (("items_start", min(old_items)), ("items_max", max(old_items))):
                    if name not in values:
                        values[name] = value
                        store.set_config(SUITE, name, json.dumps(value))
            for name, option in OPTIONS.items():
                if name not in values:
                    store.set_config(SUITE, name, json.dumps(option.default))
            for name in ("duration", "duration_ms", "items"):
                if name in values:
                    store.unset_config(SUITE, name)
            schedule = store.get_schedule_config(SUITE)
            if schedule is None or legacy:
                store.set_schedule_config(
                    SUITE, **{**(schedule or dict(enabled=False, amend=False, skip_failed=False)), "samples": 1}
                )
    return 0


def cmd_sample(database, num_samples=1, amend=False, skip_failed=False):
    if type(num_samples) is not int or num_samples != 1:
        raise ValueError("The counter suite measures each candidate once; samples must be 1")
    with closing(BenchDB(database, SUITE)) as store:
        uniform, varying = store.get_config(SUITE)
        identity = fingerprint()
        if store.results and not amend:
            raise ValueError("This run already has results; use a new name or --amend")
        if amend and store.stored_metadata.get("counter_fingerprint", identity) != identity:
            raise ValueError("Counter sources or binaries changed; start a new run")
        if amend:
            uniform, searches = amended_plan(database, store)
        else:
            searches = saved_cases(store)
        open62541.prepare_metadata(store, SUITE, amend)
        saved = {json.dumps(row["key"], sort_keys=True): row["observation"] for row in store.results.values()}
        failures = store.failures
        failed = {row["configuration"] for row in failures}
        store.metadata.update(counter_fingerprint=identity, complete=False, capacities=[], requested_cases=searches)
        with store.transaction():
            if uniform["items_max"] != store.get_config(SUITE, "items_max"):
                store.set_config(SUITE, "items_max", json.dumps(uniform["items_max"]))
            for axis in varying:
                values = list(dict.fromkeys(case[axis] for case in searches))
                if values != varying[axis]:
                    store.set_config(SUITE, axis, json.dumps(values))
            store.save_metadata(progress=True)
        complete = True
        progress = Progress(len(searches), unit="configuration", group="result", right_width=32)
        try:
            for case in searches:
                begin, end = measurement_window(case["sampling_ms"], case["publishing_ms"], case["min_publishes"])
                timing = dict(warmup_ms=begin, measured_duration_ms=end - begin)
                search_label = (
                    f"{case['implementation']}/{case['measurement']} " f"s{case['sampling_ms']} p{case['publishing_ms']}ms"
                )

                measured = False

                def probe(items):
                    nonlocal measured
                    candidate = dict(case, items=items)
                    key = json.dumps(candidate, sort_keys=True)
                    label = f"{search_label} {items} items"
                    progress.begin(label)
                    cached = key in saved
                    if cached:
                        observation = saved[key]
                    else:
                        if skip_failed and key in failed:
                            raise RuntimeError("Search remains incomplete because a failed candidate was skipped")
                        measured = True
                        try:
                            observation = asyncio.run(measure(candidate, report=progress.state))
                        except SetupTimeout as error:
                            store.note_failure(
                                dict(
                                    config_key=candidate,
                                    configuration=key,
                                    message=str(error),
                                    kind="setup_timeout",
                                    phase=error.phase,
                                    timeout_seconds=SETUP_TIMEOUT_SECONDS,
                                )
                            )
                            raise
                        except (OSError, ValueError, RuntimeError, TimeoutError) as error:
                            store.note_failure(dict(config_key=candidate, configuration=key, message=str(error)))
                            raise
                        if observation["fingerprint"] != identity:
                            raise RuntimeError("Counter sources or binaries changed during the search; start a new run")
                        store.write_result(candidate, dict(key=candidate, observation=observation))
                        saved[key] = observation
                    summary = observation["summary"]
                    passed = summary["progress_fraction_min"] >= MIN_PROGRESS
                    known[items] = summary["progress_fraction_min"]
                    verdict = "pass" if passed else "below target"
                    progress.advance(
                        label,
                        f"{'saved ' if cached else ''}{verdict} {summary['progress_fraction_min']:.1%} "
                        f"({summary['observed_min']}/{summary['expected_per_item']:g})",
                        steps=0,
                    )
                    return known[items]

                try:
                    known = {}
                    for encoded, observation in saved.items():
                        candidate = json.loads(encoded)
                        items = candidate.pop("items")
                        if candidate == case:
                            known[items] = observation["summary"]["progress_fraction_min"]
                    for failure in failures:
                        if failure.get("kind") != "setup_timeout":
                            continue
                        candidate = json.loads(failure["configuration"])
                        items = candidate.pop("items")
                        if candidate == case:
                            # Setup timeouts are terminal for this configuration, also on amend.
                            raise SetupTimeout(items, failure["phase"])
                    result = strategy.search(uniform, probe, known)
                    store.metadata["capacities"].append(dict(case=case, **timing, **result))
                    bound = f"{'at least ' if result['at_least'] else ''}{result['passing_items']} passing items"
                    progress.advance(search_label, ("" if measured else "saved ") + bound)
                except SetupTimeout as error:
                    complete = False
                    lower = max((items for items, fraction in known.items() if fraction >= MIN_PROGRESS), default=0)
                    upper = min(
                        (items for items, fraction in known.items() if fraction < MIN_PROGRESS and items > lower), default=None
                    )
                    note = (
                        f"Setup timed out at {error.items} items during {error.phase} "
                        f"({SETUP_TIMEOUT_SECONDS}s limit); "
                        + (f"last passing workload: {lower} items." if lower else "no passing workload was measured.")
                        + " Measurement capacity was not established."
                    )
                    store.metadata["capacities"].append(
                        dict(
                            case=case,
                            **timing,
                            status="setup_limited",
                            passing_items=lower,
                            failing_items=upper,
                            at_least=False,
                            tested_items=list(known),
                            minimum_progress=MIN_PROGRESS,
                            setup_timeout_items=error.items,
                            setup_phase=error.phase,
                            setup_timeout_seconds=SETUP_TIMEOUT_SECONDS,
                            note=note,
                        )
                    )
                    progress.advance(f"{search_label} SETUP FAILED", note)
                except (OSError, ValueError, RuntimeError, TimeoutError) as error:
                    complete = False
                    store.metadata["capacities"].append(dict(case=case, **timing, error=str(error)))
                    progress.advance(f"{search_label} FAILED", str(error))
                store.save_metadata(progress=True)
            store.metadata["complete"] = complete and not store.failures
            store.save_metadata(progress=True)
            return 0 if store.metadata["complete"] else 1
        finally:
            progress.finish()
