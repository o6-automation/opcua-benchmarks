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

"""Build identity for the shared native servers and measurement clients."""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BUILDS = {
    "throughput": ROOT / "throughput/open62541/build",
    "server_limits": ROOT / "server_limits/open62541/build",
    "server_capacity": ROOT / "server_capacity/native/build",
    "subscription": ROOT / "subscription/native/build",
}


def binary_directory(profile, value=None):
    """Resolve the built-in worker path in its owning checkout; preserve custom paths."""
    default = BUILDS[profile]
    if value is None or Path(value) == default.relative_to(ROOT):
        return default
    return Path(value).resolve()


def provenance(profile, build=None):
    build = binary_directory(profile, build)
    identity = json.loads((build / "provenance.json").read_text())
    identity["artifacts"] = {
        name: hashlib.sha256((build / name).read_bytes()).hexdigest()
        for name in ("server", "hammer_client" if profile == "server_limits" else "client")
    }
    header = build / "open62541/src_generated/open62541/config.h"
    identity["config_sha256"] = hashlib.sha256(header.read_bytes()).hexdigest()
    identity["cmake_options"] = {
        key.split(":", 1)[0]: value
        for line in (build / "CMakeCache.txt").read_text().splitlines()
        if line.startswith(("UA_", "CMAKE_C_FLAGS", "CMAKE_INTERPROCEDURAL_OPTIMIZATION")) and ":INTERNAL=" not in line
        for key, value in [line.split("=", 1)]
    }
    return identity


def prepare_metadata(store, profile, amend, *, build=None, selected=True):
    """Keep samples from different native builds out of the same named run."""
    if not selected:
        for key in ("open62541", "open62541_version"):
            if key in store.stored_metadata:
                store.metadata[key] = store.stored_metadata[key]
        return
    build = binary_directory(profile, build)
    try:
        current = provenance(profile, build)
    except (OSError, ValueError, KeyError) as error:
        raise RuntimeError(f"Native worker provenance missing or invalid in {build}; run python -m bench.build.") from error
    rows = list(store.results.values())
    if profile == "throughput":
        rows = [row for row in rows if "open62541" in row.get("config_key", {}).get("pair", "").split(":")]
    if amend and rows and store.stored_metadata.get("open62541") != current:
        raise RuntimeError("Native open62541 workers changed; use a new named run.")
    store.metadata["open62541"] = current
    store.metadata["open62541_version"] = current["sdk"]
