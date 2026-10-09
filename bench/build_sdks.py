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

"""Install pinned toolchains and build the optional Milo, S2OPC and gopcua servers."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from common import sdk_workers as workers

ROOT = workers.ROOT
DOWNLOADS = ROOT / "deps/downloads"


def run(command: list[str], *, cwd: Path, env: dict[str, str], verbose: bool) -> None:
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=not verbose, text=True)
    if result.returncode:
        raise RuntimeError(f"{' '.join(command)} failed:\n{result.stdout or ''}{result.stderr or ''}")


def fetch(name: str, archive: str) -> Path:
    """Download one pinned archive once and verify it against its pin."""
    pin = workers.PINS[name]["archives"][archive]
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    target = DOWNLOADS / pin["url"].rsplit("/", 1)[1]
    if not target.is_file() or workers.file_hash(target) != pin["sha256"]:
        partial = target.with_suffix(target.suffix + ".part")
        with urllib.request.urlopen(pin["url"], timeout=300) as response, partial.open("wb") as stream:
            shutil.copyfileobj(response, stream)
        if workers.file_hash(partial) != pin["sha256"]:
            partial.unlink()
            raise RuntimeError(f"{pin['url']} does not match its pinned SHA-256")
        partial.replace(target)
    return target


def unpack(name: str, archive: str) -> Path:
    """Extract a pinned tarball into its ``into`` folder, stripping the top directory."""
    pin = workers.PINS[name]["archives"][archive]
    destination = ROOT / pin["into"]
    stamp = destination / ".o6-sha256"
    if stamp.is_file() and stamp.read_text().strip() == pin["sha256"]:
        return destination
    source = fetch(name, archive)
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
        with tarfile.open(source) as bundle:
            bundle.extractall(tmp, filter="tar")
        (top,) = Path(tmp).iterdir()
        top.rename(destination)
    stamp.write_text(pin["sha256"] + "\n")
    return destination


def build_milo(verbose: bool) -> None:
    jdk = unpack("milo", "jdk")
    maven = unpack("milo", "maven")
    env = workers.environment("milo")
    env.update(JAVA_HOME=str(jdk), PATH=f"{jdk / 'bin'}{os.pathsep}{env.get('PATH', '')}")
    source = workers.SDKS["milo"].source
    run(
        [str(maven / "bin/mvn"), "-B", "-q", f"-Dmaven.repo.local={workers.STATE / 'm2'}", "clean", "package"],
        cwd=source,
        env=env,
        verbose=verbose,
    )
    output = workers.SDKS["milo"].output
    shutil.copy2(source / "target/server.jar", output / "server.jar")
    shutil.copytree(source / "target/lib", output / "lib")


def build_gopcua(verbose: bool) -> None:
    go = unpack("gopcua", "go")
    env = workers.environment("gopcua")
    env.update(
        GOROOT=str(go),
        GOTOOLCHAIN="local",
        GOPATH=str(workers.STATE / "go"),
        GOMODCACHE=str(workers.STATE / "go/mod"),
        GOCACHE=str(workers.STATE / "go/cache"),
        GOFLAGS="-mod=readonly",
        CGO_ENABLED="0",
    )
    source = workers.SDKS["gopcua"].source
    output = workers.SDKS["gopcua"].output
    run([str(go / "bin/go"), "build", "-trimpath", "-o", str(output / "server"), "."], cwd=source, env=env, verbose=verbose)


def cmake_install(source: Path, build: Path, options: list[str], verbose: bool) -> None:
    env = os.environ.copy()
    run(
        ["cmake", "-S", str(source), "-B", str(build), "-DCMAKE_BUILD_TYPE=Release",
         f"-DCMAKE_INSTALL_PREFIX={workers.S2OPC_PREFIX}", f"-DCMAKE_PREFIX_PATH={workers.S2OPC_PREFIX}", *options],
        cwd=ROOT, env=env, verbose=verbose,
    )
    run(["cmake", "--build", str(build), "-j", str(os.cpu_count() or 1), "--target", "install"], cwd=ROOT, env=env, verbose=verbose)


# S2OPC sizes its tables at compile time. These are the documented knobs
# (sopc_toolkit_config_constants.h, sopc_common_constants.h), raised to what
# the suites ask of every server: 32+ concurrent sessions, 10,000-node
# batches, and 4k-frame arrays in 64 KiB chunks. Everything else is stock.
S2OPC_LIMITS = {
    "SOPC_MAX_SESSIONS": 128,
    # Must exceed the session count (sopc_config_constants_check.h).
    "SOPC_MAX_SECURE_CONNECTIONS": 129,
    "SOPC_MAX_SOCKETS": 300,
    "SOPC_MAX_SOCKETS_CONNECTIONS": 150,
    "SOPC_MAX_OPERATIONS_PER_MSG": 100000,
    "SOPC_DEFAULT_RECEIVE_MAX_NB_CHUNKS": 1024,
    "SOPC_DEFAULT_SEND_MAX_NB_CHUNKS": 1024,
    "SOPC_DEFAULT_MAX_ARRAY_LENGTH": 1 << 24,
}


def build_s2opc(verbose: bool) -> None:
    sources = {archive: unpack("s2opc", archive) for archive in ("mbedtls", "expat", "s2opc")}
    scratch = ROOT / "deps/s2opc-build"
    stamp = workers.S2OPC_PREFIX / ".o6-inputs"
    toolkit = json.dumps([workers.PINS["s2opc"], S2OPC_LIMITS], sort_keys=True)
    if not stamp.is_file() or stamp.read_text() != toolkit:
        shutil.rmtree(workers.S2OPC_PREFIX, ignore_errors=True)
        shutil.rmtree(scratch, ignore_errors=True)
        common = ["-DCMAKE_POSITION_INDEPENDENT_CODE=ON", "-DBUILD_SHARED_LIBS=OFF"]
        cmake_install(sources["mbedtls"], scratch / "mbedtls",
                      common + ["-DENABLE_TESTING=OFF", "-DENABLE_PROGRAMS=OFF"], verbose)
        cmake_install(sources["expat"], scratch / "expat",
                      common + ["-DEXPAT_BUILD_TESTS=OFF", "-DEXPAT_BUILD_TOOLS=OFF", "-DEXPAT_BUILD_EXAMPLES=OFF",
                                "-DEXPAT_BUILD_DOCS=OFF", "-DEXPAT_SHARED_LIBS=OFF"], verbose)
        defines = " ".join(f"-D{key}={value}" for key, value in S2OPC_LIMITS.items())
        cmake_install(sources["s2opc"], scratch / "s2opc",
                      common + ["-DENABLE_TESTING=OFF", "-DENABLE_SAMPLES=OFF", "-DS2OPC_CLIENTSERVER_ONLY=ON",
                                "-DWARNINGS_AS_ERRORS=OFF", f"-DCMAKE_C_FLAGS={defines}"], verbose)
        stamp.write_text(toolkit)
    source = workers.SDKS["s2opc"].source
    output = workers.SDKS["s2opc"].output
    cmake = scratch / "server"
    shutil.rmtree(cmake, ignore_errors=True)
    run(["cmake", "-S", str(source), "-B", str(cmake), "-DCMAKE_BUILD_TYPE=Release",
         f"-DCMAKE_PREFIX_PATH={workers.S2OPC_PREFIX}", f"-DO6_REPOSITORY_ROOT={ROOT}",
         f"-DCMAKE_C_FLAGS={' '.join(f'-D{k}={v}' for k, v in S2OPC_LIMITS.items())}"],
        cwd=ROOT, env=os.environ.copy(), verbose=verbose)
    run(["cmake", "--build", str(cmake), "-j", str(os.cpu_count() or 1)], cwd=ROOT, env=os.environ.copy(), verbose=verbose)
    shutil.copy2(cmake / "server", output / "server")
    nodeset = sources["s2opc"] / "samples/ClientServer/data/address_space/s2opc_base_nodeset_origin.xml"
    shutil.copy2(nodeset, output / "base_nodeset.xml")
    shutil.rmtree(cmake, ignore_errors=True)


BUILDERS = {
    "milo": build_milo,
    "s2opc": build_s2opc,
    "gopcua": build_gopcua,
}


def build_one(name: str, *, verbose: bool, force: bool) -> bool:
    sdk = workers.SDKS[name]
    marker = sdk.output / "provenance.json"
    try:
        if platform.system() != "Linux" or platform.machine() != "x86_64":
            raise RuntimeError("the pinned SDK toolchains are for Linux x86_64")
        if not force:
            try:
                workers.preflight(name, "build")
                print(f"{sdk.label}: up to date.")
                return True
            except RuntimeError:
                pass
        print(f"Building {sdk.label}...")
        inputs = workers.input_fingerprint(name)
        shutil.rmtree(sdk.output, ignore_errors=True)
        sdk.output.mkdir(parents=True)
        BUILDERS[name](verbose)
        if inputs != workers.input_fingerprint(name):
            raise RuntimeError("sources changed during the build")
        manifest = {
            "inputs": inputs,
            "sdk": workers.PINS[name]["sdk"],
            "runtime": workers.PINS[name]["runtime"],
            "runtime_info": workers.runtime_info(name),
            "artifacts": workers.artifacts(name),
        }
        if name == "s2opc":
            manifest["compile_limits"] = S2OPC_LIMITS
        temporary = marker.with_suffix(".tmp")
        temporary.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        temporary.replace(marker)
        print(f"Built {sdk.label}.")
        return True
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        marker.unlink(missing_ok=True)
        print(f"{sdk.label} setup failed: {error}")
        return False


def build(names: list[str], *, verbose: bool, force: bool) -> list[bool]:
    return [build_one(name, verbose=verbose, force=force) for name in names]
