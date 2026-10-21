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

"""Run one fixed counter workload with exactly one measurement observer."""

import asyncio
from contextlib import AsyncExitStack
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import sys
import tempfile
import time

from subscription.options import OPTIONS, MAX_ITEMS, bounded, measurement_window
from common.extensions import project_root
from subscription.confidence import estimate

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "subscription/native/build"


def validate(case):
    if set(case) != {"implementation", "measurement", "items", "sampling_ms", "duration_ms", "publishing_ms", "min_publishes"}:
        raise ValueError("Expected exactly the seven counter workload settings")
    for key in ("implementation", "measurement", "sampling_ms", "publishing_ms"):
        OPTIONS[key].coerce(json.dumps([case[key]]))
    OPTIONS["min_publishes"].coerce(json.dumps(case["min_publishes"]))
    _, duration = measurement_window(case["sampling_ms"], case["publishing_ms"], case["min_publishes"])
    if type(case["duration_ms"]) is not int or case["duration_ms"] != duration:
        raise ValueError("duration_ms must equal the automatically derived warm-up plus measurement duration")
    verdict = bounded(1, MAX_ITEMS)(case["items"])
    if verdict is not True:
        raise ValueError(verdict)


def evaluate(case, setup, result):
    """Preserve per-item evidence; missing items never disappear into an average."""
    validate(case)
    if setup.get("queue_size") != 1 or setup.get("publishing_ms") != case["publishing_ms"]:
        raise ValueError("Server revised the queue size or requested publishing interval")
    if setup.get("publishing_enabled") is not True:
        raise ValueError("Wrong publishing mode")
    revisions = setup.get("sampling_ms", [])
    # These SDKs notify on changes rather than periodically sampling callbacks.
    if case["implementation"] in ("asyncua", "gopcua"):
        expected_sampling = case["publishing_ms"]
    elif case["implementation"] == "s2opc":
        expected_sampling = 0  # Exception-based sampling.
    else:
        expected_sampling = case["sampling_ms"]
    if isinstance(revisions, list):
        if len(revisions) != case["items"]:
            raise ValueError("Missing revised sampling intervals")
    else:
        if type(setup.get("items")) is not int or setup["items"] != case["items"]:
            raise ValueError("Missing or inconsistent monitored-item count")
        revisions = [revisions]
    if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in revisions):
        raise ValueError("Invalid revised sampling interval")
    if any(v != expected_sampling for v in revisions):
        raise ValueError("SDK revised the requested sampling interval")
    counts = result.get("counts", [])
    begin, finish = measurement_window(case["sampling_ms"], case["publishing_ms"], case["min_publishes"])
    retained_ms = finish - begin
    expected = retained_ms / case["sampling_ms"]
    maximum = math.ceil(case["duration_ms"] / case["sampling_ms"])
    if len(counts) != case["items"] or any(type(v) is not int or not 0 <= v <= maximum for v in counts):
        raise ValueError("Missing, malformed, or impossible item counters")
    intervals = result.get("interval_counts", [])
    if (
        len(intervals) != retained_ms // case["publishing_ms"]
        or any(type(v) is not int or v < 0 for v in intervals)
        or sum(intervals) != sum(counts)
    ):
        raise ValueError("Missing or inconsistent interval progress")
    if case["measurement"] == "server" and "delivery" in result:
        raise ValueError("Client statistics present in a server-only measurement")
    if case["measurement"] == "client":
        delivery = result.get("delivery", [])
        if isinstance(delivery, dict):
            keys = ("notifications", "items_with_notifications", "intervals_min", "intervals_total", "min_delta", "max_delta")
            if any(type(delivery.get(key)) is not int or delivery[key] < 0 for key in keys):
                raise ValueError("Invalid aggregate delivery counters")
            covered = delivery["items_with_notifications"]
            if not sum(count > 0 for count in counts) <= covered <= case["items"]:
                raise ValueError("Inconsistent client delivery coverage")
            if delivery["notifications"] != delivery["intervals_total"] + covered:
                raise ValueError("Inconsistent aggregate delivery intervals")
            if delivery["intervals_min"] * case["items"] > delivery["intervals_total"]:
                raise ValueError("Invalid minimum delivery intervals")
            if not 0 <= delivery["min_delta"] <= delivery["max_delta"] <= max(counts):
                raise ValueError("Invalid aggregate counter delta")
            gap = delivery.get("max_gap_ms")
            if type(gap) not in (float, int) or not math.isfinite(gap) or gap < 0:
                raise ValueError("Invalid receipt gap")
        else:
            if len(delivery) != case["items"]:
                raise ValueError("Missing client delivery evidence")
            for count, item in zip(counts, delivery):
                for key in ("notifications", "intervals", "min_delta", "max_delta"):
                    if type(item.get(key)) is not int or item[key] < 0:
                        raise ValueError("Invalid delivery counters")
                gap = item.get("max_gap_ms")
                if type(gap) not in (float, int) or not math.isfinite(gap) or gap < 0:
                    raise ValueError("Invalid receipt gap")
                if (count > 0 and item["notifications"] == 0) or item["intervals"] >= max(1, item["notifications"]):
                    raise ValueError("Inconsistent delivery evidence")
                if not 0 <= item["min_delta"] <= item["max_delta"] <= count:
                    raise ValueError("Invalid counter delta")
    return dict(
        expected_per_item=expected,
        warmup_ms=begin,
        measured_duration_ms=retained_ms,
        expected_per_publish=case["publishing_ms"] / case["sampling_ms"],
        observed_min=min(counts),
        observed_max=max(counts),
        progress_fraction_min=min(counts) / expected,
        progress_fraction_mean=sum(counts) / (len(counts) * expected),
        updates_per_second=sum(counts) / (retained_ms / 1000),
        items_without_progress=sum(value == 0 for value in counts),
        confidence=estimate(intervals, case["items"] * case["publishing_ms"] / case["sampling_ms"]),
    )


def commands(case, port):
    args = [str(port), str(case["items"]), str(case["sampling_ms"]), case["measurement"]]
    implementation = case["implementation"]
    env = os.environ.copy()
    if implementation == "open62541":
        server = [str(BUILD / "server")]
    elif implementation in ("o6-python", "asyncua"):
        server = [sys.executable, "-m", "subscription.python_server", implementation]
    elif implementation == "node-opcua":
        from common import node_workers

        server = [str(node_workers.NODE), str(ROOT / "subscription/node_server.mjs")]
        env = node_workers.environment()
    elif implementation.startswith("node-opcua-fronts"):
        from common import node_workers, sdk_workers

        server = [str(node_workers.NODE), str(ROOT / "subscription/node_fronts_server.mjs")]
        env = sdk_workers.environment(implementation)
    elif implementation == "ua-dotnet":
        from common import dotnet_workers

        server = [str(dotnet_workers.DOTNET), str(dotnet_workers.OUTPUT / "server/Benchmark.Server.dll"), "--counter"]
        env = dotnet_workers.environment()
    else:
        from common import sdk_workers

        sdk_workers.preflight(implementation, "subscription")
        server = sdk_workers.counter_command(implementation)
        env = sdk_workers.environment(implementation)
    return server + args, [str(BUILD / "client")] + args + [str(case["publishing_ms"])], env


def affinity():
    """Select two different physical cores, retaining explicit inherited evidence if unavailable."""
    allowed = sorted(os.sched_getaffinity(0))
    cores = {}
    for cpu in allowed:
        path = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
        try:
            key = ((path / "physical_package_id").read_text(), (path / "core_id").read_text())
        except OSError:
            return allowed, allowed
        cores.setdefault(key, cpu)
    if len(cores) < 2:
        return allowed, allowed
    first, second = list(cores.values())[:2]
    return [first], [second]


def fingerprint():
    """Bind samples to suite sources and the actual worker artifacts."""
    files = [
        p
        for p in (ROOT / "subscription").rglob("*")
        if p.is_file()
        and p.suffix in {".py", ".c", ".h", ".cs", ".mjs", ".txt"}
        and not {"build", "tests", "__pycache__"}.intersection(p.parts)
    ]
    files += [
        BUILD / "server",
        BUILD / "client",
        ROOT / "common/dotnet/build/server/Benchmark.Server.dll",
        ROOT / "common/dotnet/build/server/Opc.Ua.Server.dll",
        ROOT / "common/node/package-lock.json",
    ]
    from importlib.util import find_spec

    for module in ("subscription.o6_backend", "subscription.strategy"):
        adapter = find_spec(module)
        if adapter is not None and adapter.origin:
            files.append(Path(adapter.origin))
    # Editable SDK distributions need not list their inplace native extension.
    package_spec = find_spec("o6")
    if package_spec is not None and package_spec.origin:
        files.extend(Path(package_spec.origin).parent.glob("_o6*.so"))
        files.extend(Path(package_spec.origin).parent.glob("_o6*.pyd"))
    digest = hashlib.sha256()
    from common import sdk_workers

    for name in sdk_workers.NAMES:
        digest.update(sdk_workers.input_fingerprint(name).encode())
        digest.update(json.dumps(sdk_workers.artifacts(name), sort_keys=True).encode())
    for path in sorted(files):
        relative = os.path.relpath(path, ROOT)
        digest.update(relative.encode())
        digest.update(path.read_bytes() if path.exists() else b"missing")
    # Read distribution metadata without importing o6 into the long-lived runner.
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        package = distribution("o6")
    except PackageNotFoundError:
        digest.update(b"o6:missing")
    else:
        digest.update(f"o6:{package.version}".encode())
        for entry in sorted(package.files or (), key=str):
            if str(entry).endswith((".so", ".pyd", ".dll", ".dylib")):
                digest.update(str(entry).encode())
                digest.update(package.locate_file(entry).read_bytes())
    return digest.hexdigest()


class Worker:
    def __init__(self, command, env, cpus, *, timeout_scale=1):
        self.command, self.env, self.cpus = command, env, cpus
        self.process = None
        self.shutdown_timeout = 5 * timeout_scale

    async def __aenter__(self):
        self.log = tempfile.TemporaryFile()
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.command,
                cwd=project_root(),
                env=self.env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=self.log,
                limit=32 * 1024 * 1024,
                preexec_fn=lambda: os.sched_setaffinity(0, self.cpus),
            )
        except BaseException:
            self.log.close()
            raise
        return self

    async def send(self, line):
        self.process.stdin.write((line + "\n").encode())
        await self.process.stdin.drain()

    async def receive(self, expected, timeout):
        deadline = time.monotonic() + timeout
        try:
            line = await asyncio.wait_for(self.process.stdout.readline(), timeout)
        except TimeoutError as error:
            # Read without moving the file position shared with the live worker.
            size = os.fstat(self.log.fileno()).st_size
            tail = os.pread(self.log.fileno(), 4000, max(0, size - 4000)).decode(errors="replace")
            raise TimeoutError(
                f"Worker timed out after {timeout:g}s waiting for {expected}: {' '.join(self.command)}"
                + (f"\nWorker stderr: {tail}" if tail else "")
            ) from error
        if not line:
            self.log.seek(0)
            raise RuntimeError(f"Worker exited before {expected}: {self.log.read()[-4000:].decode(errors='replace')}")
        event = json.loads(line)
        if event.get("event") != expected:
            raise RuntimeError(f"Expected {expected}, got {event}")
        if expected == "ready" and event.get("sdk") == "s2opc":
            # S2OPC reports startup before its asynchronous listener opens.
            port = int(self.command[-4])
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"S2OPC endpoint did not start listening on port {port}")
                if self.process.returncode is not None:
                    raise RuntimeError(f"S2OPC exited before listening (exit {self.process.returncode})")
                try:
                    _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), remaining)
                except ConnectionRefusedError:
                    await asyncio.sleep(min(0.01, remaining))
                    continue
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), max(0, deadline - time.monotonic()))
                event["listener_ready"] = True
                break
        return event

    async def __aexit__(self, kind, error, traceback):
        try:
            if self.process.returncode is None:
                try:
                    await self.send("quit")
                    await asyncio.wait_for(self.process.wait(), self.shutdown_timeout)
                except (TimeoutError, BrokenPipeError, ConnectionResetError):
                    if self.process.returncode is None:
                        self.process.kill()
                    await self.process.wait()
            if kind is None and self.process.returncode != 0:
                self.log.seek(0)
                raise RuntimeError(
                    f"Worker cleanup failed ({self.command[0]}, exit {self.process.returncode}): "
                    f"{self.log.read()[-4000:].decode(errors='replace')}"
                )
        finally:
            self.log.close()


async def _wait_with_progress(awaitable, report, phase):
    """Refresh the UI while waiting, without changing worker deadlines or recording."""
    if report is None:
        return await awaitable
    started = time.monotonic_ns()
    pending = asyncio.ensure_future(awaitable)
    try:
        while not pending.done():
            now = time.monotonic_ns()
            report(phase(now) if callable(phase) else f"{phase} {(now - started) / 1e9:.1f}s")
            await asyncio.wait({pending}, timeout=0.5)
        return await pending
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


def _measurement_phase(now, start, measured_start, end):
    if now < start:
        return "waiting for start"
    if now < measured_start:
        return f"warmup {(now - start) / 1e9:.1f}/{(measured_start - start) / 1e9:.1f}s"
    if now < end:
        return f"measuring {(now - measured_start) / 1e9:.1f}/{(end - measured_start) / 1e9:.1f}s"
    return f"collecting results {(now - end) / 1e9:.1f}s"


SETUP_TIMEOUT_SECONDS = 300


class SetupTimeout(TimeoutError):
    """A setup-limited search must not be reported as a measured progress failure."""

    def __init__(self, items, phase, detail=""):
        self.items, self.phase = items, phase
        message = f"{phase} timed out at {items} items (setup limit {SETUP_TIMEOUT_SECONDS}s)"
        super().__init__(message + (f": {detail}" if detail else ""))


async def setup_ready(worker, items, phase, deadline):
    """Server startup and monitored-item creation share one five-minute budget."""
    try:
        return await worker.receive("ready", max(0, deadline - time.monotonic()))
    except TimeoutError as error:
        raise SetupTimeout(items, phase, str(error)) from error


async def measure(case, report=None):
    if report is not None:
        report("checking workers")
    validate(case)
    for role in (("client", "server") if case["implementation"] == "open62541" else ("client",)):
        binary = BUILD / role
        sources = [BUILD.parent / f"{role}.c", BUILD.parent / "worker.h", BUILD.parent / "CMakeLists.txt"]
        if not binary.is_file() or any(path.stat().st_mtime_ns > binary.stat().st_mtime_ns for path in sources):
            raise RuntimeError("Missing or stale counter workers; run python -m bench.build")
    cache = (BUILD / "CMakeCache.txt").read_text()
    if "CMAKE_BUILD_TYPE:STRING=Release\n" not in cache:
        raise RuntimeError("Counter workers require a Release build; run python -m bench.build")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    server_command, client_command, env = commands(case, port)
    server_cpu, client_cpu = affinity()
    timeout_scale = max(1, math.ceil(case["items"] / 65536))
    async with AsyncExitStack() as stack:
        pki = stack.enter_context(tempfile.TemporaryDirectory(prefix="counter-pki-"))
        env["O6_BENCHMARK_PKI_ROOT"] = pki
        setup_deadline = time.monotonic() + SETUP_TIMEOUT_SECONDS
        server = await stack.enter_async_context(Worker(server_command, env, server_cpu, timeout_scale=timeout_scale))
        ready = await _wait_with_progress(
            setup_ready(server, case["items"], "server startup", setup_deadline), report, "server startup (setup limit 300s)"
        )
        client = await stack.enter_async_context(
            Worker(client_command, os.environ.copy(), client_cpu, timeout_scale=timeout_scale)
        )
        setup = await _wait_with_progress(
            setup_ready(client, case["items"], "monitored-item creation", setup_deadline), report, "creating monitored items"
        )
        # Check setup before any workload; an empty recording validates all revisions.
        begin, finish = measurement_window(case["sampling_ms"], case["publishing_ms"], case["min_publishes"])
        empty = dict(counts=[0] * case["items"], interval_counts=[0] * ((finish - begin) // case["publishing_ms"]))
        if case["measurement"] == "client":
            empty["delivery"] = dict(
                notifications=0,
                items_with_notifications=0,
                intervals_min=0,
                intervals_total=0,
                min_delta=0,
                max_delta=0,
                max_gap_ms=0,
            )
        evaluate(case, setup, empty)
        # Establish the subscription and consume initial zero notifications.
        await _wait_with_progress(asyncio.sleep(2), report, "settling subscription")
        if report is not None:
            report("arming workers")
        start = time.monotonic_ns() + 500_000_000
        end = start + case["duration_ms"] * 1_000_000
        begin, finish = measurement_window(case["sampling_ms"], case["publishing_ms"], case["min_publishes"])
        measured_start = start + begin * 1_000_000
        measured_end = start + finish * 1_000_000
        interval_ns = case["publishing_ms"] * 1_000_000
        window = dict(
            start_ns=start, end_ns=end, measured_start_ns=measured_start, measured_end_ns=measured_end, interval_ns=interval_ns
        )
        for worker in (client, server):
            await worker.send(f"arm {start} {end} {measured_start} {measured_end} {interval_ns}")
            ack = await worker.receive("armed", 2)
            if any(int(ack.get(key, 0)) != value for key, value in window.items()) or time.monotonic_ns() >= start:
                raise RuntimeError("Worker missed the common measurement window")
        observer = server if case["measurement"] == "server" else client
        other = client if observer is server else server
        recording, done = await _wait_with_progress(
            asyncio.gather(
                observer.receive("result", case["duration_ms"] / 1000 + 10),
                other.receive("done", case["duration_ms"] / 1000 + 10),
            ),
            report,
            lambda now: _measurement_phase(now, start, measured_start, measured_end),
        )
        if set(done) != {"event"}:
            raise ValueError("Unselected worker returned measurement evidence")
        if report is not None:
            report("evaluating counters")
        summary = evaluate(case, setup, recording)
        if report is not None:
            report("stopping workers")
    return dict(
        schema=8,
        case=case,
        start_ns=start,
        end_ns=end,
        measured_start_ns=measured_start,
        measured_end_ns=measured_end,
        clock="CLOCK_MONOTONIC",
        server=ready,
        setup=setup,
        recording=recording,
        summary=summary,
        affinity=dict(server=server_cpu, client=client_cpu),
        fingerprint=fingerprint(),
    )
