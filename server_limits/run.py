#!/usr/bin/env python3
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

"""Measure server throughput as client count and outstanding requests increase."""

from __future__ import annotations

import os
import sys

if __package__ in (None, ""):  # allow `python server_limits/run.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import json
import statistics
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from common import dotnet_workers, node_workers, sdk_workers
from common.bench_db import BenchDB
from common.contract import DEFAULT_ENDPOINT, SERVER_READY
from common.histogram import BUCKETS as HISTOGRAM_BUCKETS
from common.histogram import merge as merge_histograms
from common.histogram import percentile_ms
from common.oom import CLIENT_OOM_SCORE_ADJ, SERVER_OOM_SCORE_ADJ, child_limits
from common.progress import Progress
from common.runner import (
    confirm,
    executable,
    one_of,
    partition_applicable,
    positive_fraction,
    whole_number,
)
from common.workers import (
    compose_preexec,
    drain,
    partition_cpus,
    pin_to_cpus,
    port_in_use,
    read_until,
    teardown_server,
    wait_for_server,
)

# Printed by the C hammer client once it has connected and is waiting on the
# stdin barrier; must match O6_LIMITS_READY_MARKER in open62541/common.h.
READY_MARKER = "O6_LIMITS_READY"

ROOT = Path(__file__).resolve().parents[1]
BENCH_DIR = Path(__file__).parent
# The Python servers used to live here, one per suite; they were all strict
# subsets of one program, so they were collapsed into a single shared
# program under common/servers/. The runner picks the same executable for
# every suite; the only thing that changes is --port.
SERVERS_DIR = ROOT / "common" / "servers"
O6_SERVER = SERVERS_DIR / "o6_server.py"
ASYNCUA_SERVER = SERVERS_DIR / "asyncua_server.py"

IMPLEMENTATIONS: tuple[str, ...] = ("open62541", "o6-python", "asyncua", "ua-dotnet", "node-opcua") + sdk_workers.NAMES
# Each cell retains at least seven samples after discarding its first new sample.
SAMPLES_DEFAULT = 7
MIN_SAMPLES = 7  # the validator's lower bound; do not edit past this


def applicable(config_key: dict) -> bool:
    """Whether this suite's matrix has a measurement for ``config_key``."""
    return True


def holds_c_binaries(value: object, fields: dict) -> bool | str:
    directory = Path(str(value)).resolve()
    missing = [
        str(path) for path in (executable(directory, "server"), executable(directory, "hammer_client")) if not path.is_file()
    ]
    return not missing or ("is missing the C benchmark executable(s) " + ", ".join(missing) + " — build them per the README")


def within_ladder(other: str, cmp: Callable[[int, int], bool], message: str) -> Callable[[object, dict], bool | str]:
    """A validator that compares one uniform field against another by name."""

    def validate(value: object, fields: dict) -> bool | str:
        bound = fields.get(other)
        if not isinstance(bound, int) or not isinstance(value, int):
            return True  # the other field's own validator reports the type problem
        return cmp(value, bound) or message.format(value=value, other=other, bound=bound)

    return validate


# The per-suite configuration fields this runner reads from the ``config``
# table at sample time and writes into it on ``new``; declared in
# :mod:`server_limits.options`.
SUITE_NAME = "server_limits"


def ladder(minimum: int, maximum: int) -> list[int]:
    """A doubling sequence from ``minimum`` to ``maximum``, ``maximum`` included.

    ``[1, 2, 4, 8, ..., maximum]`` — the escalation this benchmark runs along
    both the client-count and the pipeline-depth axis: each step roughly
    doubles that axis's contribution to the total load, so a run reaches a
    server's limit (or the ladder's cap) in a handful of steps rather than one
    per integer.
    """
    values: list[int] = []
    value = minimum
    while value < maximum:
        values.append(value)
        value *= 2
    values.append(maximum)
    return values


def start_client(process: subprocess.Popen[str], label: str) -> tuple[bool, list[str]]:
    """Read a hammer client's startup output.

    Returns ``(ready, lines)``. ``ready`` is true once the client has printed
    :data:`READY_MARKER` and is blocked on the stdin barrier; false if it
    instead printed its one-line JSON result immediately — the connection
    attempt itself failed, which is a measurement (the server already refused
    it), not a client crash. Raises :class:`RuntimeError` only if the process
    exits having printed neither, which is an actual client failure.
    """
    captured = read_until(
        process,
        READY_MARKER + " or a JSON result",
        label,
        matches=lambda line: READY_MARKER in line or line.lstrip().startswith("{"),
    )
    return READY_MARKER in captured[-1], captured


def parse_client_result(output: str, label: str) -> dict:
    """The one JSON line a hammer client prints, normalised for aggregation.

    A client that never connected reports ``attempted: 0`` — it is telling
    the truth, it made no calls — but for this benchmark a refused connection
    is itself the server giving up on a would-be caller, so it is folded in
    here as one attempted, failed (connection-level) call rather than
    vanishing from the level's totals.
    """
    for line in output.splitlines():
        if line.startswith("{"):
            result = json.loads(line)
            if result.get("connect_failed"):
                return {
                    "attempted": 1,
                    "succeeded": 0,
                    "timeouts": 0,
                    "connection_lost": 1,
                    "other_errors": 0,
                    "histogram_us": [0] * HISTOGRAM_BUCKETS,
                    "start_ns": 0,
                    "end_ns": 0,
                    "connection_broken": 0,
                    "connect_failed": 1,
                    "connect_status": result.get("connect_status"),
                }
            return result
    raise RuntimeError(f"No result from {label}:\n{output}")


def aggregate_step(results: list[dict], fallback_elapsed_ns: int) -> dict:
    """Combine one load level's per-client results into one record.

    ``fallback_elapsed_ns`` covers the (pathological, but not impossible)
    case where every client failed to connect: there is no real timed window
    to measure, so the configured step length stands in — the rates it
    produces are all zero regardless, since nothing succeeded.
    """
    attempted = sum(r["attempted"] for r in results)
    succeeded = sum(r["succeeded"] for r in results)
    timeouts = sum(r["timeouts"] for r in results)
    connection_lost = sum(r["connection_lost"] for r in results)
    other_errors = sum(r["other_errors"] for r in results)
    failed = timeouts + connection_lost + other_errors
    starts = [r["start_ns"] for r in results if r["start_ns"] > 0]
    ends = [r["end_ns"] for r in results if r["end_ns"] > 0]
    elapsed_ns = (max(ends) - min(starts)) if starts and ends else fallback_elapsed_ns
    elapsed_ns = max(elapsed_ns, 1)
    histogram = merge_histograms([r["histogram_us"] for r in results])
    elapsed_seconds = elapsed_ns / 1_000_000_000.0
    return {
        "attempted": attempted,
        "succeeded": succeeded,
        "timeouts": timeouts,
        "connection_lost": connection_lost,
        "other_errors": other_errors,
        "failed": failed,
        "error_rate": round(failed / attempted, 6) if attempted else 1.0,
        "elapsed_ns": elapsed_ns,
        "ops_per_second": round(succeeded / elapsed_seconds, 3),
        "attempted_per_second": round(attempted / elapsed_seconds, 3),
        "latency_ms": {
            "median": percentile_ms(histogram, 0.5),
            "p95": percentile_ms(histogram, 0.95),
            "p99": percentile_ms(histogram, 0.99),
        },
        "clients_connect_failed": sum(1 for r in results if r.get("connect_failed")),
        "connection_broken_count": sum(1 for r in results if r.get("connection_broken")),
    }


def run_step(
    client_path: Path,
    endpoint: str,
    clients: int,
    outstanding: int,
    step_seconds: int,
    warmup_seconds: int,
    grace_seconds: int,
    timeout_ms: int,
    client_preexec: Callable[[], None] | None = None,
) -> dict:
    """Fire one load level (``clients`` processes x ``outstanding`` pipeline depth) and aggregate it.

    ``client_preexec`` is the composed ``pin_to_cpus`` + ``child_limits``
    the calling ``run_one_implementation`` already built; ``run_step`` is
    a module-level function so it cannot reach a name defined inside the
    caller, hence the explicit parameter. ``None`` keeps the unconstrained
    behaviour (no pinning, no address-space cap) so a non-Linux runner or
    a unit test that does not need the controls keeps working.
    """
    processes: list[subprocess.Popen[str]] = []
    try:
        for index in range(clients):
            command = [
                str(client_path),
                "--endpoint",
                endpoint,
                "--duration-ms",
                str(step_seconds * 1000),
                "--warmup-ms",
                str(warmup_seconds * 1000),
                "--timeout-ms",
                str(timeout_ms),
                "--grace-ms",
                str(grace_seconds * 1000),
                "--outstanding",
                str(outstanding),
                "--seed",
                str(index + 1),
            ]
            processes.append(
                subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    preexec_fn=client_preexec,
                )
            )

        ready = []
        prefixes = []
        for index, process in enumerate(processes):
            state, captured = start_client(process, f"client {index}")
            ready.append(state)
            prefixes.append(captured)
        for process, state in zip(processes, ready):
            if state:
                assert process.stdin is not None
                process.stdin.write("\n")
                process.stdin.flush()

        timeout_seconds = step_seconds + warmup_seconds + grace_seconds + 30
        results = []
        for index, process in enumerate(processes):
            assert process.stdout is not None
            remainder, _ = process.communicate(timeout=timeout_seconds)
            return_code = process.returncode
            output = "".join(prefixes[index]) + remainder
            if return_code != 0:
                raise RuntimeError(f"client {index} failed with {return_code}:\n{output}")
            results.append(parse_client_result(output, f"client {index}"))
        # A client that declined to issue a call for a reason of its own — its
        # outstanding-request cap being the one that bites — measured nothing
        # about the server, so this level has no verdict to give. Raising here
        # records it as a failure of the harness instead, which is honest;
        # calling it "the server gave up" would pin a limit of the load
        # generator on every implementation at the identical load level.
        refused = sorted({str(result.get("send_refused")) for result in results if result.get("send_refused")})
        if refused:
            raise RuntimeError(
                f"the load generator refused to issue calls at {outstanding} outstanding "
                f"({', '.join(refused)}); this is a limit of the client, not of the server, so "
                "the level was not measured. Lower 'max_outstanding' or raise the client's cap."
            )
        return aggregate_step(results, fallback_elapsed_ns=step_seconds * 1_000_000_000)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait()


def evaluate_step(aggregate: dict, baseline_p99_ms: float | None, previous_ops: float | None, uniform: dict) -> list[str]:
    """Which of the configured criteria this load level tripped, if any.

    Every criterion that applies is reported rather than just the first, so a
    level that both errors out and collapses in throughput says so in one
    record instead of hiding one reason behind the other.
    """
    reasons: list[str] = []
    if aggregate["error_rate"] > uniform["error_rate_threshold"]:
        reasons.append("error_rate")
    p99 = aggregate["latency_ms"]["p99"]
    if baseline_p99_ms and p99 is not None and p99 > baseline_p99_ms * uniform["latency_blowup_factor"]:
        reasons.append("latency_blowup")
    if (
        previous_ops is not None
        and previous_ops > 0
        and aggregate["ops_per_second"] < previous_ops * uniform["throughput_collapse_ratio"]
    ):
        reasons.append("throughput_collapse")
    if aggregate["connection_broken_count"] > 0:
        reasons.append("connection_broken")
    return reasons


# How close to the peak a level has to come to count as "already at capacity".
# The knee is the cheapest load that reaches it, which is the useful capacity
# number: past there, concurrency buys latency rather than work.
KNEE_FRACTION = 0.95


def saturation_knee(levels: list[dict]) -> tuple[dict, dict] | None:
    """The peak-throughput level and the cheapest level that essentially matches it.

    Read off the levels that *held*, not all of them: a level past the edge can
    post a high rate while erroring, and capacity reached by failing is not
    capacity. Returns ``(peak, knee)``, or ``None`` when nothing held.
    """
    held = [level for level in levels if not level["failed"]]
    if not held:
        return None
    peak = max(held, key=lambda level: level["ops"])
    knee = min(
        (level for level in held if level["ops"] >= peak["ops"] * KNEE_FRACTION),
        key=lambda level: level["concurrency"],
    )
    return peak, knee


def compact(value: float) -> str:
    if value < 1_000:
        return f"{value:.0f}"
    if value < 1_000_000:
        return f"{value / 1e3:.1f}k"
    return f"{value / 1e6:.2f}M"


def server_command(implementation: str, c_server: Path) -> list[str]:
    if implementation == "open62541":
        return [str(c_server)]
    if implementation == "o6-python":
        return [sys.executable, str(O6_SERVER)]
    if implementation == "asyncua":
        return [sys.executable, str(ASYNCUA_SERVER)]
    if implementation == "node-opcua":
        return node_workers.command("server", "server_limits")
    if implementation == "ua-dotnet":
        return dotnet_workers.command("server", "server_limits")
    if implementation in sdk_workers.SDKS:
        return sdk_workers.command(implementation)
    raise ValueError(f"unknown server implementation: {implementation!r}")


def run_one_implementation(
    implementation: str,
    uniform: dict,
    store: BenchDB,
    c_server_path: Path,
    c_client_path: Path,
    progress: Progress,
    prior_runs: dict | None = None,
) -> int:
    """Trace one server implementation's capacity frontier.

    Within one client-process count, pipeline depth escalates from the lightest
    until a level trips one of the failure criteria (or the ladder's cap is
    reached), which is recorded as that count's edge. Every client-process count
    is walked the same way from the same starting depth, so none of them
    inherits a starting point — or a mistake — from another. The ramp stops once
    even the lightest depth (min_outstanding) fails, since more clients would
    only fail faster.

    ``prior_runs`` carries the per-cell kept runs a previous run already
    measured for this implementation. With ``--amend`` it is the run's
    source of truth: the ramp visits exactly the ``(clients, outstanding)``
    cells that already have rows, and tops each one up to the configured
    ``samples`` count using the stored runs as the kept set. Without
    ``--amend`` (or when ``prior_runs`` is ``None`` / empty) the function
    falls back to the adaptive walk, which is what it always did before
    ``--amend`` was added.

    Returns how many load levels were actually measured, which the caller
    needs to keep the progress bar honest: the total is sized for the worst
    case and this is what it really cost.
    """
    samples = int(uniform["samples"])
    clients_ladder = ladder(uniform["min_clients"], uniform["max_clients"])
    outstanding_ladder = ladder(uniform["min_outstanding"], uniform["max_outstanding"])

    # CPU-set pinning: server on one half of the machine's
    # logical CPUs, clients on the other, so neither party ever preempts
    # the other. ``pin_to_cpus`` returns ``None`` on macOS/Windows and
    # the worker falls through to the unpinned ``child_limits`` path.
    server_cpus, client_cpus = partition_cpus(os.cpu_count() or 1)
    server_preexec = compose_preexec(
        pin_to_cpus(server_cpus),
        child_limits(0, SERVER_OOM_SCORE_ADJ),
    )
    client_preexec = compose_preexec(
        pin_to_cpus(client_cpus),
        child_limits(0, CLIENT_OOM_SCORE_ADJ),
    )

    pki_dir = tempfile.TemporaryDirectory(prefix="o6-limits-pki-") if implementation in {"ua-dotnet", "node-opcua"} else None
    server_command_line = server_command(implementation, c_server_path)
    server = subprocess.Popen(
        server_command_line,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=(os.name != "nt"),
        preexec_fn=server_preexec,
        env=(
            {
                **(node_workers.environment() if implementation == "node-opcua" else dotnet_workers.environment()),
                "O6_BENCHMARK_PKI_ROOT": pki_dir.name,
            }
            if pki_dir
            else sdk_workers.environment(implementation)
            if implementation in sdk_workers.SDKS
            else None
        ),
    )
    try:
        startup = read_until(server, SERVER_READY, server_command_line[0])
        if implementation == "node-opcua":
            for line in startup:
                if line.startswith('{"node_opcua_limits":'):
                    store.metadata["node_opcua_diagnostics"] = {"effective_limits": json.loads(line)["node_opcua_limits"]}
        assert server.stdout is not None
        threading.Thread(target=drain, args=(server.stdout,), daemon=True).start()
        wait_for_server(server)
    except (OSError, RuntimeError) as error:
        store.note_failure(
            {
                "configuration": f"{implementation} (server)",
                "config_key": {"implementation": implementation},
                "error": f"server did not start: {error}",
            }
        )
        teardown_server(server)
        if pki_dir:
            pki_dir.cleanup()
        return 0

    # Whether this run is driven by the stored level set (``--amend`` with
    # prior rows for this implementation) or by the adaptive walk. The
    # stored-driven path visits exactly the levels the previous run visited,
    # so a re-run that finds a different edge does not silently change the
    # level set a topped-up cell is compared against. ``--amend`` without
    # prior rows for this implementation falls through to the adaptive
    # walk so a freshly added implementation still gets measured.
    amend = prior_runs is not None and bool(prior_runs)
    if amend:
        # Preserve the order in which the previous run visited the cells,
        # row by row, so the ramp replays in the same shape rather than
        # re-sorting by ``(clients, outstanding)`` and producing a different
        # order. ``prior_runs`` is keyed by the same tuple the matrix walk
        # builds, so iterating ``prior_runs.items()`` matches what the
        # previous ``run_one_implementation`` did.
        cells: list[tuple[int, int]] = list(prior_runs.keys())
    else:
        # Adaptive walk over the doubling ladder; a cell that fails ends
        # this client count's escalation, the same as before ``--amend``.
        cells = [(clients, outstanding) for clients in clients_ladder for outstanding in outstanding_ladder]

    baseline_p99_ms: float | None = None
    died = False
    measured = 0
    # Throughput of the last level that held at the current client count, which
    # the throughput-collapse rule is judged against. It only means anything
    # against a *shallower* level at the same client count — comparing a level
    # against a deeper one would read the load easing off as the server
    # collapsing — and both hold here: the walk below only ever deepens within a
    # client count, and resets this at every new one. Under ``--amend`` the
    # stored-driven walk visits cells in stored order; the rule below still
    # resets at each new client count, so a topped-up row that previously held
    # remains a valid baseline for the next deeper cell at the same count.
    previous_ops: float | None = None

    def measure(clients: int, outstanding: int) -> bool | None:
        """Measure one load level. True if it beat the server, None if unmeasurable."""
        nonlocal baseline_p99_ms, died, measured, previous_ops
        if server.poll() is not None:
            store.note_failure(
                {
                    "configuration": f"{implementation} {clients}c x {outstanding}o",
                    "config_key": {"implementation": implementation, "clients": clients, "outstanding": outstanding},
                    "error": f"server exited ({server.returncode}) before this load level was measured",
                }
            )
            died = True
            return None

        config_key = {"implementation": implementation, "clients": clients, "outstanding": outstanding}
        label = f"{implementation} {clients}c x {outstanding}o"
        is_baseline = clients == clients_ladder[0] and outstanding == outstanding_ladder[0]
        existing = list(prior_runs.get((clients, outstanding), [])) if amend else []
        # ``needed_kept`` is how many kept entries this run has to add to
        # reach ``samples``. ``new_passes`` is that plus one to discard,
        # matching the first-sample-discard rule; the
        # discard only fires when this run actually adds passes, so a
        # topped-up cell is never shrunk below ``samples`` and a cell that
        # is already at the target is not re-measured.
        needed_kept = max(0, samples - len(existing))
        new_passes = needed_kept + (1 if needed_kept > 0 else 0)
        progress.begin(label)
        new_runs: list[dict] = []
        try:
            for offset in range(new_passes):
                progress.state(f"repeat {len(existing) + offset + 1}/{len(existing) + new_passes}")
                new_runs.append(
                    run_step(
                        c_client_path,
                        DEFAULT_ENDPOINT,
                        clients,
                        outstanding,
                        uniform["step_seconds"],
                        uniform["warmup_seconds"],
                        uniform["grace_seconds"],
                        uniform["request_timeout_ms"],
                        client_preexec,
                    )
                )
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            # A hammer client that crashed outright or hung past its own
            # generous timeout is not one of the measured failure criteria — it
            # is the harness losing track of what happened, which is reason
            # enough to stop trusting this implementation's ramp rather than
            # guess at a verdict. Keep any partial new runs we managed to
            # collect (folded with ``existing`` under the same discard rule
            # below) so the row still records what was observed before the
            # harness lost the thread — better than leaving the row's
            # ``runs`` empty when partial data exists.
            if new_passes > 0:
                kept = list(existing) + new_runs[1:]
            else:
                kept = list(existing) + new_runs
            self_contained_failure = str(error)
            with store.transaction():
                store.write_result(
                    config_key,
                    {
                        "config_key": config_key,
                        "runs": kept,
                        "stats": {
                            "samples_requested": samples,
                            "samples_kept": len(kept),
                            "first_sample_discarded": bool(new_passes > 0 and len(kept) > 0),
                        },
                        "verdict": {"failed": True, "reasons": ["harness_failure"]},
                        "harness_failure": self_contained_failure,
                    },
                )
                store.note_failure({"configuration": label, "config_key": config_key, "error": str(error)})
            progress.advance(left=f"{label} FAILED", right="see failures")
            measured += 1
            died = True
            return None

        # First-sample discard: when this run added new
        # passes, drop the first of those before computing the verdict.
        # Stored kept runs from a previous run are not re-discarded, so a
        # topped-up cell ends up with exactly ``samples`` kept entries and
        # is never shrunk below its previous count.
        if new_passes > 0:
            runs = list(existing) + new_runs[1:]
        else:
            runs = list(existing) + new_runs

        median_ops = statistics.median(run["ops_per_second"] for run in runs)
        median_error_rate = statistics.median(run["error_rate"] for run in runs)
        p99_values = [run["latency_ms"]["p99"] for run in runs if run["latency_ms"]["p99"] is not None]
        median_p99 = statistics.median(p99_values) if p99_values else None
        summary = {
            "ops_per_second": median_ops,
            "error_rate": median_error_rate,
            "latency_ms": {"p99": median_p99},
            "connection_broken_count": sum(run["connection_broken_count"] for run in runs),
        }
        reasons = evaluate_step(summary, baseline_p99_ms, previous_ops, uniform)
        if server.poll() is not None:
            # The server did not survive this load level. Distinct from the
            # criteria above: those infer distress from what the client saw,
            # this is the server observably gone, and it ends the ramp outright
            # rather than just this row.
            reasons.append("process_death")
            died = True
        failed = bool(reasons)

        if is_baseline:
            baseline_p99_ms = median_p99
        if not failed:
            previous_ops = median_ops

        store.write_result(
            config_key,
            {
                "config_key": config_key,
                "runs": runs,
                "stats": {
                    "samples_requested": samples,
                    "samples_kept": len(runs),
                    "first_sample_discarded": bool(new_passes > 0),
                    "median_ops_per_second": median_ops,
                    "median_error_rate": median_error_rate,
                    "median_p99_latency_ms": median_p99,
                },
                "verdict": {"failed": failed, "reasons": reasons},
            },
        )
        progress.advance(
            left=label + (" GAVE UP" if failed else ""),
            right=f"{compact(median_ops):>7} ok/s  err {median_error_rate * 100:5.1f}%"
            + (f"  p99 {median_p99:.0f}ms" if median_p99 is not None else ""),
        )
        measured += 1
        if not failed:
            time.sleep(uniform["settle_seconds"])
        return failed

    try:
        # Every client count is walked from the lightest depth to its own edge,
        # and nothing is carried from one to the next. That costs rows x columns
        # measurements where tracing only the boundary would cost about
        # rows + columns, and the difference buys the one thing a search that
        # shares state cannot offer: each client count's edge is a level this
        # run measured at that client count, so no row's verdict depends on
        # another row's having been right. A level that fails from noise rather
        # than from load costs exactly the row it happened in.
        #
        # ``cells`` carries the (clients, outstanding) pairs to visit in the
        # order the ramp should walk them. Under ``--amend`` it is the
        # stored set in the order the previous run visited them; otherwise it
        # is the full cartesian of the doubling ladders so a fresh run walks
        # every level and breaks on the first failure.
        groups: dict[int, list[int]] = {}
        for clients, outstanding in cells:
            groups.setdefault(clients, []).append(outstanding)
        for clients in sorted(groups):
            previous_ops = None  # comparisons never cross a client count
            column = 0
            outstanding_for_count = groups[clients]
            while column < len(outstanding_for_count):
                failed = measure(clients, outstanding_for_count[column])
                if failed is None:
                    break
                # Adaptive walk ends a client count's escalation at the first
                # failing level; the stored-driven ``--amend`` walk visits
                # every stored level so the topped-up verdict has a chance
                # to move.
                if failed and not amend:
                    break
                column += 1
            if died:
                break
            if column == 0 and not amend:
                break  # even the lightest depth failed: more clients cannot help
    finally:
        teardown_server(server)
        if pki_dir:
            pki_dir.cleanup()
    return measured


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description=(
            "Escalate concurrent load (client processes x async pipeline depth) against "
            "each OPC UA server implementation, fired by a fixed open62541 C client, until "
            "it errors, its tail latency blows up, its throughput collapses, or it dies."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    new = subparsers.add_parser("new", help="write a default bench document to <file> and stop")
    new.add_argument("filename", type=Path)

    sample = subparsers.add_parser(
        "sample",
        help="trace every implementation's capacity frontier and write it into <file>",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog=(
            "<num_samples> is the *kept* samples per cell — the runner fires one extra "
            "pass and discards the first, so a run "
            "that stores N samples actually invokes the worker N+1 times per cell. Without "
            "--amend the run remeasures every cell from scratch; with --amend existing "
            "rows are kept and topped up to <num_samples>."
        ),
    )
    sample.add_argument(
        "num_samples",
        type=int,
        help="kept samples wanted per cell; existing rows are extended to this many with --amend",
    )
    sample.add_argument("filename", type=Path)
    sample.add_argument(
        "--amend",
        action="store_true",
        help=(
            "keep existing rows and top them up to <num_samples> instead of remeasuring "
            "every cell from scratch; the ramp for each implementation visits exactly the "
            "(clients, outstanding) cells that already have rows in the document"
        ),
    )
    sample.add_argument(
        "--skip-failed",
        action="store_true",
        help=(
            "leave the configurations recorded under 'failures' alone instead of "
            "retrying them; useful once a failure is known to be a limit of the "
            "implementation rather than a flake (needs --amend)"
        ),
    )

    args = parser.parse_args(argv)
    if args.command == "sample":
        if args.num_samples < MIN_SAMPLES:
            parser.error(
                f"num_samples must be at least {MIN_SAMPLES} — the lower bound is what "
                "the bootstrap-style signal needs to land on"
            )
        if args.skip_failed and not args.amend:
            parser.error("--skip-failed only makes sense with --amend")
    return args


def cmd_new(args: argparse.Namespace) -> int:
    """Seed the per-suite config rows in ``args.database`` from OPTIONS.

    Asks before overwriting a ``config`` table whose rows already match
    every declared option — narrowing the matrix by hand is real work to
    redo. The result tables are untouched either way; when the database
    already exists, the prompt names what is recorded in it so seeding
    over a populated store does not read as losing those measurements.
    """
    from server_limits.options import OPTIONS

    database = Path(args.database)
    db = BenchDB(database, suite=SUITE_NAME)

    existing_rows = {row[0] for row in db._connection.execute("SELECT option FROM config WHERE suite = ?", (SUITE_NAME,))}
    expected = set(OPTIONS)
    if expected and existing_rows and existing_rows >= expected:
        result_rows = db.results
        held = f"{database} holds {len(result_rows)} measured cell(s)." if result_rows else f"{database} holds no results yet."
        print(
            f"{database} already has {len(existing_rows)} configured option(s) for suite {SUITE_NAME!r}. {held}",
            file=sys.stderr,
        )
        if not confirm("Overwrite them with the default values?"):
            print(
                "Left unchanged. Use 'python -m bench.server_limits config <db> <name> <value>' " "to edit individual options.",
                file=sys.stderr,
            )
            return 1

    seeded = 0
    for name, option in OPTIONS.items():
        db.set_config(SUITE_NAME, name, json.dumps(option.default))
        seeded += 1
    print(f"Wrote {seeded} option(s) to {database}", file=sys.stderr)
    return 0


def _clear_failures_for(store: BenchDB, implementations: set[str]) -> None:
    """Drop every stored failure whose ``config_key.implementation`` is in ``implementations``.

    Shared by ``cmd_sample``'s ``--amend --skip-failed`` and plain (non-amend)
    paths: both start this run by discarding stale failures for whatever
    implementations it is about to (re)measure, so a run that now avoids a
    cell entirely does not leave a failure record behind that this run never
    touched.
    """
    with store.transaction():
        for entry in list(store.failures):
            key = entry.get("config_key") or {}
            implementation = key.get("implementation") if isinstance(key, dict) else None
            if implementation not in implementations:
                continue
            label = entry.get("configuration")
            if label is not None:
                store.clear_failure(label)


def cmd_sample(args: argparse.Namespace) -> int:
    store = BenchDB(args.database, suite=SUITE_NAME)
    uniform, varying = store.get_config(SUITE_NAME)
    if not uniform and not varying:
        print(
            f"{args.database} has no configured options for suite {SUITE_NAME!r}. "
            f"Run 'python -m bench.server_limits new {args.database}' first.",
            file=sys.stderr,
        )
        return 1

    if port_in_use():
        print(
            "Something is already listening on 127.0.0.1:4840, where this benchmark's "
            "server goes. Stop it first, or every implementation here would be measured "
            "against it rather than against the one it names.",
            file=sys.stderr,
        )
        return 1

    implementations = varying.get("implementation") or list(IMPLEMENTATIONS[:3])
    try:
        node_workers.prepare_metadata(
            store, "node-opcua" in implementations, {"server"}, "server_limits", bool(getattr(args, "amend", False))
        )
        dotnet_workers.prepare_metadata(
            store, "ua-dotnet" in implementations, {"server"}, "server_limits", bool(getattr(args, "amend", False))
        )
        sdk_workers.prepare_metadata(
            store, set(implementations) & set(sdk_workers.SDKS), "server_limits", bool(getattr(args, "amend", False))
        )
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 1
    differences = store.metadata_differences()
    if getattr(args, "amend", False) and store.results and differences:
        print(f"--amend would mix incomparable environments: {differences}", file=sys.stderr)
        return 1

    # The CLI's num_samples wins over whatever was stored — a run is sized
    # by the caller, not by the stale default left over from the last
    # ``new``. Written to the config table now, ahead of the walk, so the
    # target survives a run interrupted before its first cell lands.
    num_samples = int(args.num_samples)
    store.set_config(SUITE_NAME, "samples", json.dumps(num_samples))
    uniform["samples"] = num_samples
    amend = bool(getattr(args, "amend", False))
    skip_failed = bool(getattr(args, "skip_failed", False))

    store.metadata["partial"] = True

    c_binary_dir = Path(uniform["c_binary_dir"]).resolve()
    c_server_path = executable(c_binary_dir, "server")
    c_client_path = executable(c_binary_dir, "hammer_client")

    implementations = varying.get("implementation") or list(IMPLEMENTATIONS[:3])

    # ``--amend`` reads the previously-stored rows per implementation and
    # feeds them to ``run_one_implementation`` as ``prior_runs``; without
    # ``--amend`` every implementation's frontier is remeasured from
    # scratch — the run replaces any stored rows for the listed
    # implementations, the same as before ``--amend`` was added. Only the
    # implementations this run covers are dropped; a result for one that is
    # no longer in the matrix is the store's record of it and is left
    # alone.
    remeasured = set(implementations)
    dropped = [
        key
        for key, row in store.results.items()
        if isinstance(row.get("config_key"), dict) and row["config_key"].get("implementation") in remeasured
    ]
    prior_runs_by_implementation: dict[str, dict[tuple[int, int], list[dict]]] = {}
    if amend:
        for key, row in store.results.items():
            cfg_key = row.get("config_key")
            if not isinstance(cfg_key, dict):
                continue
            implementation = cfg_key.get("implementation")
            if implementation not in remeasured:
                continue
            clients = int(cfg_key.get("clients") or 0)
            outstanding = int(cfg_key.get("outstanding") or 0)
            if clients <= 0 or outstanding <= 0:
                continue
            runs = row.get("runs")
            if not isinstance(runs, list):
                continue
            prior_runs_by_implementation.setdefault(implementation, {})[(clients, outstanding)] = [
                run for run in runs if isinstance(run, dict)
            ]
        if skip_failed:
            _clear_failures_for(store, remeasured)
    else:
        with store.transaction():
            for key, row in list(store.results.items()):
                cfg_key = row.get("config_key")
                if isinstance(cfg_key, dict) and cfg_key.get("implementation") in remeasured:
                    store.drop_result(cfg_key)
            _clear_failures_for(store, remeasured)
    if dropped:
        if amend:
            print(
                f":: --amend: keeping {len(dropped)} stored load level(s) for "
                f"{', '.join(sorted(remeasured))} and topping each up to {num_samples} kept sample(s) "
                f"(first new sample of each cell is discarded)",
                file=sys.stderr,
            )
        else:
            print(
                f":: discarding {len(dropped)} stored load level(s) for "
                f"{', '.join(sorted(remeasured))}; this run measures them again from scratch "
                "(the copy taken above still has them)",
                file=sys.stderr,
            )

    # How many load levels a run visits is adaptive — it depends on where each
    # implementation's edge turns out to be — so the bar is sized for the worst
    # case, which here is exact rather than a bound: every client count climbs
    # the whole depth ladder when no level in it beats the server. Sizing it for
    # the *typical* cost instead is what made the counter pin at 100% with
    # levels still to run. Each implementation tops the bar up by whatever of
    # its share it did not need, so a run that finds its edges early still ends
    # at exactly 100%. Under ``--amend`` the stored set may be smaller, but
    # sizing for the worst case is still the right call: a topped-up run that
    # finds an edge early needs to over-allocate the bar the same way.
    rows = len(ladder(uniform["min_clients"], uniform["max_clients"]))
    columns = len(ladder(uniform["min_outstanding"], uniform["max_outstanding"]))
    levels_per_implementation = rows * columns

    # The matrix walk is built up front and filtered through :func:`applicable`
    # once, even though this suite's predicate always returns ``True``: the
    # seam is the same one a future suite uses to declare a cell inapplicable,
    # and consulting it here is what makes the ``skipped`` list in the
    # document a real artefact of the run rather than a slot only filled by
    # other suites. The result is always ``(all_cells, [])`` today; the
    # bookkeeping below is the only place that assumption is read.
    matrix = [
        {"implementation": implementation, "clients": clients, "outstanding": outstanding}
        for implementation in implementations
        for clients in ladder(uniform["min_clients"], uniform["max_clients"])
        for outstanding in ladder(uniform["min_outstanding"], uniform["max_outstanding"])
    ]
    _, skipped_keys = partition_applicable(matrix, applicable)
    skipped_count = len(skipped_keys)
    for config_key in skipped_keys:
        label = f"{config_key['implementation']} {config_key['clients']}c x {config_key['outstanding']}o"
        store.note_skipped(
            {
                "configuration": label,
                "config_key": config_key,
                "reason": "applicable() returned False",
            }
        )

    # 'group' counts retired lines, which here is one per load level plus one
    # divider per implementation — so it is neither implementations (three
    # where thirty would be reported) nor load levels (the dividers are not
    # measurements). 'entry' is what it actually is.
    progress = Progress(
        len(implementations) * levels_per_implementation,
        unit="load level",
        group="entry",
        right_width=40,
    )
    print(
        f":: median successful calls/second, error rate, and p99 latency per load level "
        f"({num_samples} kept samples per cell, first new sample discarded)",
        file=sys.stderr,
    )
    if skipped_count:
        # Reported here rather than only at the end, so the user sees the
        # matrix shrink before the progress bar takes over the terminal and a
        # later "0 failure(s)" line could be misread as a clean sweep.
        print(
            f":: applicable() filtered out {skipped_count} cell(s); they are recorded under "
            f"'skipped' in {args.database}, not under 'results' or 'failures'",
            file=sys.stderr,
        )

    for implementation in implementations:
        prior = prior_runs_by_implementation.get(implementation) if amend else None
        measured = run_one_implementation(
            implementation, uniform, store, c_server_path, c_client_path, progress, prior_runs=prior
        )
        progress.advance(left=f"{implementation} done", steps=max(0, levels_per_implementation - measured))

    store.metadata["partial"] = False
    store.save_metadata(progress=True)
    # Closed before anything else is written, so the last line of the live area
    # is not what the summary below prints over.
    progress.finish()
    print(f"Wrote {len(store.results)} load-level result(s) to {args.database}", file=sys.stderr)

    print(file=sys.stderr)
    print(":: frontier summary", file=sys.stderr)
    for implementation in implementations:
        rows = [
            entry for entry in store.results.values() if entry.get("config_key", {}).get("implementation") == implementation
        ]
        if not rows:
            print(f"  {implementation}: not measured", file=sys.stderr)
            continue
        levels = [
            {
                "clients": row["config_key"]["clients"],
                "outstanding": row["config_key"]["outstanding"],
                "concurrency": row["config_key"]["clients"] * row["config_key"]["outstanding"],
                "ops": float((row.get("stats") or {}).get("median_ops_per_second") or 0.0),
                "failed": bool((row.get("verdict") or {}).get("failed")),
                "reasons": (row.get("verdict") or {}).get("reasons") or [],
            }
            for row in rows
        ]
        held = [level for level in levels if not level["failed"]]
        beaten = [level for level in levels if level["failed"]]
        best = max(held, key=lambda level: level["concurrency"], default=None)
        edge = min(beaten, key=lambda level: level["concurrency"], default=None)
        best_text = f"sustained {best['clients']}c x {best['outstanding']}o" if best else "sustained nothing"
        edge_text = (
            f"; gave up at {edge['clients']}c x {edge['outstanding']}o ({', '.join(edge['reasons'])})"
            if edge
            else "; ramp cap reached without finding a limit"
        )
        print(f"  {implementation}: {best_text}{edge_text}", file=sys.stderr)
        # The capacity figure, which is a different question from the edge:
        # where the work stops growing, rather than where the server stops
        # coping. For a server that saturates early the two are far apart, and
        # the knee is the one worth quoting.
        knee = saturation_knee(levels)
        if knee:
            peak, elbow = knee
            print(
                f"      peak {compact(peak['ops'])} ops/s at {peak['clients']}c x {peak['outstanding']}o; "
                f"reaches {int(KNEE_FRACTION * 100)}% of it by {elbow['clients']}c x {elbow['outstanding']}o "
                f"({elbow['concurrency']} concurrent) — past there, load buys latency rather than work",
                file=sys.stderr,
            )

    if store.failures:
        print(f"{len(store.failures)} failure(s) recorded; see 'failures' in {args.database}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_arguments(argv)
    if args.command == "new":
        return cmd_new(args)
    if args.command == "sample":
        return cmd_sample(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
