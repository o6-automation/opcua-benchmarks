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

"""Local .NET worker artifacts, runtime environment, and reproducible provenance."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "common/dotnet"
OUTPUT = SOURCE / "build"
DOTNET = ROOT / "deps/dotnet/dotnet"
PINS = json.loads((SOURCE / "toolchain.json").read_text())
BUILD_INSTRUCTION = "Run python3 -m bench.build --dotnet."


def environment(memory_bytes: int = 0, base: dict[str, str] | None = None) -> dict[str, str]:
    """Isolate CLI state and restore cache; reserve 25% of a worker's budget for native memory."""
    env = (os.environ if base is None else base).copy()
    # Ambient tuning must not silently change the experiment.
    for key in list(env):
        if key.startswith(("DOTNET_", "COMPlus_")):
            del env[key]
    env.update(
        DOTNET_ROOT=str(DOTNET.parent),
        DOTNET_CLI_HOME=str(ROOT / "deps/dotnet-state"),
        NUGET_PACKAGES=str(ROOT / "deps/nuget"),
        DOTNET_NOLOGO="1",
        DOTNET_CLI_TELEMETRY_OPTOUT="1",
        DOTNET_MULTILEVEL_LOOKUP="0",
    )
    if memory_bytes:
        from common.oom import WORKER_ADDRESS_SPACE_RESERVE_MB

        share = memory_bytes - WORKER_ADDRESS_SPACE_RESERVE_MB * 1024**2
        if share < 128 * 1024**2:
            raise RuntimeError(".NET worker memory budget is below the 128 MiB runtime allowance")
        env["DOTNET_GCHeapHardLimit"] = format(share * 3 // 4, "x")
    return env


def command(role: str, profile: str) -> list[str]:
    name = {"server": "Benchmark.Server", "client": "Benchmark.Client"}[role]
    return [str(DOTNET), str(OUTPUT / role / f"{name}.dll"), "--profile", profile]


def input_fingerprint() -> str:
    digest = hashlib.sha256()
    files = [ROOT / "bench/build_dotnet.py", Path(__file__)]
    files.append(ROOT / "subscription/DotnetWorker.cs")
    for folder in (SOURCE, ROOT / "throughput/dotnet"):
        files.extend(
            p
            for p in folder.rglob("*")
            if p.is_file()
            and p.suffix.lower() in {".cs", ".csproj", ".props", ".targets", ".json", ".config"}
            and not {"bin", "obj", "build"}.intersection(p.relative_to(folder).parts)
        )
    for path in sorted(files):
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def preflight(roles: set[str], profile: str) -> dict:
    """Reject absent, stale, or incompatible artifacts before starting a matrix."""
    try:
        manifest = json.loads((OUTPUT / "provenance.json").read_text())
        if manifest["inputs"] != input_fingerprint():
            raise ValueError("C# sources or dependency inputs changed")
        for role in roles | {"server"}:
            for name, expected in manifest["artifacts"][role].items():
                if hashlib.sha256((OUTPUT / role / name).read_bytes()).hexdigest() != expected:
                    raise ValueError(f"changed artifact: {role}/{name}")
        runtime = subprocess.run(
            command("server", profile) + ["--runtime-info"],
            env=environment(),
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        info = json.loads(runtime.stdout)
        if info["runtime"] != PINS["runtime"]:
            raise ValueError(f"runtime {info['runtime']} differs from pinned {PINS['runtime']}")
        return {
            **manifest,
            "runtime_info": info,
            "profile": profile,
            "memory_policy": "gc-heap-75%-of-resident-share; no-RLIMIT_AS" if profile == "throughput" else "oom-priority-only",
        }
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        detail = error.stderr if isinstance(error, subprocess.CalledProcessError) else str(error)
        raise RuntimeError(f".NET workers are unavailable or stale: {detail}. {BUILD_INSTRUCTION}") from error


def prepare_metadata(store, selected: bool, roles: set[str], profile: str, amend: bool) -> None:
    """Require complete, equal provenance before appending to existing .NET samples."""
    rows = [
        row
        for row in store.results.values()
        if row.get("runs")
        and (
            row.get("config_key", {}).get("implementation") == "ua-dotnet"
            or "ua-dotnet" in row.get("config_key", {}).get("pair", "").split(":")
        )
    ]
    if not selected:
        if rows and "ua_dotnet" in store.stored_metadata:
            store.metadata["ua_dotnet"] = store.stored_metadata["ua_dotnet"]
        return
    current = preflight(roles, profile)
    stored = store.stored_metadata.get("ua_dotnet")
    if rows and amend and stored != current:
        raise RuntimeError("Existing .NET samples have missing or incompatible provenance; use a new named run.")
    store.metadata["ua_dotnet"] = current


def check_memory(array_size: int, batch_size: int, depth: int, memory_bytes: int) -> None:
    """Reject payloads whose conservative managed working-set estimate exceeds the heap budget."""
    if not memory_bytes:
        return
    heap = int(environment(memory_bytes)["DOTNET_GCHeapHardLimit"], 16)
    estimate = 64 * 1024**2 + depth * (array_size * 4 * 4 + batch_size * 512)
    if estimate > heap:
        raise RuntimeError(f".NET payload ran out of memory budget: estimated {estimate} managed bytes exceeds {heap}")
