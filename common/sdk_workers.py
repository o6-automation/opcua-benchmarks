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
#    Copyright 2026 (c) Sterfive (Author: Etienne Rossignon)

"""Optional server-only SDK workers: Eclipse Milo, S2OPC and gopcua.

Each one is a single server program under ``common/<sdk>/`` that exposes the
address space of ``common/servers/open62541_server.c`` and takes the same
flags (``--port``, ``--security``, the certificate triple, ``--array-sizes``).
They are measured against the suites' existing clients; none of them is a
client implementation. Toolchains are pinned in ``common/sdk_toolchains.json``
and installed under ``deps/`` by ``python -m bench.build --sdks``.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PINS = json.loads((ROOT / "common/sdk_toolchains.json").read_text())
BUILD_INSTRUCTION = "Run python3 -m bench.build --sdks (or --milo, --s2opc, --gopcua)."

JAVA = ROOT / "deps/jdk/bin/java"
GO = ROOT / "deps/go/bin/go"
S2OPC_PREFIX = ROOT / "deps/s2opc"
STATE = ROOT / "deps/sdk-state"


@dataclass(frozen=True)
class Sdk:
    name: str
    label: str
    source: Path
    # Whether the runtime manages its own heap and reserves far more address
    # space than it uses (the JVM and the Go runtime): RLIMIT_AS would kill it
    # at startup, so its memory share is passed as a heap limit instead.
    managed_heap: bool

    @property
    def output(self) -> Path:
        return self.source / "build"


SDKS: dict[str, Sdk] = {
    sdk.name: sdk
    for sdk in (
        Sdk("milo", "Eclipse Milo (Java)", ROOT / "common/milo", True),
        Sdk("s2opc", "S2OPC (C)", ROOT / "common/s2opc", False),
        Sdk("gopcua", "gopcua (Go)", ROOT / "common/gopcua", True),
    )
}
NAMES: tuple[str, ...] = tuple(SDKS)
LABELS: dict[str, str] = {name: sdk.label for name, sdk in SDKS.items()}
# Report colours, light and dark, picked from the validated series palette
# slots the five original implementations do not use.
COLORS: dict[str, tuple[str, str]] = {
    "milo": ("#e87ba4", "#d55181"),
    "s2opc": ("#eda100", "#c98500"),
    "gopcua": ("#4a3aa7", "#9085e9"),
}


def _memory_share(memory_bytes: int) -> int:
    from common.oom import WORKER_ADDRESS_SPACE_RESERVE_MB

    return max(0, memory_bytes - WORKER_ADDRESS_SPACE_RESERVE_MB * 1024**2)


def environment(name: str, memory_bytes: int = 0, base: dict[str, str] | None = None) -> dict[str, str]:
    """Remove ambient runtime tuning and point each toolchain at its pinned state."""
    env = (os.environ if base is None else base).copy()
    for key in list(env):
        if key.startswith(("JAVA_", "_JAVA_", "JDK_", "GO")) or key in {"CLASSPATH", "MAVEN_OPTS"}:
            del env[key]
    if name == "gopcua" and memory_bytes:
        # A soft limit: the collector works harder near it rather than failing.
        env["GOMEMLIMIT"] = str(_memory_share(memory_bytes) * 3 // 4)
    return env


def command(name: str, memory_bytes: int = 0) -> list[str]:
    """The server command line, before the per-run flags."""
    sdk = SDKS[name]
    if name == "milo":
        heap = [f"-Xmx{max(256, _memory_share(memory_bytes) * 3 // 4 // 1024**2)}m"] if memory_bytes else []
        return [
            str(JAVA),
            *heap,
            "-Dorg.slf4j.simpleLogger.defaultLogLevel=warn",
            "-cp",
            f"{sdk.output / 'server.jar'}{os.pathsep}{sdk.output / 'lib'}/*",
            "o6.benchmark.BenchmarkServer",
        ]
    if name == "s2opc":
        return [str(sdk.output / "server"), "--nodeset", str(sdk.output / "base_nodeset.xml")]
    return [str(sdk.output / "server")]


def address_space_limit(name: str, memory_bytes: int) -> int:
    """The RLIMIT_AS to apply to a server, 0 where the runtime gets a heap limit instead."""
    return 0 if SDKS[name].managed_heap else memory_bytes


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def input_fingerprint(name: str) -> str:
    """Bind a build to its sources, the build steps and the toolchain pins."""
    sdk = SDKS[name]
    digest = hashlib.sha256(json.dumps(PINS[name], sort_keys=True).encode())
    files = [Path(__file__), ROOT / "bench/build_sdks.py", ROOT / "common/contract.h"]
    files += [
        path
        for path in sdk.source.rglob("*")
        if path.is_file() and not {"build", "target"}.intersection(path.relative_to(sdk.source).parts)
    ]
    for path in sorted(files):
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def artifacts(name: str) -> dict[str, str]:
    output = SDKS[name].output
    return {
        str(path.relative_to(output)): file_hash(path)
        for path in sorted(output.rglob("*"))
        if path.is_file() and path.name != "provenance.json"
    }


def preflight(name: str, profile: str) -> dict:
    """Reject absent, stale, or changed artifacts before starting a matrix."""
    sdk = SDKS[name]
    try:
        manifest = json.loads((sdk.output / "provenance.json").read_text())
        if manifest["inputs"] != input_fingerprint(name):
            raise ValueError("sources, build steps or toolchain pins changed")
        if manifest["artifacts"] != artifacts(name):
            raise ValueError("built artifacts changed")
        executable = Path(command(name)[0])
        if not executable.is_file():
            raise ValueError(f"missing {executable}")
        return {**manifest, "profile": profile}
    except (OSError, ValueError, KeyError) as error:
        raise RuntimeError(f"{sdk.label} server unavailable or stale: {error}. {BUILD_INSTRUCTION}") from error


def uses(config_key: dict, name: str) -> bool:
    return config_key.get("implementation") == name or name in config_key.get("pair", "").split(":")


def prepare_metadata(store, names: set[str], profile: str, amend: bool) -> None:
    """Require equal provenance before appending to existing samples, as for Node and .NET."""
    for name in NAMES:
        key = f"sdk_{name.replace('-', '_')}"
        rows = [row for row in store.results.values() if row.get("runs") and uses(row.get("config_key", {}), name)]
        if name not in names:
            if rows and key in store.stored_metadata:
                store.metadata[key] = store.stored_metadata[key]
            continue
        current = preflight(name, profile)
        if rows and amend and store.stored_metadata.get(key) != current:
            raise RuntimeError(f"Existing {SDKS[name].label} samples have incompatible provenance; use a new named run.")
        store.metadata[key] = current


def runtime_info(name: str) -> dict:
    """Ask the built server who it is, where it can say (Milo and gopcua)."""
    if name not in ("milo", "gopcua"):
        return {}
    output = subprocess.run(
        command(name) + ["--runtime-info"], env=environment(name), capture_output=True, text=True, timeout=60, check=True
    )
    return json.loads(output.stdout)
