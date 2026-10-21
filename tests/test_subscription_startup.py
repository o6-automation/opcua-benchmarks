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

"""Do not release a subscription client before S2OPC's listener opens."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from subscription import run


def worker(event, returncode=None):
    instance = run.Worker(["counter", "nodeset.xml", "48000", "1", "10", "client"], {}, [0])
    instance.process = SimpleNamespace(
        returncode=returncode, stdout=SimpleNamespace(readline=AsyncMock(return_value=json.dumps(event).encode()))
    )
    return instance


def test_ready_waits_for_asynchronous_listener(monkeypatch):
    writer = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
    connect = AsyncMock(side_effect=[ConnectionRefusedError(), (None, writer)])
    monkeypatch.setattr(run.asyncio, "open_connection", connect)
    event = asyncio.run(worker({"event": "ready", "sdk": "s2opc"}).receive("ready", 1))
    assert connect.await_count == 2
    connect.assert_awaited_with("127.0.0.1", 48000)
    writer.close.assert_called_once()
    writer.wait_closed.assert_awaited_once()
    assert event["listener_ready"] is True


def test_other_workers_do_not_probe_tcp(monkeypatch):
    connect = AsyncMock()
    monkeypatch.setattr(run.asyncio, "open_connection", connect)
    event = {"event": "ready", "sdk": "other"}
    assert asyncio.run(worker(event).receive("ready", 1)) == event
    connect.assert_not_awaited()


def test_listener_uses_remaining_setup_deadline(monkeypatch):
    times = iter([0, 2])
    monkeypatch.setattr(run, "time", SimpleNamespace(monotonic=lambda: next(times)))
    with pytest.raises(TimeoutError, match="did not start listening"):
        asyncio.run(worker({"event": "ready", "sdk": "s2opc"}).receive("ready", 1))


def test_worker_exit_is_not_retried():
    with pytest.raises(RuntimeError, match="exit 2"):
        asyncio.run(worker({"event": "ready", "sdk": "s2opc"}, returncode=2).receive("ready", 1))
