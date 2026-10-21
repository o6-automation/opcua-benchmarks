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

"""Native build identity prevents mixing different workers in a named run."""

from types import SimpleNamespace
import hashlib
import json

import pytest

from common import open62541


@pytest.mark.parametrize("profile", open62541.BUILDS)
def test_default_binaries_belong_to_the_harness_when_embedded(profile, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    relative = open62541.BUILDS[profile].relative_to(open62541.ROOT)
    assert open62541.binary_directory(profile, str(relative)) == open62541.BUILDS[profile]
    assert open62541.binary_directory(profile, f"./{relative}") == open62541.BUILDS[profile]
    assert open62541.binary_directory(profile) == open62541.BUILDS[profile]


def test_custom_worker_directories_are_not_redirected(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert open62541.binary_directory("throughput", "custom/build") == tmp_path / "custom/build"
    assert open62541.binary_directory("throughput", tmp_path / "absolute") == tmp_path / "absolute"


def test_provenance_tracks_binaries_configuration_and_options(tmp_path):
    (tmp_path / "provenance.json").write_text(json.dumps({"source_commit": "revision", "sdk": "version"}))
    (tmp_path / "CMakeCache.txt").write_text("UA_LOGLEVEL:STRING=600\nUA_HIDDEN:INTERNAL=ignored\nCMAKE_C_FLAGS:STRING=-O3\n")
    generated = tmp_path / "open62541/src_generated/open62541"
    generated.mkdir(parents=True)
    (generated / "config.h").write_text("#define UA_LOGLEVEL 600\n")
    for name in ("server", "client"):
        (tmp_path / name).write_bytes(name.encode())
    before = open62541.provenance("throughput", tmp_path)
    (tmp_path / "client").write_bytes(b"rebuilt-client")
    after = open62541.provenance("throughput", tmp_path)
    assert before["cmake_options"] == {"UA_LOGLEVEL": "600", "CMAKE_C_FLAGS": "-O3"}
    assert before["artifacts"]["client"] != after["artifacts"]["client"]
    assert before["artifacts"]["server"] == after["artifacts"]["server"]
    assert before["config_sha256"] == hashlib.sha256((generated / "config.h").read_bytes()).hexdigest()


@pytest.mark.parametrize("profile", open62541.BUILDS)
@pytest.mark.parametrize("saved", [None, {"source_commit": "old"}])
def test_amend_rejects_changed_or_unrecorded_native_workers(monkeypatch, profile, saved):
    current = {"source_commit": "new", "sdk": "version", "artifacts": {"client": "new-binary"}}
    monkeypatch.setattr(open62541, "provenance", lambda profile, build: current)
    store = SimpleNamespace(
        results={"case": {"config_key": {"pair": "open62541:asyncua"}}},
        stored_metadata={} if saved is None else {"open62541": saved},
        metadata={},
    )
    with pytest.raises(RuntimeError, match="new named run"):
        open62541.prepare_metadata(store, profile, True)
    store.stored_metadata["open62541"] = current
    open62541.prepare_metadata(store, profile, True)
    assert store.metadata["open62541"] == current


def test_non_native_throughput_does_not_require_a_native_build(tmp_path):
    identity = {"source_commit": "saved"}
    store = SimpleNamespace(stored_metadata={"open62541": identity}, metadata={})
    open62541.prepare_metadata(store, "throughput", True, build=tmp_path, selected=False)
    assert store.metadata["open62541"] == identity


def test_missing_provenance_reports_rebuild_command(tmp_path):
    with pytest.raises(RuntimeError, match="run python -m bench.build"):
        open62541.prepare_metadata(SimpleNamespace(), "throughput", False, build=tmp_path)
