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

"""BENCH_NATIVE_LIVE=1 python -m pytest tests/test_native_live.py -q"""

import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import socket
import subprocess
import time

import pytest

from common import open62541
from common.bench_db import BenchDB
from common.workers import read_until, teardown_server, wait_for_server
from subscription import run as subscription

pytestmark = pytest.mark.skipif(os.environ.get("BENCH_NATIVE_LIVE") != "1", reason="opt-in native loopback checks")


def test_subscription_sdk_clocks_match_recorder(tmp_path):
    binary = tmp_path / "clock_check"
    subprocess.run(
        [
            os.environ.get("CC", "cc"),
            "-std=c99",
            "-D_GNU_SOURCE",
            f"-I{open62541.ROOT / 'deps/open62541/include'}",
            f"-I{open62541.ROOT / 'deps/open62541/plugins/include'}",
            f"-I{subscription.BUILD / 'open62541/src_generated'}",
            str(Path(__file__).with_name("test_subscription_clock.c")),
            str(subscription.BUILD / "bin/libopen62541.a"),
            "-lpthread",
            "-lm",
            "-lrt",
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run([str(binary)], check=True, capture_output=True, timeout=10)


def test_native_throughput_matrix_and_recorded_build(tmp_path):
    from bench import throughput

    database = tmp_path / "throughput.db"
    assert throughput.main(["new", str(database)]) == 0
    settings = dict(
        pair=["open62541:open62541"],
        operation=["read", "write"],
        mode=["sync", "async"],
        payload=["scalar", "batch:10", "array:100"],
        security=["None", "Basic256Sha256"],
        clients=[2],
        iterations=100,
        warmup=10,
        max_outstanding=8,
    )
    with closing(BenchDB(database, "throughput")) as store, store.transaction():
        for name, value in settings.items():
            store.set_config("throughput", name, json.dumps(value))
    assert throughput.main(["sample", "1", str(database), "--name", "smoke"]) == 0
    with closing(BenchDB(database, "throughput", name="smoke")) as store:
        assert not store.failures
        assert len(store.results) == 24
        assert all(len(row["runs"]) == 1 and row["runs"][0]["operations"] == 200 for row in store.results.values())
        assert store.stored_metadata["open62541"] == open62541.provenance("throughput")


def test_native_server_limits():
    from server_limits import run

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    build = open62541.BUILDS["server_limits"]
    server = subprocess.Popen(
        [str(build / "server"), "--port", str(port)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    try:
        read_until(server, "benchmark server ready", "server", timeout_seconds=10)
        wait_for_server(server, port=port)
        result = run.run_step(build / "hammer_client", f"opc.tcp://127.0.0.1:{port}", 2, 8, 1, 1, 1, 500)
        assert result["succeeded"] > 0
        assert result["failed"] == 0
    finally:
        teardown_server(server)


def test_native_server_capacity():
    from server_capacity import run

    deadline = time.monotonic() + 30
    with run.running_server("open62541", deadline) as server:
        result = run.measure(
            dict(clients=2, outstanding=8), dict(duration_ms=250, warmup_ms=100, timeout_ms=500, grace_ms=500), server, deadline
        )
    assert result["requests_per_second"] > 0
    assert result["errors"] == 0
    assert run.fingerprint()["open62541"] == open62541.provenance("server_capacity")


@pytest.mark.parametrize("side", ["server", "client"])
def test_native_subscriptions(side):
    _, duration = subscription.measurement_window(1, 100)
    result = asyncio.run(
        subscription.measure(
            dict(
                implementation="open62541",
                measurement=side,
                items=4,
                sampling_ms=1,
                publishing_ms=100,
                min_publishes=5,
                duration_ms=duration,
            )
        )
    )
    assert result["summary"]["observed_min"] > 0
    assert result["server"]["sdk_clock"] == "CLOCK_MONOTONIC"
    assert result["setup"]["sdk_clock"] == "CLOCK_MONOTONIC"
