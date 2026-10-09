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

"""Run Read/Write throughput measurements across configured client/server pairs."""

from __future__ import annotations

import os
import sys

import argparse
import dataclasses
import datetime
import importlib.metadata
import json
import statistics
import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from common.bench_db import BenchDB, _freeze
from common.certificates import certificate_material, require_certificate_tool, secure_arguments
from common.contract import SERVER_READY
from common.oom import (
    CLIENT_OOM_SCORE_ADJ,
    SERVER_OOM_SCORE_ADJ,
    WORKER_ADDRESS_SPACE_RESERVE_MB,
    available_memory_bytes,
    child_limits,
    out_of_memory,
)
from common.progress import Progress
from common.runner import (
    compact,
    confirm,
    executable,
    pair_to_tuple as runner_pair_to_tuple,
    parse_first_json_line,
    partition_applicable,
)
from common.suites import walk_matrix
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
from throughput.benchmark_common import (
    NAMED_ARRAY_SIZES,
    REQUEST_TIMEOUT_MS,
    service_calls,
)
from throughput.options import OPTIONS, _ALL_PAIRS
from common import dotnet_workers, node_workers, sdk_workers

ROOT = Path(__file__).resolve().parents[1]
# Per-implementation worker scripts. Each implementation lives in its own
# subdirectory and follows the naming convention ``<role>.py``. The
# C/open62541 implementation has no Python script here; its executables live
# under the open62541/ subdirectory and are launched directly.
BENCH_DIR = Path(__file__).parent
# The Python servers used to live here, one per suite; they were all strict
# subsets of one program, so they were collapsed into a single shared
# program under common/servers/. The runner picks the same executable for
# every suite; the only thing that changes is --array-sizes, --security
# and --port.
SERVERS_DIR = ROOT / "common" / "servers"
O6_CLIENT = BENCH_DIR / "o6" / "client.py"
O6_SERVER = SERVERS_DIR / "o6_server.py"
ASYNCUA_CLIENT = BENCH_DIR / "asyncua" / "client.py"
ASYNCUA_SERVER = SERVERS_DIR / "asyncua_server.py"

IMPLEMENTATIONS: tuple[str, ...] = ("open62541", "o6-python", "asyncua", "ua-dotnet", "node-opcua") + sdk_workers.NAMES
ALL_PAIRS = _ALL_PAIRS
SECURITY_POLICIES: tuple[str, ...] = ("None", "Basic256Sha256")
PAIR_STRINGS: tuple[str, ...] = tuple(sorted(f"{client}:{server}" for client, server in ALL_PAIRS))
# Every benchmarked value is an Int32, so the payload a sample moved is its
# value count times this. Payload only: the OPC UA framing around it, the
# DataValue wrapper, and TCP overhead are not counted, so the reported MB/s is
# useful data moved rather than bytes on the wire.
BYTES_PER_VALUE = 4
# How much of the memory that is free when a run starts its workers may hold
# between them; the rest is what the machine keeps doing everything else with.
# This is the sizing policy :mod:`common.oom` deliberately leaves to the runner
# — see :func:`worker_address_space` for how a share is cut from it.
MEMORY_BUDGET_FRACTION = 0.6


def megabytes_per_second(values: int, elapsed_ns: int) -> float:
    """Payload rate of ``values`` Int32s moved in ``elapsed_ns`` nanoseconds."""
    return values * BYTES_PER_VALUE * 1_000.0 / elapsed_ns


_ASYNCUA_WATCHDOG_DISCONNECT = "asyncua-watchdog-head-of-line"
ASYNCUA_IMPLEMENTATION = "asyncua"


def classify_failure(error: str) -> str | None:
    """Tag a worker's error text with the class of failure it belongs to.

    Returns ``"asyncua-watchdog-head-of-line"`` when the error carries the
    asyncua watchdog race's stack — ``ConnectionError("client is
    disconnected")`` on the worker's pre-ready path — and ``None`` for
    everything else, including connection errors that come from elsewhere.
    The runner attaches the returned tag as the ``diagnostic`` field of
    the failure entry it writes to the document.
    """
    if "client is disconnected" in error and "ConnectionError" in error:
        return _ASYNCUA_WATCHDOG_DISCONNECT
    return None


def failure_entry(
    label: str,
    config_key: dict,
    samples_recorded: int,
    samples_requested: int,
    error: str,
) -> dict:
    """Build the dict :meth:`common.bench_db.BenchDB.note_failure` consumes.

    Wraps the four fields the runner always carries with the optional
    ``diagnostic`` tag from :func:`classify_failure` when the error
    belongs to a known failure class. The runner's two failure call
    sites (a worker's exit, a server's failure to start) share this
    shape.
    """
    payload = {
        "configuration": label,
        "config_key": config_key,
        "samples_recorded": samples_recorded,
        "samples_requested": samples_requested,
        "error": error,
    }
    diagnostic = classify_failure(error)
    if diagnostic is not None:
        payload["diagnostic"] = diagnostic
    return payload


def summarise_failures(failures: list[dict], retry: str) -> str:
    """Format the end-of-run failures line on stderr.

    Total failure count is always named. A non-zero count of entries
    carrying a ``diagnostic`` field is broken out by tag, so a reader
    knows whether one failure class dominated the matrix shrink. The
    breakdown is omitted when no tagged entry exists, so a clean run
    does not advertise a class that did not occur.
    """
    total = len(failures)
    tagged: dict[str, int] = {}
    for entry in failures:
        tag = entry.get("diagnostic")
        if isinstance(tag, str):
            tagged[tag] = tagged.get(tag, 0) + 1
    breakdown = ""
    if tagged:
        parts = [f"{count} tagged {tag}" for tag, count in sorted(tagged.items())]
        breakdown = f"; {', '.join(parts)}"
    return f"{total} configuration(s) could not be measured{breakdown}; see 'failures' in the output document. {retry}"


def asyncua_watchdog_note(config_keys: list[dict]) -> None:
    """Print the asyncua watchdog interval the workers will use.

    Fires once per ``sample`` run when the matrix contains any
    asyncua-client configuration, so a captured log proves which
    interval a particular sample ran with. Suppressed when the matrix
    has no asyncua client — a configuration whose every pairing uses
    o6-python or open62541 has no watchdog to advertise.

    The interval is :data:`throughput.benchmark_common.REQUEST_TIMEOUT_MS`
    expressed in seconds, the same constant the request timeout is bounded
    by. Tying the two keeps the watchdog's probe budget from drifting
    apart from the request timeout in a future change.
    """
    has_asyncua_client = any(
        str(config_key.get("pair", "")).split(":", 1)[0] == ASYNCUA_IMPLEMENTATION for config_key in config_keys
    )
    if not has_asyncua_client:
        return

    print(f":: asyncua client watchdog interval: {REQUEST_TIMEOUT_MS / 1000} s", file=sys.stderr)


def applicable(config_key: dict) -> bool:
    """Whether this suite's matrix has a measurement for ``config_key``."""
    return True


def holds_c_binaries(value: object, fields: dict) -> bool | str:
    """The compiled open62541 programs the matrix needs must be on disk."""
    directory = Path(str(value)).resolve()
    pairs = [pair_to_tuple(pair) for pair in fields.get("pair") or []]
    wanted = set()
    if any(server == "open62541" for _, server in pairs):
        wanted.add(executable(directory, "server"))
    if any(client == "open62541" for client, _ in pairs):
        wanted.add(executable(directory, "client"))
    missing = sorted(str(path) for path in wanted if not path.is_file())
    return not missing or ("is missing the C benchmark executable(s) " + ", ".join(missing) + " — build them per the README")


# The per-suite configuration fields this runner reads from the ``config``
# table at sample time and writes into it on ``new``; declared in
# :mod:`throughput.options`. Field order is deliberate: ``security`` and
# ``pair`` are the outer axes so the runner can group configurations by
# server (one process per (pair, security)) and avoid paying for a server
# restart on every configuration.
SUITE_NAME = "throughput"


@dataclasses.dataclass(frozen=True)
class Payload:
    """One payload shape: scalar, a batch of scalar nodes, or one array node.

    ``batch_size`` and ``array_size`` are never both above one, and no matrix
    point can ask for that: a configuration names one shape, so the batch curve
    and the array curve cannot confound each other and the workers (which
    refuse batched array access) never see a combination they reject.

    ``array_sizes`` is the full set of arrays the server exposes, in the
    server's order, because that position is what fixes each array's NodeId on
    both ends. Passing anything narrower makes the worker address a different
    array than the one it reports.
    """

    batch_size: int
    array_size: int
    array_sizes: tuple[int, ...]

    @classmethod
    def from_token(cls, token: object, array_sizes: tuple[int, ...]) -> "Payload":
        """Read one matrix ``payload`` value: ``scalar``, ``batch:N``, ``array:M``.

        Raises :class:`ValueError` naming the token, so a typo in the document
        is a one-line complaint before the first server starts rather than a
        worker dying mid-matrix.
        """
        text = str(token).strip().lower()
        if text == "scalar":
            return cls(1, 1, array_sizes)
        kind, separator, size = text.partition(":")
        if separator and kind == "batch":
            try:
                nodes = int(size)
            except ValueError:
                raise ValueError(f"{token!r}: batch size {size!r} is not an integer") from None
            if nodes < 2:
                raise ValueError(f"{token!r}: a batch needs at least 2 nodes; " "single-node access is 'scalar'")
            return cls(nodes, 1, array_sizes)
        if separator and kind == "array":
            try:
                elements = resolve_array_size(size)
            except ValueError as error:
                raise ValueError(f"{token!r}: {error}") from None
            if elements < 2:
                raise ValueError(f"{token!r}: an array needs at least 2 elements; " "one element on one node is 'scalar'")
            return cls(1, elements, array_sizes)
        raise ValueError(f"{token!r} is not 'scalar', 'batch:<nodes>', or " "'array:<elements|name>'")

    @property
    def values_per_call(self) -> int:
        return self.batch_size * self.array_size

    @property
    def array_sizes_argument(self) -> str:
        return ",".join(str(size) for size in self.array_sizes)

    def label(self) -> str:
        if self.array_size > 1:
            return f"array {self.array_size}"
        if self.batch_size > 1:
            return f"batch {self.batch_size}"
        return "scalar"


def exposed_arrays(payloads: list) -> tuple[int, ...]:
    """The array element counts the server must publish, in NodeId order.

    Sorted rather than taken in document order: the position in this list is
    what fixes each array variable's NodeId on both ends, so reordering the
    ``payload`` axis by hand must not silently repoint a NodeId. Shapes that
    need no array node contribute nothing.
    """
    sizes = {Payload.from_token(token, ()).array_size for token in payloads}
    return tuple(sorted(size for size in sizes if size > 1))


def resolve_array_size(value: int | str) -> int:
    """Resolve an element count written as a number or a named pixel count.

    An ``array:`` shape carries what the user wrote (``"vga"`` and friends, or
    a plain integer); the worker command line needs an int. Anything we cannot
    understand is rejected loudly so a typo does not silently measure a
    different payload than the one asked for.
    """
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        if value in NAMED_ARRAY_SIZES:
            return NAMED_ARRAY_SIZES[value]
        try:
            return int(value)
        except ValueError:
            raise ValueError(
                f"element count {value!r} is not an integer and not one of " f"{', '.join(NAMED_ARRAY_SIZES)}"
            ) from None
    raise ValueError(f"element count {value!r} is not an integer or a named size")


def aggregate_row(config_key: dict, runs: list[dict]) -> dict:
    """Store the configuration, raw runs, and aggregates recomputed over all runs."""
    call_rates = sorted(float(run["ops_per_second"]) for run in runs)
    data_rates = sorted(float(run["mb_per_second"]) for run in runs)
    stats: dict = {}
    if call_rates:
        stats = {
            "median_ops_per_second": round(statistics.median(call_rates), 3),
            "min_ops_per_second": round(call_rates[0], 3),
            "max_ops_per_second": round(call_rates[-1], 3),
            "median_mb_per_second": round(statistics.median(data_rates), 6),
            "min_mb_per_second": round(data_rates[0], 6),
            "max_mb_per_second": round(data_rates[-1], 6),
        }
    return {"config_key": dict(config_key), "runs": runs, "stats": stats}


def compact_mb(value: float) -> str:
    """A MB/s figure in at most six characters, sub-1 rates included.

    :func:`compact` is built for call rates, which are whole numbers on any
    configuration worth reporting; a scalar payload moves thousandths of a
    megabyte per second and would round to a bare 0 there.
    """
    if value < 1:
        return f"{value:.3f}"
    if value < 1_000:
        return f"{value:.1f}"
    return f"{value / 1e3:.2f}G"


def worker_address_space(clients: int, available_bytes: int) -> int:
    """Address space one worker may map, from the free memory and the client count.

    Derived rather than configured: what a worker may hold is a property of the
    machine it runs on and of how many of them run at once, both of which are
    known by the time a run starts. ``available_bytes`` is shared out between
    the clients of the widest configuration in the matrix and the one server
    they talk to, and each share carries
    :data:`WORKER_ADDRESS_SPACE_RESERVE_MB` on top for the address space an
    interpreter maps before it does any work — without which the reserve would
    eat the share and every worker would die on startup.

    Zero for a machine whose free memory could not be read, which leaves the
    workers unlimited: a limit guessed at from nothing would fail
    configurations the machine could have measured.
    """
    if available_bytes <= 0:
        return 0
    share = int(available_bytes * MEMORY_BUDGET_FRACTION) // (clients + 1)
    return share + WORKER_ADDRESS_SPACE_RESERVE_MB * 1024 * 1024


def out_of_memory_message(label: str, address_space_bytes: int, output: str) -> str:
    """What a worker that ran out of memory is recorded and reported as.

    The diagnosis goes in front of the worker's own output rather than after
    it: none of the ways a worker reports this name what it ran into, one of
    them (the OOM killer's SIGKILL) says nothing at all, and the traceback the
    others carry is thousands of characters long — leaving the sentence that
    explains the failure at the bottom of the stored record.
    """
    working = max(0, address_space_bytes - WORKER_ADDRESS_SPACE_RESERVE_MB * 1024 * 1024) // (1024 * 1024)
    held = f"the {working} MB each worker is held to" if address_space_bytes else "the memory on this machine"
    return (
        f"{label} ran out of memory: this configuration wants more than {held}. "
        "Measure it with fewer 'clients' or a smaller payload, or free memory on the "
        f"machine and retry it with --amend.\n{output}"
    )


def parse_worker_result(output: str, label: str) -> dict[str, object]:
    result = parse_first_json_line(output, label)
    if "start_ns" in result and "end_ns" in result:
        return result
    raise RuntimeError(f"No worker result from {label}:\n{output}")


def run_sample(
    client_command: list[str],
    mode: str,
    operation: str,
    clients: int,
    iterations: int,
    warmup: int,
    max_outstanding: int,
    payload: Payload,
    environment: dict[str, str] | None = None,
    memory_bytes: int = 0,
    client_preexec: Callable[[], None] | None = None,
    report: Callable[[str], None] = lambda _phase: None,
) -> dict:
    """Run one sample and return its record.

    The record carries the raw totals (elapsed time, service calls, values) as
    well as the two derived rates, so a stored sample can be re-checked — or
    re-expressed in another unit — without rerunning it.

    ``memory_bytes`` is the address space each client is started with (see
    :func:`worker_address_space`), so a configuration that wants more memory
    than the machine has to spare ends as one failed configuration rather than
    as a machine in swap.

    ``report`` is called with the phase this sample is in, for the caller to
    display. A sample is one blocking wait from out here — the clients warm up,
    measure, and exit on their own — so without it a configuration that takes
    minutes looks identical to a wedged one. The counts it reports are what
    distinguishes the two: a client that has not signalled ready yet is still
    warming up, and warming up on a 4k array is slow but not stuck.

    The phases reported here are the ones inside a single sample; which sample
    that is belongs to the caller, which knows the sample count, and is added
    by it in front. Warm-up runs once per sample, not once per configuration,
    so the two are shown together rather than one at a time.
    """
    is_dotnet = bool(client_command and Path(client_command[0]) == dotnet_workers.DOTNET)
    is_node = bool(client_command and Path(client_command[0]) == node_workers.NODE)
    watches = []
    if is_node:
        environment = node_workers.environment(environment)
        node_workers.check_memory(
            payload.array_size, payload.batch_size, max_outstanding if mode == "async" else 1, memory_bytes
        )
        client_command = node_workers.memory_command(client_command, memory_bytes)
    if is_dotnet:
        environment = dotnet_workers.environment(memory_bytes, environment)
        dotnet_workers.check_memory(
            payload.array_size, payload.batch_size, max_outstanding if mode == "async" else 1, memory_bytes
        )
    processes: list[subprocess.Popen[str]] = []
    prefixes: list[list[str]] = []
    try:
        for index in range(clients):
            command = client_command + [
                "--iterations",
                str(iterations),
                "--warmup",
                str(warmup),
                "--samples",
                "1",
                "--worker",
                mode,
                "--operation",
                operation,
                "--max-outstanding",
                str(max_outstanding if mode == "async" else 1),
                "--seed",
                str(index + 1),
                "--batch-size",
                str(payload.batch_size),
                "--array-size",
                str(payload.array_size),
                "--array-sizes",
                payload.array_sizes_argument,
            ]
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=environment,
                preexec_fn=compose_preexec(
                    client_preexec,
                    child_limits(0 if is_dotnet or is_node else memory_bytes, CLIENT_OOM_SCORE_ADJ),
                ),
            )
            processes.append(process)
            if is_node:
                watches.append(node_workers.MemoryWatch(process, memory_bytes))
        # Connect, NodeId resolution and warm-up all happen before a client
        # signals ready, so this wait is the slow part of a large payload.
        report(f"warmup 0/{clients} ready")
        for index, process in enumerate(processes):
            try:
                prefixes.append(read_until(process, "O6_BENCHMARK_READY", f"client {index}"))
            except RuntimeError as error:
                # A payload's memory is committed by the end of warm-up, so
                # this is where a configuration too large for the machine dies
                # — checked here as well as on the timed path below, or the
                # reason it stopped is nowhere in the record.
                if "timed out waiting" not in str(error) and out_of_memory(process.poll(), str(error)):
                    raise RuntimeError(out_of_memory_message(f"client {index}", memory_bytes, str(error))) from None
                raise
            report(f"warmup {index + 1}/{clients} ready")
        report("measuring")
        for process in processes:
            assert process.stdin is not None
            process.stdin.write("\n")
            process.stdin.flush()

        results = []
        for index, process in enumerate(processes):
            assert process.stdout is not None
            remainder, _ = process.communicate(timeout=120)
            return_code = process.returncode
            report(f"collecting {index + 1}/{clients}")
            output = "".join(prefixes[index]) + remainder
            if return_code != 0:
                if out_of_memory(return_code, output):
                    raise RuntimeError(out_of_memory_message(f"client {index}", memory_bytes, output))
                raise RuntimeError(f"client {index} failed with {return_code}:\n{output}")
            results.append(parse_worker_result(output, f"client {index}"))

        start = min(int(result["start_ns"]) for result in results)
        end = max(int(result["end_ns"]) for result in results)
        operations = sum(int(result["operations"]) for result in results)
        values = sum(int(result["operations"]) * int(result.get("values_per_operation", 1)) for result in results)
        elapsed = end - start
        if elapsed <= 0:
            # A clock that did not advance would otherwise divide by zero and
            # take the whole matrix down with it.
            raise RuntimeError(f"sample spanned {elapsed} ns across {clients} client(s); no rate to derive")
        return {
            "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "elapsed_ns": elapsed,
            "operations": operations,
            "values": values,
            "ops_per_second": round(operations * 1_000_000_000.0 / elapsed, 3),
            "mb_per_second": round(megabytes_per_second(values, elapsed), 6),
            **(
                {
                    "node_opcua_diagnostics": {
                        "workers": [r.get("node_opcua_diagnostics", {}) for r in results],
                        "rss_peaks": [w.peak for w in watches],
                    }
                }
                if is_node
                else {}
            ),
        }
    finally:
        for watch in watches:
            watch.close()
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait()


def pair_to_tuple(pair: str) -> tuple[str, str]:
    """Split a ``client:server`` matrix value into its two implementation names.

    Thin wrapper around :func:`common.runner.pair_to_tuple` that pins this
    suite's allowed implementations.
    """
    return runner_pair_to_tuple(pair, IMPLEMENTATIONS)


def configuration_label(config_key: dict, server_arrays: tuple[int, ...]) -> str:
    """The one-line name a configuration is shown and recorded under.

    Derived from the config_key rather than stored anywhere, so the label on a
    progress line, the label on a failure, and the row they belong to cannot
    drift apart.
    """
    client, server = pair_to_tuple(config_key["pair"])
    payload = Payload.from_token(config_key["payload"], server_arrays)
    return (
        f"{client}/{server} {config_key['operation']}/{config_key['mode']} "
        f"{payload.label()} {int(config_key['clients'])}c "
        f"{'B256' if config_key['security'] != 'None' else 'plain'}"
    )


def run_one_configuration(
    config_key: dict,
    store: BenchDB,
    num_samples: int,
    restore_runs: list[dict],
    client_command: list[str],
    client_environment: dict[str, str] | None,
    client_memory_bytes: int,
    server_arrays: tuple[int, ...],
    progress: Progress,
    client_preexec: Callable[[], None] | None,
) -> None:
    """Measure one configuration and push the result through :class:`BenchDB`.

    ``restore_runs`` is the list of samples already on disk for this
    configuration (the ``runs`` array of the stored row) when ``--amend`` is
    set; empty otherwise. The function takes whatever is needed to reach
    ``num_samples`` and writes a fresh aggregated row after every sample.

    ``client_memory_bytes`` is the address space each client is started with,
    so a configuration too large for the machine is recorded as an
    out-of-memory failure instead of measured; ``server_arrays`` is the array
    set the running server exposes, passed through to the client so both ends
    agree on which NodeId an array size means.
    """
    operation = config_key["operation"]
    mode = config_key["mode"]
    clients = int(config_key["clients"])
    iterations = int(config_key["iterations"])
    warmup = int(config_key["warmup"])
    max_outstanding = int(config_key["max_outstanding"])
    max_values = int(config_key["max_values"])
    min_calls = int(config_key["min_service_calls"])

    payload = Payload.from_token(config_key["payload"], server_arrays)
    runs = list(restore_runs)
    needed = max(0, num_samples - len(runs))
    if needed == 0:
        return  # already complete on disk; nothing to do for this configuration

    label = configuration_label(config_key, server_arrays)
    progress.begin(label + (f" (+{needed})" if runs else ""))

    # --iterations is the requested call count. It is honoured unless the
    # payload is large enough that the value budget has to cut it. The cut is
    # not stored: it is the same pure function of the config_key wherever it is
    # asked for, so a reader recomputes it rather than trusting a stale copy.
    calls = service_calls(iterations, payload.values_per_call, max_values, min_calls)
    warmup_calls = min(warmup, max(1, calls))

    row: dict | None = None
    taken = 0
    try:
        if pair_to_tuple(config_key["pair"])[1] == "node-opcua":
            node_workers.check_memory(
                payload.array_size,
                payload.batch_size,
                clients * (max_outstanding if mode == "async" else 1),
                client_memory_bytes,
                server_arrays,
            )
        if pair_to_tuple(config_key["pair"])[1] == "ua-dotnet":
            dotnet_workers.check_memory(
                payload.array_size,
                payload.batch_size,
                clients * (max_outstanding if mode == "async" else 1),
                client_memory_bytes,
            )
        for _ in range(needed):
            # Numbered against the target rather than this run's share of it,
            # so a topped-up configuration reads "sample 3/3", not "sample 1/1".
            sample_number = len(runs) + 1
            run = run_sample(
                client_command,
                mode,
                operation,
                clients,
                calls,
                warmup_calls,
                max_outstanding,
                payload,
                client_environment,
                client_memory_bytes,
                client_preexec=client_preexec,
                report=lambda phase, n=sample_number: progress.state(f"sample {n}/{num_samples} · {phase}"),
            )
            run["index"] = len(runs)
            runs.append(run)
            row = aggregate_row(config_key, runs)
            store.write_result(config_key, row, append_field="runs")
            taken += 1
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        # One configuration failing is not a reason to throw away a matrix
        # that can take hours. Record it, say so, and move on.
        # SubprocessError is in the list for the two ways a worker fails
        # without ever running: a client that outlives the collection timeout,
        # and one the limits in :func:`child_limits` refused to start under.
        with store.transaction():
            if not runs:
                # Nothing measured this configuration, here or in the samples this
                # run was asked to keep. Any row on disk for it was measured by an
                # earlier run, and leaving it would present that as this run's
                # result — with the full sample count on it, and no way to tell
                # from the row itself. The failure below is the record now.
                store.drop_result(config_key)
            store.note_failure(
                failure_entry(
                    label=label,
                    config_key=config_key,
                    samples_recorded=len(runs),
                    samples_requested=num_samples,
                    error=str(error),
                )
            )
        progress.advance(
            left=f"{label} FAILED after {len(runs)}/{num_samples}",
            right="see failures",
            steps=taken,
        )
        return

    store.clear_failure(label)
    if row is not None and row["stats"]:
        stats = row["stats"]
        low = float(stats["min_ops_per_second"])
        high = float(stats["max_ops_per_second"])
        spread = high / low if low else 0
        progress.advance(
            left=label + (" !" if spread > 2 else ""),
            right=(
                f"{compact(float(stats['median_ops_per_second'])):>7} c/s "
                f"{compact_mb(float(stats['median_mb_per_second'])):>7} MB/s"
            ),
            steps=taken,
        )


def start_server(
    server_command: list[str],
    server_marker: str,
    server_environment: dict[str, str] | None,
    memory_bytes: int = 0,
    server_preexec: Callable[[], None] | None = None,
) -> subprocess.Popen[str]:
    """Launch the server, wait for the readiness marker, and drain its logs.

    ``server_preexec`` is the per-server pinning+OOM callable computed
    once in :func:`cmd_sample`; ``None`` falls back to the unpinned
    ``child_limits`` path so a caller that has not opted in still works.
    """
    is_node = Path(server_command[0]) == node_workers.NODE
    if is_node:
        server_command = node_workers.memory_command(server_command, memory_bytes)
    preexec = compose_preexec(
        server_preexec,
        child_limits(0 if is_node else memory_bytes, SERVER_OOM_SCORE_ADJ),
    )
    server = subprocess.Popen(
        server_command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=(os.name != "nt"),
        env=server_environment,
        preexec_fn=preexec,
    )
    if is_node:
        server.node_memory_watch = node_workers.MemoryWatch(server, memory_bytes)
    try:
        startup = read_until(server, server_marker, server_command[0])
        if is_node:
            for line in startup:
                if line.startswith('{"node_opcua_limits":'):
                    server.node_effective_limits = json.loads(line)["node_opcua_limits"]
        assert server.stdout is not None
        threading.Thread(target=drain, args=(server.stdout,), daemon=True).start()
        wait_for_server(server)
    except BaseException:
        # A server that never signalled ready can still be running, and would
        # hold port 4840 against the next attempt at it.
        teardown_server(server)
        raise
    return server


def server_command(
    implementation: str,
    c_server: Path,
    secure: list[str],
    security: str,
    array_sizes: tuple[int, ...],
    memory_bytes: int = 0,
) -> list[str]:
    """The ``subprocess.Popen`` command that starts one implementation's server.

    ``secure`` is the suffix from :func:`secure_arguments`, empty for
    SecurityPolicy None. Only the Python servers take all four flags. The
    shared C server under common/servers/ takes the policy name alone —
    ``--security`` — and reads its key material from
    O6_BENCHMARK_CERTIFICATE/O6_BENCHMARK_PRIVATE_KEY in the environment,
    trusting whatever certificate the client presents. Leaving the flag off
    starts it on a plaintext endpoint, which no encrypted client can reach.
    The optional server-only SDKs (:mod:`common.sdk_workers`) take the same
    four flags as the Python servers; ``memory_bytes`` sizes the Milo heap.
    """
    # Every server exposes the same array variables, and every client resolves
    # NodeIds against the same list, so a size means one node on both ends.
    arrays = ["--array-sizes", ",".join(str(size) for size in array_sizes)] if array_sizes else []
    if implementation == "open62541":
        policy = ["--security", security] if secure else []
        return [str(c_server)] + policy + arrays
    if implementation == "o6-python":
        return [sys.executable, str(O6_SERVER)] + secure + arrays
    if implementation == "asyncua":
        return [sys.executable, str(ASYNCUA_SERVER)] + secure + arrays
    if implementation == "node-opcua":
        return node_workers.command("server", "throughput") + secure + arrays
    if implementation == "ua-dotnet":
        return dotnet_workers.command("server", "throughput") + secure + arrays
    if implementation in sdk_workers.SDKS:
        return sdk_workers.command(implementation, memory_bytes) + secure + arrays
    raise ValueError(f"unknown server implementation: {implementation!r}")


def client_command(implementation: str, c_client: Path, secure: list[str]) -> list[str]:
    """The command prefix for one implementation's client.

    The per-sample arguments (iterations, payload shape, …) are appended by
    :func:`run_sample`; the C client takes its key material from the
    environment, so ``secure`` only applies to the Python clients.
    """
    if implementation == "open62541":
        return [str(c_client)]
    if implementation == "o6-python":
        return [sys.executable, str(O6_CLIENT)] + secure
    if implementation == "asyncua":
        return [sys.executable, str(ASYNCUA_CLIENT)] + secure
    if implementation == "node-opcua":
        return node_workers.command("client", "throughput") + secure
    if implementation == "ua-dotnet":
        return dotnet_workers.command("client", "throughput") + secure
    raise ValueError(f"unknown client implementation: {implementation!r}")


def _distribution_version(name: str) -> str | None:
    """The installed version of ``name``, or None if it is not installed."""
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


# --- Library surface, driven by bench.throughput ----------------------------


def cmd_new(args: argparse.Namespace) -> int:
    """Seed the per-suite config rows in ``args.database`` from OPTIONS.

    Asks before overwriting a ``config`` table whose rows already match
    every declared option — narrowing the matrix by hand is real work to
    redo. The result tables are untouched either way; when the database
    already exists, the prompt names what is recorded in it so seeding
    over a populated store does not read as losing those measurements.

    Fields with ``default is None`` (``samples``, written by the runner)
    are not seeded: a default here would only ever be a guess every
    ``sample`` run overwrites.
    """
    database = Path(args.database)
    db = BenchDB(database, suite=SUITE_NAME)

    existing_rows = {row[0] for row in db._connection.execute("SELECT option FROM config WHERE suite = ?", (SUITE_NAME,))}
    expected = {name for name, option in OPTIONS.items() if option.default is not None}
    if expected and existing_rows and existing_rows >= expected:
        result_rows = db.results
        samples = sum(len(entry.get("runs") or []) for entry in result_rows.values())
        held = (
            f"{database} holds {len(result_rows)} measured configuration(s) covering {samples} sample(s)."
            if result_rows
            else f"{database} holds no results yet."
        )
        print(
            f"{database} already has {len(existing_rows)} configured option(s) for suite {SUITE_NAME!r}. {held}",
            file=sys.stderr,
        )
        if not confirm("Overwrite them with the default values?"):
            print(
                "Left unchanged. Use 'python -m bench.throughput config <db> <name> <value>' " "to edit individual options.",
                file=sys.stderr,
            )
            return 1

    seeded = 0
    for name, option in OPTIONS.items():
        if option.default is None:
            continue
        db.set_config(SUITE_NAME, name, json.dumps(option.default))
        seeded += 1
    print(f"Wrote {seeded} option(s) to {database}", file=sys.stderr)
    return 0


def cmd_sample(args: argparse.Namespace) -> int:
    """Walk the matrix stored in ``args.database``'s ``config`` table and (re)measure it."""
    store = BenchDB(args.database, suite=SUITE_NAME)
    uniform, varying = store.get_config(SUITE_NAME)
    if not uniform and not varying:
        print(
            f"{args.database} has no configured options for suite {SUITE_NAME!r}. "
            f"Run 'python -m bench.throughput new {args.database}' first.",
            file=sys.stderr,
        )
        return 1

    pairs = [pair_to_tuple(pair) for pair in varying.get("pair", [])]
    dotnet_roles = ({"client"} if any(c == "ua-dotnet" for c, _ in pairs) else set()) | (
        {"server"} if any(s == "ua-dotnet" for _, s in pairs) else set()
    )
    node_roles = ({"client"} if any(c == "node-opcua" for c, _ in pairs) else set()) | (
        {"server"} if any(s == "node-opcua" for _, s in pairs) else set()
    )
    try:
        node_workers.prepare_metadata(
            store,
            bool(node_roles),
            node_roles,
            "throughput",
            args.amend,
            {
                "max_sessions": max(map(int, varying.get("clients") or [1])) + 16,
                "max_connections": max(map(int, varying.get("clients") or [1])) + 16,
                "max_read_write_operations": max(
                    10000, *(Payload.from_token(p, ()).batch_size for p in varying.get("payload") or ["scalar"])
                ),
            },
        )
        dotnet_workers.prepare_metadata(store, bool(dotnet_roles), dotnet_roles, "throughput", args.amend)
        sdk_workers.prepare_metadata(store, {s for _, s in pairs if s in sdk_workers.SDKS}, "throughput", args.amend)
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 1

    # The store fills in the machine and interpreter itself; the version of
    # the library under test is the one thing it cannot know. It comes from
    # the installed distribution rather than ``o6.__version__`` because this
    # package's own throughput/o6/ directory shadows the SDK module when
    # run.py is executed as a script.
    store.metadata["o6_version"] = _distribution_version("o6")

    # Topping up samples only makes sense if the stored ones were measured
    # here; the aggregates would otherwise mix two machines in one median.
    differences = store.metadata_differences()
    if args.amend and store.results and differences:
        print(
            f"{args.database} was measured in a different environment, so " "--amend would mix incomparable samples:",
            file=sys.stderr,
        )
        for name, (stored, current) in differences.items():
            print(f"  {name}: {stored!r} in the database, {current!r} here", file=sys.stderr)
        print(
            "Re-run without --amend to measure the matrix from scratch.",
            file=sys.stderr,
        )
        return 1

    store.metadata["partial"] = True

    # Collect every configuration up front so we can compute the total
    # sample count for the progress bar and then iterate again to run.
    #
    # ``samples`` is dropped from the key: it says how many times a
    # configuration is measured, not what is measured, so two runs at
    # different sample counts are the same matrix point and must land on the
    # same row. Leaving it in would make --amend re-measure the whole matrix
    # from scratch the first time the count changed.
    config_keys: list[dict] = [
        {name: value for name, value in config_key.items() if name != "samples"} for config_key in walk_matrix(uniform, varying)
    ]

    # --skip-failed drops the failed configurations out of the matrix rather
    # than skipping them inside the loop, so the progress total counts only
    # what will actually be measured and the bar still reaches the end.
    # The 'failures' entries stay in the store: they are the record of what
    # went wrong, and dropping them would hide it.
    if args.skip_failed:
        failed = {
            _freeze({name: value for name, value in entry["config_key"].items() if name != "samples"}, store.key_fields)
            for entry in store.failures
            if isinstance(entry.get("config_key"), dict)
        }
        remaining = [config_key for config_key in config_keys if _freeze(config_key, store.key_fields) not in failed]
        skipped_failed = len(config_keys) - len(remaining)
        config_keys = remaining
        print(
            f":: --skip-failed: leaving {skipped_failed} previously failed configuration(s) "
            f"alone; they stay in 'failures' with the error that was recorded",
            file=sys.stderr,
        )

    # The document's record of what this run asked for. Nothing else stores
    # the target, so a row is short exactly when its 'runs' array is
    # shorter. Written now, ahead of the matrix walk, so the target survives
    # a run interrupted before its first sample lands.
    store.set_config(SUITE_NAME, "samples", json.dumps(args.num_samples))

    def prior_runs(config_key: dict) -> list[dict]:
        if not args.amend:
            return []
        existing = store.results.get(_freeze(config_key, store.key_fields))
        if existing is None:
            return []
        runs = existing.get("runs")
        return list(runs) if isinstance(runs, list) else []

    total_samples = sum(max(0, args.num_samples - len(prior_runs(config_key))) for config_key in config_keys)
    # The right-hand column carries the two rates on a retired line and the
    # phase on the status line above the bar; it is sized for the wider of the
    # two, which is the longest phase this runner reports.
    progress = Progress(
        total_samples,
        unit="sample",
        group="configuration",
        right_width=len("sample 10/10 · collecting 10/10"),
    )
    print(
        ":: median service calls and megabytes per second, per configuration",
        file=sys.stderr,
    )

    c_binary_dir = Path(uniform["c_binary_dir"]).resolve()
    c_server_path = executable(c_binary_dir, "server")
    c_client_path = executable(c_binary_dir, "client")
    # One array set for the whole run: every server exposes it and every client
    # resolves NodeIds against it, so the two ends agree whichever shape a
    # configuration asks for.
    array_sizes = exposed_arrays(varying.get("payload") or [])

    # CPU-set pinning: server on one half of the machine's
    # logical CPUs, clients on the other, so neither party ever preempts
    # the other. ``pin_to_cpus`` returns ``None`` on macOS/Windows and
    # the worker falls through to the unpinned ``child_limits`` path.
    server_cpus, client_cpus = partition_cpus(os.cpu_count() or 1)
    server_preexec_factory = pin_to_cpus(server_cpus)
    client_preexec_factory = pin_to_cpus(client_cpus)

    # The matrix walk is a full cartesian product; the per-suite
    # ``applicable()`` predicate decides which cells produce a result row.
    # Filtered-out cells land on :attr:`BenchDB.skipped`, not on
    # ``results`` or ``failures`` — a skip is "not a measurement we intended
    # to make", distinct from a failure, which says "this should have
    # worked and did not". For ``throughput`` the predicate always returns
    # ``True``, so the skipped list stays empty in this suite's store today.
    config_keys, inapplicable = partition_applicable(config_keys, applicable)
    for config_key in inapplicable:
        label = configuration_label(config_key, array_sizes) if array_sizes else str(config_key)
        store.note_skipped(
            {
                "configuration": label,
                "config_key": dict(config_key),
                "reason": "applicable() returned False",
            }
        )

    if port_in_use():
        print(
            "Something is already listening on 127.0.0.1:4840, where this benchmark's "
            "server goes. It is most likely a server orphaned by a run that was killed "
            "before it could tear one down — stop it and start again, or every "
            "configuration here would be measured against it rather than against the "
            "pairing it names.",
            file=sys.stderr,
        )
        return 1

    needs_certs = "Basic256Sha256" in (varying.get("security") or [])
    if needs_certs:
        # Asked here rather than three hours in, when the matrix reaches its
        # first encrypted configuration and has nothing to present to it.
        try:
            require_certificate_tool()
        except RuntimeError as error:
            print(error, file=sys.stderr)
            return 1

    # The memory limit every worker is started under, from the widest client
    # count the matrix asks for rather than the configuration in hand: the
    # server is started once per (pair, security) and outlives the
    # configurations under it, so the share it is given has to cover the
    # busiest of them. The clients are held to the same share as the server —
    # one client's footprint and the server's were within a tenth of each other
    # on the run this limit exists because of.
    clients_axis = varying.get("clients") or [1]
    widest_client_count = max(int(value) for value in (clients_axis if isinstance(clients_axis, list) else [clients_axis]))
    available_bytes = available_memory_bytes() or 0
    per_process_bytes = worker_address_space(widest_client_count, available_bytes)
    if node_roles:
        store.metadata["node_opcua_diagnostics"] = {
            "process_budget_bytes": per_process_bytes,
            "available_memory_bytes": available_bytes,
            "old_space_limit_mib": min(4096, node_workers.resident_share(per_process_bytes) // (2 * 1024**2)),
            "server_rss_peaks": {},
        }
    if dotnet_roles:
        diagnostics = {
            "process_budget_bytes": per_process_bytes,
            "available_memory_bytes": available_bytes,
        }
        if "server" in dotnet_roles:
            diagnostics["max_queued_request_count"] = max(
                200,
                widest_client_count * (int(uniform["max_outstanding"]) if "async" in varying["mode"] else 1),
            )
        try:
            runtime_env = dotnet_workers.environment(per_process_bytes)
            diagnostics["managed_heap_limit_bytes"] = int(runtime_env.get("DOTNET_GCHeapHardLimit", "0"), 16)
        except RuntimeError as error:
            # The per-cell launch path records this failure and continues the matrix.
            diagnostics["budget_error"] = str(error)
        store.metadata["ua_dotnet_diagnostics"] = diagnostics
    if per_process_bytes:
        working_mb = (per_process_bytes - WORKER_ADDRESS_SPACE_RESERVE_MB * 1024 * 1024) // (1024 * 1024)
        print(
            f":: {available_bytes // (1024 * 1024)} MB free: each of {widest_client_count} client(s) "
            f"and the server is held to {working_mb} MB of it; a configuration that wants more is "
            "recorded as an out-of-memory failure rather than measured",
            file=sys.stderr,
        )

    asyncua_watchdog_note(config_keys)

    server = None
    current_sig: tuple | None = None
    material: dict[str, str] | None = None
    certificate_dir: tempfile.TemporaryDirectory | None = None

    def record_server_memory():
        watch = getattr(server, "node_memory_watch", None)
        if watch is not None:
            peaks = store.metadata["node_opcua_diagnostics"]["server_rss_peaks"]
            key = str(current_sig)
            peaks[key] = max(peaks.get(key, 0), watch.peak)

    try:
        if needs_certs or dotnet_roles or node_roles:
            certificate_dir = tempfile.TemporaryDirectory(prefix="o6-benchmark-certificates-")
            if needs_certs:
                material = certificate_material(Path(certificate_dir.name))

        for config_key in config_keys:
            pair_value = config_key["pair"]
            client, server_impl = pair_to_tuple(pair_value)
            security = config_key["security"]
            sig = (pair_value, security)

            if server is not None and server.poll() is not None:
                record_server_memory()
                # The server did not survive the previous configuration — a
                # memory limit or the OOM killer, most likely. Forgetting the
                # signature restarts it below, which is what keeps the rest of
                # its block measurable; left dead it would fail every remaining
                # configuration that shares it, one timeout at a time.
                print(
                    f":: benchmark server exited with {server.returncode} under the previous " "configuration; restarting it",
                    file=sys.stderr,
                )
                server = None
                current_sig = None

            if sig != current_sig:
                if server is not None:
                    record_server_memory()
                    teardown_server(server)
                    server = None
                encrypted = security == "Basic256Sha256"
                if encrypted:
                    assert material is not None
                    client_secure = secure_arguments("client", material)
                    server_secure = secure_arguments("server", material)
                else:
                    client_secure = []
                    server_secure = []
                server_cmd = server_command(server_impl, c_server_path, server_secure, security, array_sizes, per_process_bytes)
                client_cmd = client_command(client, c_client_path, client_secure)
                server_env = None
                client_env = None
                if encrypted:
                    assert material is not None
                    server_env = os.environ.copy()
                    server_env.update(
                        {
                            "O6_BENCHMARK_SECURITY_POLICY": "Basic256Sha256",
                            "O6_BENCHMARK_CERTIFICATE": material["server_cert"],
                            "O6_BENCHMARK_PRIVATE_KEY": material["server_key"],
                        }
                    )
                    client_env = os.environ.copy()
                    client_env.update(
                        {
                            "O6_BENCHMARK_SECURITY_POLICY": "Basic256Sha256",
                            "O6_BENCHMARK_CERTIFICATE": material["client_cert"],
                            "O6_BENCHMARK_PRIVATE_KEY": material["client_key"],
                        }
                    )
                try:
                    if server_impl == "node-opcua":
                        node_workers.check_memory(1, 1, 1, per_process_bytes, array_sizes)
                        server_cmd += [
                            "--max-clients",
                            str(widest_client_count),
                            "--max-batch",
                            str(max(Payload.from_token(v, array_sizes).batch_size for v in varying["payload"])),
                        ]
                        server_env = {**node_workers.environment(server_env), "O6_BENCHMARK_PKI_ROOT": certificate_dir.name}
                    if client == "node-opcua":
                        client_env = {**node_workers.environment(client_env), "O6_BENCHMARK_PKI_ROOT": certificate_dir.name}
                    if server_impl == "ua-dotnet":
                        server_cmd += [
                            "--max-queued-requests",
                            str(store.metadata["ua_dotnet_diagnostics"]["max_queued_request_count"]),
                        ]
                        server_env = {
                            **dotnet_workers.environment(per_process_bytes, server_env),
                            "O6_BENCHMARK_PKI_ROOT": certificate_dir.name,
                        }
                    if client == "ua-dotnet":
                        client_env = {
                            **dotnet_workers.environment(per_process_bytes, client_env),
                            "O6_BENCHMARK_PKI_ROOT": certificate_dir.name,
                        }
                    server_limit = per_process_bytes
                    if server_impl == "ua-dotnet":
                        server_limit = 0
                    elif server_impl in sdk_workers.SDKS:
                        server_env = sdk_workers.environment(server_impl, per_process_bytes, server_env)
                        server_limit = sdk_workers.address_space_limit(server_impl, per_process_bytes)
                    server = start_server(
                        server_cmd,
                        SERVER_READY,
                        server_env,
                        server_limit,
                        server_preexec_factory,
                    )
                    if server_impl == "node-opcua":
                        store.metadata["node_opcua_diagnostics"]["effective_limits"] = server.node_effective_limits
                except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                    # The server this configuration needs would not come up.
                    # Record it and move on: the next configuration in the
                    # block tries again, and a matrix that takes hours is not
                    # thrown away for one server that would not start.
                    label = configuration_label(config_key, array_sizes)
                    # Same reasoning as the sample loop's failure path: the
                    # samples --amend was told to keep are still this run's
                    # result and stay, but a row nothing backs is an earlier
                    # run's and would read as this one's.
                    kept = prior_runs(config_key)
                    with store.transaction():
                        if not kept:
                            store.drop_result(config_key)
                        store.note_failure(
                            failure_entry(
                                label=label,
                                config_key=config_key,
                                samples_recorded=len(kept),
                                samples_requested=args.num_samples,
                                error=f"benchmark server did not start: {error}",
                            )
                        )
                    progress.advance(left=f"{label} FAILED (no server)", right="see failures", steps=0)
                    server = None
                    current_sig = None
                    continue
                current_sig = sig

            run_one_configuration(
                config_key,
                store,
                args.num_samples,
                prior_runs(config_key),
                client_cmd,
                client_env,
                per_process_bytes,
                array_sizes,
                progress,
                client_preexec_factory,
            )
    finally:
        if server is not None:
            record_server_memory()
            teardown_server(server)
        if certificate_dir is not None:
            certificate_dir.cleanup()

    store.metadata["partial"] = False
    store.save_metadata(progress=True)
    total_runs = sum(len(entry.get("runs") or []) for entry in store.results.values())
    print(
        f"Wrote {len(store.results)} result(s) covering {total_runs} sample(s) " f"to {args.database}",
        file=sys.stderr,
    )
    progress.finish()
    incomplete = [entry for entry in store.results.values() if len(entry.get("runs") or []) < args.num_samples]
    if incomplete:
        print(
            f"{len(incomplete)} configuration(s) are short of {args.num_samples} "
            "sample(s); their aggregates are computed from what was recorded. "
            "A row is short when its 'runs' array is shorter than "
            "'uniform.samples'. Re-run with --amend to top them up.",
            file=sys.stderr,
        )
    if store.failures:
        retry = (
            "They were left alone by --skip-failed; drop the flag to retry them."
            if args.skip_failed
            else "Re-run with --amend to retry only those, or add --skip-failed to leave them alone."
        )
        print(summarise_failures(store.failures, retry), file=sys.stderr)
    return 0
