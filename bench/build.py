#!/usr/bin/env python3
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

"""Build native workers and optional .NET and Node.js workers."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# (relative path to the suite's CMake project, its CMAKE_BUILD_TYPE,
#  list of executables the build is expected to produce) — matches
# the top-level README's Setup section exactly.
C_PROJECTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("throughput/open62541", "Release", ("client", "server")),
    ("subscription/native", "Release", ("server", "client")),
    ("server_capacity/native", "Release", ("server", "client")),
    ("server_limits/open62541", "Release", ("server", "hammer_client")),
)

def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m bench.build",
        description="Build native benchmark workers. Use --rebuild for a clean build.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="stream the build tool's own output live instead of one line per step",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="force a clean rebuild of every step",
    )
    parser.add_argument(
        "--dotnet", action="store_true", help="also bootstrap the local pinned SDK and build .NET workers in Release"
    )
    parser.add_argument("--node", action="store_true", help="also bootstrap pinned Node.js and the public node-opcua package")
    parser.add_argument("--sdks", action="store_true", help="also build all four optional servers below")
    for name, label in (
        ("milo", "Eclipse Milo (pinned JDK and Maven)"),
        ("s2opc", "S2OPC (pinned mbedtls and expat)"),
        ("gopcua", "gopcua (pinned Go)"),
    ):
        parser.add_argument(f"--{name}", action="store_true", help=f"also build the optional {label} server")
    return parser.parse_args(argv)


def run_commands(
    label: str,
    commands: list[list[str]],
    *,
    cwd: Path | None = None,
    env: dict | None = None,
    verbose: bool = False,
    announce: bool = True,
) -> str | None:
    """Run commands sequentially; return the last captured output, or None on failure.

    Verbose mode streams output. Failures always print their diagnostics."""
    if announce:
        print(f"Building {label}...")
    for command in commands:
        if verbose:
            result = subprocess.run(command, cwd=cwd, env=env)
            captured: str | None = ""
        else:
            result = subprocess.run(command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            captured = result.stdout
        if result.returncode != 0:
            print(f"failed: {label} (exit {result.returncode}; {' '.join(command)})", file=sys.stderr)
            if captured:
                print(captured, file=sys.stderr)
            return None
    return captured


def c_project_is_up_to_date(build_dir: Path, executables: tuple[str, ...]) -> bool:
    """Check that CMake is configured and expected executables exist; CMake checks source staleness."""
    cache = build_dir / "CMakeCache.txt"
    if not cache.exists():
        return False
    for executable in executables:
        if not (build_dir / executable).exists():
            return False
    return True


def _cmake_rebuilt_anything(output: str) -> bool:
    """Detect compilation or linking in CMake build output."""
    return "Building " in output or "Linking " in output


def compile_c_project(relative_dir: str, build_type: str, executables: tuple[str, ...], *, verbose: bool, force: bool) -> bool:
    source = REPO_ROOT / relative_dir
    build = source / "build"
    label = f"{relative_dir} ({build_type})"
    if force:
        commands: list[list[str]] = [
            ["cmake", "-S", str(source), "-B", str(build), f"-DCMAKE_BUILD_TYPE={build_type}"],
            ["cmake", "--build", str(build), "-j", "--clean-first"],
        ]
        return run_commands(label, commands, verbose=verbose) is not None
    if not c_project_is_up_to_date(build, executables):
        # Never configured, or a previous build was interrupted. Configure
        # + incremental build: CMake handles whatever's stale.
        commands = [
            ["cmake", "-S", str(source), "-B", str(build), f"-DCMAKE_BUILD_TYPE={build_type}"],
            ["cmake", "--build", str(build), "-j"],
        ]
        return run_commands(label, commands, verbose=verbose) is not None
    # Already configured; let CMake decide what (if anything) to rebuild.
    # ``announce=False`` so we can replace the "Building ..." line with
    # an "up to date." summary if CMake reports no recompile/relink.
    captured = run_commands(label, [["cmake", "--build", str(build), "-j"]], verbose=verbose, announce=False)
    if captured is None:
        return False
    if _cmake_rebuilt_anything(captured):
        print(f"Rebuilt {label}.")
    else:
        print(f"{label}: up to date.")
    return True


def main(argv: list[str] | None = None) -> int:
    args = parse_arguments(argv)
    results = [
        compile_c_project(relative_dir, build_type, executables, verbose=args.verbose, force=args.rebuild)
        for relative_dir, build_type, executables in C_PROJECTS
    ]
    if args.dotnet:
        from bench.build_dotnet import build

        results.append(build(verbose=args.verbose, force=args.rebuild))
    if args.node:
        from bench.build_node import build

        results.append(build(verbose=args.verbose, force=args.rebuild))
    from common.sdk_workers import NAMES

    sdks = [name for name in NAMES if args.sdks or getattr(args, name.replace("-", "_"))]
    if sdks:
        from bench.build_sdks import build as build_sdks

        results.extend(build_sdks(sdks, verbose=args.verbose, force=args.rebuild))
    if all(results):
        print("All binaries up to date.")
        return 0
    print(f"{results.count(False)} of {len(results)} step(s) failed; see above.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
