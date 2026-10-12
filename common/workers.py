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

"""Worker process lifecycle, readiness checks, and CPU affinity."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
import queue
import threading
from collections.abc import Callable, Iterable
from pathlib import Path

# Where an OPC UA server listens unless it is told otherwise, and so where
# every runner in this repository puts the one it starts.
DEFAULT_PORT = 4840


def pin_to_cpus(cpu_ids: Iterable[int], *, strict: bool = False) -> Callable[[], None] | None:
    """Return a pre-exec CPU-affinity callback; strict mode propagates pinning failures."""
    cpu_set = frozenset(int(cpu) for cpu in cpu_ids)
    if not cpu_set:
        return None
    if not hasattr(os, "sched_setaffinity"):
        return None

    def pin() -> None:  # runs in the forked child, between fork and exec
        try:
            os.sched_setaffinity(0, cpu_set)
        except OSError:
            if strict:
                raise
            # The set may be empty or contain CPUs the kernel does not
            # know about on this machine — best-effort, just like the
            # oom_score_adj write in :mod:`common.oom`.
            pass

    return pin


def partition_cpus(total: int, *, single_core: bool = False) -> tuple[set[int], set[int]]:
    """Split CPUs equally, or reserve one allowed server CPU and exclude its SMT siblings from clients."""
    if single_core:
        if not hasattr(os, "sched_getaffinity"):
            raise RuntimeError("Single-core CPU allocation requires Linux CPU affinity support")
        allowed = set(os.sched_getaffinity(0))
        server_cpu = min(allowed)
        topology = Path(f"/sys/devices/system/cpu/cpu{server_cpu}/topology/thread_siblings_list")
        siblings = set()
        for group in topology.read_text().strip().split(","):
            first, separator, last = group.partition("-")
            siblings.update(range(int(first), int(last) + 1) if separator else (int(first),))
        clients = allowed - siblings - {server_cpu}
        if not clients:
            raise RuntimeError("Single-core CPU allocation requires at least two available physical cores")
        return {server_cpu}, clients
    if total <= 0:
        return set(), set()
    server_count = (total + 1) // 2
    server_cpus = set(range(server_count))
    client_cpus = set(range(server_count, total))
    return server_cpus, client_cpus


def compose_preexec(*fns: Callable[[], None] | None) -> Callable[[], None] | None:
    """Combine ``preexec_fn``-shaped callables into one.

    Both :func:`pin_to_cpus` and :func:`common.oom.child_limits` follow
    the ``Callable[[], None] | None`` convention so they can sit in
    ``subprocess.Popen``'s ``preexec_fn`` slot individually. A worker
    that wants both — pinning *and* the OOM-score adjustment — needs a
    single callable that runs both, which is what this helper produces.
    ``None`` arguments are skipped (the platform may have only one of
    the two); if every input is ``None`` the result is also ``None`` so
    the worker gets "no preexec_fn" rather than a do-nothing function.
    """
    active = [fn for fn in fns if fn is not None]
    if not active:
        return None

    def combined() -> None:  # runs in the forked child, between fork and exec
        for fn in active:
            fn()

    return combined


def read_until(
    process: subprocess.Popen[str],
    marker: str,
    label: str,
    timeout_seconds: float = 120.0,
    matches: Callable[[str], bool] | None = None,
) -> list[str]:
    """Read through a readiness marker, bounding silent or partial-line startup hangs."""
    captured: list[str] = []
    outcome: queue.Queue[Exception | None] = queue.Queue(maxsize=1)

    def read() -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                captured.append(line)
                if (matches(line) if matches is not None else marker in line):
                    outcome.put(None)
                    return
            outcome.put(RuntimeError(f"{label} exited before {marker!r}:\n{''.join(captured)}"))
        except Exception as error:
            outcome.put(error)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        error = outcome.get(timeout=timeout_seconds)
    except queue.Empty:
        # The caller owns teardown; killing unblocks the reader even if the
        # child stopped in the middle of writing a line.
        process.kill()
        reader.join(timeout=5)
        raise RuntimeError(f"{label} timed out waiting for {marker!r}:\n{''.join(captured)}") from None
    if error is not None:
        raise error
    reader.join()
    return captured


def drain(stream: object) -> None:
    """Read a stream to exhaustion and discard it.

    Run on a thread for a process nobody is reading any more: a server that
    fills its pipe buffer blocks in the middle of being measured.
    """
    for _ in stream:  # type: ignore[union-attr]
        pass


def port_in_use(host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> bool:
    """Whether something is already listening where this run's server goes.

    Worth asking before a run rather than after it: :func:`wait_for_server`
    takes a connection on this port as its server being up, so a stranger
    holding it is not an error there — it is a whole matrix measured against
    the wrong program. The realistic stranger is one of these benchmarks' own
    servers, orphaned when an earlier run was killed abruptly enough to skip
    its teardown.
    """
    try:
        with socket.create_connection((host, port), 0.25):
            return True
    except OSError:
        return False


def wait_for_server(
    process: subprocess.Popen[str],
    timeout_seconds: float = 10.0,
    port: int = DEFAULT_PORT,
    label: str = "benchmark server",
) -> None:
    """Wait until ``process`` accepts a connection on ``port``.

    ``timeout_seconds`` is how long that may take before it counts as a hang,
    and it belongs to the caller: an instrumented server spends a while
    importing itself before it ever binds, which is slow rather than stuck.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{label} exited before accepting clients")
        try:
            with socket.create_connection(("127.0.0.1", port), 0.1):
                return
        except OSError:
            time.sleep(0.01)
    raise RuntimeError(f"{label} did not accept clients within {timeout_seconds:.0f} seconds")


def teardown_server(server: subprocess.Popen[str], timeout_seconds: float = 10.0) -> None:
    """Stop the running server, cleanly if it will go cleanly.

    A SIGINT first, so the server can log its shutdown and finish whatever it
    writes on the way out; SIGKILL only if it has not gone within
    ``timeout_seconds``. How long that is worth waiting depends on what the
    exit produces — a profile written as the process ends is worth waiting
    minutes for, a log line is not — so it is the caller's to choose.
    """
    watch = getattr(server, "node_memory_watch", None)
    if watch is not None:
        watch.close()
    if server.poll() is not None:
        return
    if os.name == "nt":
        server.terminate()
    else:
        server.send_signal(signal.SIGINT)
    try:
        server.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()
