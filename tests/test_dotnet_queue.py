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

"""Throughput admission capacity is part of the measured .NET server identity."""

from types import SimpleNamespace

import pytest

from common import dotnet_workers as dotnet


@pytest.mark.parametrize("clients,depth,expected", [(1, 1, 200), (10, 1, 200), (1, 100, 203), (10, 100, 2003)])
def test_queue_keeps_the_sdk_floor_and_headroom_for_deep_pipelines(clients, depth, expected):
    assert dotnet.throughput_queue_capacity(clients, depth) == expected


def store_with_dotnet_samples(provenance):
    return SimpleNamespace(
        results={"cell": {"config_key": {"pair": "open62541:ua-dotnet"}, "runs": [1]}},
        stored_metadata={"ua_dotnet": provenance},
        metadata={},
    )


@pytest.mark.parametrize("previous", [None, {"max_queued_request_count": 1003}])
def test_amend_rejects_changed_or_unrecorded_queue_capacity(monkeypatch, previous):
    current = {"inputs": "same-worker", "source_version": "2.0.2"}
    monkeypatch.setattr(dotnet, "preflight", lambda *_: dict(current))
    saved = dict(current)
    if previous is not None:
        saved["throughput_capacity"] = previous
    store = store_with_dotnet_samples(saved)
    with pytest.raises(RuntimeError, match="incompatible provenance"):
        dotnet.prepare_metadata(store, True, {"server"}, "throughput", True, {"max_queued_request_count": 2003})


def test_matching_queue_capacity_can_be_amended(monkeypatch):
    capacity = {"max_queued_request_count": 2003}
    identity = {"inputs": "same-worker", "source_version": "2.0.2"}
    monkeypatch.setattr(dotnet, "preflight", lambda *_: dict(identity))
    saved = {**identity, "throughput_capacity": capacity}
    store = store_with_dotnet_samples(saved)
    dotnet.prepare_metadata(store, True, {"server"}, "throughput", True, capacity)
    assert store.metadata["ua_dotnet"] == saved


def test_other_profiles_keep_their_original_provenance(monkeypatch):
    identity = {"inputs": "same-worker", "source_version": "2.0.2"}
    monkeypatch.setattr(dotnet, "preflight", lambda *_: dict(identity))
    store = store_with_dotnet_samples(identity)
    dotnet.prepare_metadata(store, True, {"server"}, "server_limits", True)
    assert store.metadata["ua_dotnet"] == identity
