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

"""Bounded adaptive capacity searches with synchronized native measurements."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, contextmanager
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time

from common import dotnet_workers, node_workers, sdk_workers
from common.bench_db import BenchDB
from common.contract import SERVER_READY
from common.histogram import BUCKETS, merge, percentile_ms
from common.progress import Progress
from common.workers import drain, port_in_use, read_until, teardown_server, wait_for_server
from server_limits.run import server_command
from server_capacity.options import OPTIONS
from server_capacity.search import METHOD, search

SUITE = "server_capacity"
ROOT = Path(__file__).resolve().parents[1]
BINARY_DIR = ROOT / SUITE / "native/build"
MIN_SAMPLES = 3


def cmd_new(database):
    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(BenchDB(database, SUITE)) as store:
        if store._connection.execute("SELECT 1 FROM config WHERE suite='server_limits_simple' LIMIT 1").fetchone():
            raise ValueError("Rename legacy runs first: python -m bench.server_capacity migrate <db>")
        uniform, varying = store.get_config(SUITE)
        for name, option in OPTIONS.items():
            if name not in uniform and name not in varying:
                store.set_config(SUITE, name, json.dumps(option.default))
    return 0


def placement():
    """Partition the CPUs actually allowed by this process's affinity mask."""
    cpus = sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else []
    middle = (len(cpus) + 1) // 2
    return {"server": cpus[:middle], "clients": cpus[middle:] or cpus}


def fingerprint():
    paths = [BINARY_DIR / "client", BINARY_DIR / "server"]
    if any(not path.is_file() for path in paths):
        raise ValueError("Build the native workers first: python -m bench.server_capacity build")
    paths += list((ROOT / SUITE).glob("*.py")) + list((ROOT / SUITE / "native").glob("*.*"))
    paths += list((ROOT / "common/servers").glob("*.py"))
    paths += [ROOT / "common/contract.h", ROOT / "common/hammer_client.h", ROOT / "common/histogram.h"]
    paths += [ROOT / "server_limits/run.py", ROOT / "common/workers.py"]
    # Editable o6 installs can keep their package version while changing the
    # Python or native implementation. Hash the installed code, not just its label.
    for package in ("o6", "asyncua"):
        spec = importlib.util.find_spec(package)
        if spec and spec.submodule_search_locations:
            for folder in spec.submodule_search_locations:
                paths += [p for p in Path(folder).rglob("*") if p.suffix in {".py", ".so", ".pyd"}]
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path).encode())
        digest.update(path.read_bytes())
    versions = {}
    for package in ("o6", "asyncua"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "unavailable"
    return {"sha256": digest.hexdigest(), "python_packages": versions, "cpus": placement()}


def aggregate(raw, start_ns, duration_ms):
    """Validate worker accounting before calculating a completion rate."""
    end_ns = start_ns + duration_ms * 1_000_000
    if not raw:
        raise ValueError("No clients reported results")
    for result in raw:
        required = {
            "start_ns",
            "end_ns",
            "actual_start_ns",
            "attempted",
            "succeeded",
            "late",
            "errors",
            "abandoned",
            "histogram_us",
            "send_refused",
            "broken",
            "cpu_ns",
            "full_window_ns",
            "response_wait_ns",
            "error_statuses",
        }
        if not required <= result.keys():
            raise ValueError("Client result is missing required fields")
        if result["start_ns"] != start_ns or result["end_ns"] != end_ns:
            raise ValueError("Client measured a different time window")
        if type(result["actual_start_ns"]) is not int or result["actual_start_ns"] < start_ns:
            raise ValueError("Client reported an invalid measurement start")
        counts = [result[key] for key in ("attempted", "succeeded", "late", "errors", "abandoned")]
        if any(type(n) is not int or n < 0 for n in counts) or counts[0] != sum(counts[1:]):
            raise ValueError("Client request accounting is inconsistent")
        histogram = result["histogram_us"]
        if (
            len(histogram) != BUCKETS
            or any(type(n) is not int or n < 0 for n in histogram)
            or sum(histogram) != result["succeeded"]
        ):
            raise ValueError("Client histogram does not match measured successes")
        if result["send_refused"]:
            raise ValueError(f"Load generator refused a send ({result['send_refused']}); capacity is unmeasured")
    window_ns = duration_ms * 1_000_000
    for result in raw:
        if any(type(result[k]) is not int or result[k] < 0 for k in ("cpu_ns", "full_window_ns", "response_wait_ns")):
            raise ValueError("Invalid CPU or pipeline timing")
        if result["full_window_ns"] > window_ns:
            raise ValueError("Pipeline timing exceeds the measurement window")
        if result["response_wait_ns"] > result["full_window_ns"]:
            raise ValueError("Response waiting exceeds response-pump wall time")
        statuses = result["error_statuses"]
        if (
            not isinstance(statuses, dict)
            or any(type(n) is not int or n < 0 for n in statuses.values())
            or sum(statuses.values()) != result["errors"]
        ):
            raise ValueError("Error statuses do not match the error count")
    counts = {key: sum(r[key] for r in raw) for key in ("attempted", "succeeded", "late", "errors", "abandoned")}
    histogram = merge([r["histogram_us"] for r in raw])
    failed = counts["errors"] + counts["abandoned"]
    start_delay_max_ms = max(r["actual_start_ns"] - start_ns for r in raw) / 1_000_000
    observation = {
        **counts,
        "requests_per_second": counts["succeeded"] * 1000 / duration_ms,
        "error_rate": failed / counts["attempted"] if counts["attempted"] else 1.0,
        "healthy": bool(counts["succeeded"]) and failed == 0 and not any(r["broken"] for r in raw),
        "p50_ms": percentile_ms(histogram, 0.5),
        "p99_ms": percentile_ms(histogram, 0.99),
        "duration_ms": duration_ms,
        "start_delay_max_ms": start_delay_max_ms,
        "histogram_us": histogram,
        "clients": raw,
        "client_cpu_max_fraction": max(r["cpu_ns"] / window_ns for r in raw),
        "client_cpu_total_fraction": sum(r["cpu_ns"] / window_ns for r in raw),
        "backpressure_wait_min_fraction": min(r["response_wait_ns"] / window_ns for r in raw),
        "error_statuses": {
            code: sum(r["error_statuses"].get(code, 0) for r in raw)
            for code in sorted({code for r in raw for code in r["error_statuses"]})
        },
    }
    if start_delay_max_ms > 10:
        observation["healthy"] = False
        observation["probe_error"] = (
            f"Client missed the shared start by {start_delay_max_ms:.3f}ms (limit 10ms); "
            "generator scheduling invalidated this measurement"
        )
    return observation


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("Worker phase exceeded its deadline")
    return value


def pin(process, cpus):
    if cpus and hasattr(os, "sched_setaffinity"):
        os.sched_setaffinity(process.pid, cpus)


@contextmanager
def running_server(implementation, deadline, report=lambda phase: None):
    """Keep one server alive until teardown or recovery after an unhealthy probe."""
    if port_in_use():
        raise RuntimeError("Port 4840 is already in use")
    server = None
    reader = None
    with tempfile.TemporaryDirectory(prefix="server-capacity-pki-") as pki:
        environment = os.environ.copy()
        if implementation == "node-opcua":
            environment = node_workers.environment()
        elif implementation == "ua-dotnet":
            environment = dotnet_workers.environment()
        elif implementation in sdk_workers.SDKS:
            environment = sdk_workers.environment(implementation)
        environment["O6_BENCHMARK_PKI_ROOT"] = pki
        try:
            report("starting server")
            server = subprocess.Popen(
                server_command(implementation, BINARY_DIR / "server"),
                cwd=ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            pin(server, placement()["server"])
            read_until(server, SERVER_READY, implementation, timeout_seconds=min(30, remaining(deadline)))
            reader = threading.Thread(target=drain, args=(server.stdout,), daemon=True)
            reader.start()
            wait_for_server(server, timeout_seconds=min(10, remaining(deadline)))
            yield server
        finally:
            if server is not None:
                report("stopping server")
                teardown_server(server)
                if reader:
                    reader.join(timeout=2)
                if server.stdout:
                    server.stdout.close()


class WarmupFailed(RuntimeError):
    """A connected client failed under warmup load; recover below this load."""


class ConnectionFailed(RuntimeError):
    """The server could not establish all sessions for this load configuration."""


def amendment_compatible(previous, current, updates):
    """Accept exact identities or an explicitly audited runner-only transition."""
    return previous == current or any(update.get("before") == previous and update.get("after") == current for update in updates)


def needs_restart(observation):
    """Keep a drained server after a late start or queue rejection, preserving JIT state."""
    if "server_exit" in observation:
        return True
    if observation["healthy"]:
        return False
    workers = observation.get("clients", [])
    if (
        observation.get("start_delay_max_ms", 0) > 10
        and workers
        and all(not r["errors"] and not r["broken"] and not r["send_refused"] and not r["abandoned"] for r in workers)
    ):
        return False
    statuses = {code for code, count in observation.get("error_statuses", {}).items() if count}
    busy_codes = {"0x80ee0000", "0x80100000"}  # BadServerTooBusy, BadTooManyOperations
    drained_rejections = (
        workers
        and statuses
        and all(code.split()[0].lower() in busy_codes for code in statuses)
        and all(not r["broken"] and not r["send_refused"] and not r["abandoned"] for r in workers)
    )
    return not drained_rejections


def measure(case, settings, server, deadline, report=lambda phase: None):
    """Warm fresh clients, measure one shared window, and drain before reuse."""
    if server.poll() is not None:
        raise RuntimeError("Server exited before the next probe")
    clients = []
    try:
        report("connecting clients")
        ready_deadline = min(deadline, time.monotonic() + 30 + (settings["warmup_ms"] + settings["grace_ms"]) / 1000)
        for index in range(case["clients"]):
            command = [str(BINARY_DIR / "client"), "--outstanding", str(case["outstanding"]), "--seed", str(index + 1)]
            if "endpoint" in settings:
                command += ["--endpoint", settings["endpoint"]]
            for option in ("duration_ms", "warmup_ms", "timeout_ms", "grace_ms"):
                command += ["--" + option.replace("_", "-"), str(settings[option])]
            client = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
            clients.append(client)
            pin(client, placement()["clients"])
        for index, client in enumerate(clients):
            try:
                read_until(client, "O6_CAPACITY_CONNECTED", f"client {index}", timeout_seconds=remaining(ready_deadline))
            except RuntimeError as error:
                if "connect failed:" in str(error):
                    raise ConnectionFailed(str(error)) from error
                raise
        report("warming clients")
        for client in clients:
            client.stdin.write("WARMUP\n")
            client.stdin.flush()
        for index, client in enumerate(clients):
            try:
                read_until(client, "O6_CAPACITY_READY", f"client {index}", timeout_seconds=remaining(ready_deadline))
            except RuntimeError as error:
                if "warmup failed:" in str(error):
                    raise WarmupFailed(str(error)) from error
                raise
        report("synchronizing clients")
        start_ns = time.monotonic_ns() + 100_000_000
        for client in clients:
            client.stdin.write(f"{start_ns}\n")
            client.stdin.flush()
        collect_deadline = min(deadline, time.monotonic() + (settings["duration_ms"] + settings["grace_ms"]) / 1000 + 5)

        def collect(client):
            output, _ = client.communicate(timeout=remaining(collect_deadline))
            if client.returncode:
                raise RuntimeError(f"Client exited with {client.returncode}: {output[-4000:]}")
            rows = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
            if len(rows) != 1:
                raise ValueError("Client did not produce exactly one result")
            return rows[0]

        report("measuring")
        with ThreadPoolExecutor(max_workers=len(clients)) as pool:
            futures = [pool.submit(collect, client) for client in clients]
            # Update phase without changing the worker's measurement timing.
            from concurrent.futures import wait

            wait(futures, timeout=min(remaining(collect_deadline), settings["duration_ms"] / 1000 + 0.1))
            report("draining responses")
            raw = [future.result() for future in futures]
        result = aggregate(raw, start_ns, settings["duration_ms"])
        if server.poll() is not None:
            result["healthy"] = False
            result["server_exit"] = server.returncode
        return result
    finally:
        for client in clients:
            if client.poll() is None:
                client.kill()
            client.wait()
            for stream in (client.stdin, client.stdout):
                if stream and not stream.closed:
                    stream.close()


class BudgetReached(RuntimeError):
    """The configured wall time or number of new probes has been exhausted."""


def cmd_sample(database, num_samples=3, amend=False, skip_failed=False):
    if type(num_samples) is not int or num_samples < MIN_SAMPLES:
        raise ValueError(f"At least {MIN_SAMPLES} confirmation samples are required")
    if skip_failed and not amend:
        raise ValueError("--skip-failed requires --amend")
    with closing(BenchDB(database, SUITE)) as store:
        settings, varying = store.get_config(SUITE)
        if store.stored_metadata.get("method") == "legacy_grid":
            raise ValueError("This is a preserved grid run; use a new name for the adaptive capacity search")
        if any(name not in settings for name, option in OPTIONS.items() if option.kind == "uniform"):
            raise ValueError("Seed settings first: python -m bench.server_capacity new <db>")
        if settings["grace_ms"] < settings["timeout_ms"]:
            raise ValueError("grace_ms must be at least timeout_ms")
        if settings["start_outstanding"] > settings["max_outstanding"]:
            raise ValueError("start_outstanding must not exceed max_outstanding")
        if not varying["implementation"]:
            raise ValueError("Select at least one implementation")
        identity = fingerprint()
        binary_stamp = (BINARY_DIR / "client").stat() if (BINARY_DIR / "client").exists() else None
        if store.results and not amend:
            raise ValueError("This run already has results; use a new name or --amend")
        optional_errors = {}
        for implementation, workers in (("ua-dotnet", dotnet_workers), ("node-opcua", node_workers)):
            if implementation in varying["implementation"]:
                try:
                    identity[implementation] = workers.preflight({"server"}, "server_limits")
                except (OSError, RuntimeError, ValueError) as error:
                    optional_errors[implementation] = str(error)
        for implementation in sdk_workers.NAMES:
            if implementation in varying["implementation"]:
                try:
                    identity[implementation] = sdk_workers.preflight(implementation, "server_limits")
                except RuntimeError as error:
                    optional_errors[implementation] = str(error)
        if amend and not amendment_compatible(
            store.stored_metadata.get("measurement_identity", identity),
            identity,
            store.stored_metadata.get("compatible_runner_updates", []),
        ):
            raise ValueError("Workers or CPU allocation changed; start a new run")
        store.metadata.update(store.stored_metadata)
        store.metadata.update(
            measurement_identity=identity,
            complete=False,
            samples_requested=num_samples,
            method=METHOD,
            capacities={},
        )
        if not amend:
            store.metadata["candidate_selections"] = {}
        store.metadata.setdefault("candidate_selections", {})
        with store.transaction():
            store.invalidate_reports()
            store.save_metadata(progress=True)
        encode = lambda key: json.dumps(key, sort_keys=True)
        saved = {encode(row["config_key"]): row["observation"] for row in store.results.values()}
        failures = {encode(row["config_key"]) for row in store.failures}
        implementations = list(dict.fromkeys(varying["implementation"]))
        progress = Progress(1, unit="probe", group="observation", right_width=42)
        complete = True
        try:
            for index, implementation in enumerate(implementations):
                deadline = time.monotonic() + settings["budget_seconds"]
                measured = 0
                sdk_key = {"implementation": implementation}
                with ExitStack() as workers:
                    server = None

                    def can_discover(clients, depth, reserve):
                        key = encode(
                            dict(
                                implementation=implementation,
                                clients=clients,
                                outstanding=depth,
                                phase="discovery",
                                repetition=0,
                            )
                        )
                        if key in saved:
                            return True
                        seconds = (settings["probe_ms"] + settings["warmup_ms"]) / 1000 + 0.1
                        seconds += reserve * ((settings["confirm_ms"] + settings["warmup_ms"]) / 1000 + 0.1)
                        seconds += settings["grace_ms"] / 1000 + (5 if server is None else 0)
                        return measured + 1 + reserve <= settings["max_probes"] and deadline - time.monotonic() >= seconds

                    def save_selection(selection):
                        store.metadata["candidate_selections"][implementation] = selection
                        store.save_metadata(progress=True)
                        chosen = ", ".join(f"{c['clients']}c × {c['outstanding']}o" for c in selection["candidates"])
                        progress.write(f"{implementation}: top 10% candidates: {chosen or 'none'}; {num_samples} repeats each")

                    def probe(clients, depth, phase, repetition):
                        nonlocal server, measured
                        case = dict(
                            implementation=implementation,
                            clients=clients,
                            outstanding=depth,
                            phase=phase,
                            repetition=repetition,
                        )
                        key = encode(case)
                        if key in saved:
                            return saved[key]
                        if binary_stamp is not None:
                            current_stamp = (BINARY_DIR / "client").stat()
                            if (current_stamp.st_mtime_ns, current_stamp.st_size) != (
                                binary_stamp.st_mtime_ns,
                                binary_stamp.st_size,
                            ):
                                raise RuntimeError(
                                    "Measurement client changed during the run; start a fresh run after the build finishes"
                                )
                        if skip_failed and key in failures:
                            raise RuntimeError("A failed probe was skipped; search remains incomplete")
                        duration = settings["probe_ms"] if phase == "discovery" else settings["confirm_ms"]
                        warmup = settings["warmup_ms"]
                        if measured >= settings["max_probes"] or deadline - time.monotonic() < (duration + warmup) / 1000 + 0.1:
                            raise BudgetReached("Probe/time budget reached before confirmation finished")
                        remaining_probes = settings["max_probes"] - measured
                        later = len(implementations) - index - 1
                        progress.estimate(
                            remaining_probes + later * settings["max_probes"],
                            max(0, deadline - time.monotonic()) + later * settings["budget_seconds"],
                        )
                        label = f"{implementation} {clients}c × {depth}o {phase} #{repetition + 1}"
                        progress.begin(label)
                        try:
                            if server is None:
                                server = workers.enter_context(running_server(implementation, deadline, progress.state))
                            observation = measure(
                                case,
                                {**settings, "duration_ms": duration, "warmup_ms": warmup},
                                server,
                                deadline,
                                progress.state,
                            )
                        except BudgetReached:
                            raise
                        except (WarmupFailed, ConnectionFailed) as error:
                            if time.monotonic() >= deadline:
                                raise BudgetReached("Server time budget exhausted during client setup") from error
                            observation = dict(
                                healthy=False,
                                requests_per_second=0,
                                error_rate=None,
                                p50_ms=None,
                                p99_ms=None,
                                client_cpu_max_fraction=0,
                                client_cpu_total_fraction=0,
                                backpressure_wait_min_fraction=0,
                                error_statuses={},
                                probe_error=str(error),
                                duration_ms=0,
                            )
                        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                            if time.monotonic() >= deadline:
                                raise BudgetReached("Server time budget exhausted during a probe") from error
                            store.note_failure(dict(config_key=case, configuration=key, error=str(error)))
                            progress.advance(label, "FAILED")
                            progress.write(str(error))
                            raise
                        measured += 1
                        store.write_result(case, dict(config_key=case, observation=observation))
                        saved[key] = observation
                        progress.advance(
                            label,
                            (
                                f"INVALID — {observation['probe_error']}"
                                if "probe_error" in observation
                                else f"{observation['requests_per_second']:,.0f} req/s"
                                + (" (overload)" if not observation["healthy"] else "")
                            ),
                        )
                        if needs_restart(observation):
                            workers.close()
                            server = None
                        return observation

                    try:
                        if implementation in optional_errors:
                            raise RuntimeError(optional_errors[implementation])
                        available = len(placement()["clients"]) if hasattr(os, "sched_setaffinity") else 0
                        # One shared CPU cannot verify independent load-generator headroom.
                        if set(placement()["clients"]) & set(placement()["server"]):
                            available = 0
                        result = search(
                            settings,
                            num_samples,
                            probe,
                            client_cpus=available,
                            selection=store.metadata["candidate_selections"].get(implementation),
                            save_selection=save_selection,
                            can_discover=can_discover,
                        )
                        if result["status"] == "budget_exhausted":
                            complete = False
                        store.clear_failure(encode(sdk_key))
                    except BudgetReached as error:
                        complete = False
                        result = dict(status="budget_exhausted", reason=str(error), capacity_requests_per_second=None)
                    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                        complete = False
                        result = dict(status="failed", reason=str(error), capacity_requests_per_second=None)
                        if implementation in optional_errors:
                            store.note_failure(dict(config_key=sdk_key, configuration=encode(sdk_key), error=str(error)))
                        progress.write(f"{implementation}: {error}")
                    result.setdefault(
                        "highest_observed_requests_per_second",
                        max(
                            (
                                value["requests_per_second"]
                                for key, value in saved.items()
                                if json.loads(key)["implementation"] == implementation and value["healthy"]
                            ),
                            default=0,
                        ),
                    )
                    result["new_probes"] = measured
                    store.metadata["capacities"][implementation] = result
                    store.save_metadata(progress=True)
                    rate = result["highest_observed_requests_per_second"]
                    progress.write(
                        f"{implementation}: {result['status']}"
                        + (f" — {rate:,.0f} req/s maximum observed" if rate else f" — {result['reason']}")
                    )
            store.metadata["complete"] = complete and not store.failures
            store.save_metadata(progress=True)
            progress.estimate(0, 0)
            return 0 if store.metadata["complete"] else 1
        finally:
            progress.finish()
