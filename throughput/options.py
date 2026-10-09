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

"""Throughput matrix axes and shared workload settings."""

from __future__ import annotations

from common.suites import (
    ConfigOption,
    one_of,
    payload_token,
    path_string,
    uniform,
    varying,
    whole_number,
)

_DEFAULT_PAIRS: frozenset[tuple[str, str]] = frozenset(
    {
        ("open62541", "open62541"),
        ("o6-python", "open62541"),
        ("open62541", "o6-python"),
        ("o6-python", "o6-python"),
        ("asyncua", "open62541"),
        ("open62541", "asyncua"),
        ("asyncua", "asyncua"),
    }
)
_ALL_PAIRS = _DEFAULT_PAIRS | frozenset(
    (client, server)
    for client in ("open62541", "o6-python", "asyncua", "ua-dotnet", "node-opcua")
    for server in ("open62541", "o6-python", "asyncua", "ua-dotnet", "node-opcua")
    if {"ua-dotnet", "node-opcua"}.intersection((client, server))
) | frozenset(
    # The optional server-only SDKs (common/sdk_workers.py), against every client.
    (client, server)
    for client in ("open62541", "o6-python", "asyncua", "ua-dotnet", "node-opcua")
    for server in ("milo", "s2opc", "gopcua")
)
_SECURITY_POLICIES: tuple[str, ...] = ("None", "Basic256Sha256")
_PAIR_STRINGS: tuple[str, ...] = tuple(sorted(f"{client}:{server}" for client, server in _ALL_PAIRS))


OPTIONS: dict[str, ConfigOption] = {
    "security": ConfigOption(
        kind="varying",
        default=["None", "Basic256Sha256"],
        coerce=varying(one_of(*_SECURITY_POLICIES)),
        help=(
            "OPC UA security policies to measure. 'None' is plaintext; "
            "'Basic256Sha256' runs SignAndEncrypt with auto-generated self-signed "
            "certificates. The difference between the two is the cost of the crypto."
        ),
    ),
    "pair": ConfigOption(
        kind="varying",
        default=sorted(f"{client}:{server}" for client, server in _DEFAULT_PAIRS),
        coerce=varying(one_of(*_PAIR_STRINGS)),
        help=(
            "Client/server pairings to measure, as 'client:server' strings. The first "
            "element is the client implementation, the second the server. "
            "Implementations: open62541, o6-python, asyncua, ua-dotnet and node-opcua (opt-in), plus the "
            "server-only milo, s2opc and gopcua (opt-in, as the second element). The supported choices are the "
            "whole matrix; defaults retain the seven C/Python pairs. A 'client:server' "
            "combination that is not listed here is not part of the comparison."
        ),
    ),
    "operation": ConfigOption(
        kind="varying",
        default=["read", "write"],
        coerce=varying(one_of("read", "write")),
        help=(
            "Services to benchmark. 'read' reads a single Int32 value; 'write' writes "
            "one (each node's own numeric id, so the sequence is deterministic across "
            "implementations)."
        ),
    ),
    "mode": ConfigOption(
        kind="varying",
        default=["sync", "async"],
        coerce=varying(one_of("sync", "async")),
        help=(
            "Request modes to measure. 'sync' issues one request at a time "
            "(max_outstanding is forced to 1); 'async' pipelines up to "
            "max_outstanding requests in flight. Node sync uses sequential asynchronous I/O."
        ),
    ),
    "clients": ConfigOption(
        kind="varying",
        default=[1, 3, 10],
        coerce=varying(whole_number(1)),
        help=(
            "Concurrent client-process counts to test. Each value is measured "
            "separately, so [1, 3, 10] produces results for 1, 3, and 10 simultaneous "
            "clients and shows whether throughput holds as load is added."
        ),
    ),
    "payload": ConfigOption(
        kind="varying",
        default=[
            "scalar",
            "batch:10",
            "batch:100",
            "batch:1000",
            "array:100",
            "array:1000",
            "array:vga",
            "array:full_hd",
        ],
        coerce=varying(payload_token),
        help=(
            "Payload shapes to measure, one shape per configuration:\n"
            "  scalar    a single Int32 on one node — where both curves start\n"
            "  batch:N   N scalar nodes addressed in one Read/Write call\n"
            "  array:M   one node holding M Int32 elements; M is an integer or one\n"
            "            of the named pixel counts vga (307,200), hd (921,600),\n"
            "            full_hd (2,073,600), 4k (8,294,400)\n"
            "Batching and arrays are the two ways to move more per call, and they are "
            "measured separately so neither curve confounds the other — which is why "
            "this is one axis of shapes rather than a batch size crossed with an array "
            "size. Delete the shapes you do not want; 'scalar' alone measures "
            "single-value access only."
        ),
    ),
    "iterations": ConfigOption(
        kind="uniform",
        default=2000,
        coerce=uniform(whole_number(1)),
        help=(
            "Timed Read/Write service calls each client performs per sample. Used as "
            "given unless max_values has to cut it for a larger payload; the runner "
            "names every cut on stderr before it starts, and a stored sample records "
            "the calls it made as 'operations'. Higher lowers variance."
        ),
    ),
    "warmup": ConfigOption(
        kind="uniform",
        default=100,
        coerce=uniform(whole_number(1)),
        help=(
            "Untimed calls each client performs before measurement begins, to warm "
            "caches and connections. Excluded from the reported rate, along with "
            "connect, session setup, and NodeId resolution."
        ),
    ),
    "samples": ConfigOption(
        kind="uniform",
        default=None,
        coerce=uniform(whole_number(1)),
        help=(
            "Samples per configuration, as the last 'run.py sample <num_samples>' asked "
            "for. Written by the runner; editing it changes nothing, since the next run "
            "takes the count from its command line and writes it back here. It is the "
            "document's record of the target: every sample is stored individually in the "
            "row's 'runs' array and the reported median, min, and max are computed from "
            "them, so a row holding fewer 'runs' than this is short of the target, and no "
            "row repeats the number."
        ),
    ),
    "max_outstanding": ConfigOption(
        kind="uniform",
        default=32,
        coerce=uniform(whole_number(1)),
        help=(
            "Async pipeline depth: the most in-flight requests per client before "
            "awaiting (used in 'async' mode only). Every client raises its own "
            "outstanding-call limit to this, so the depth measured is the depth asked for."
        ),
    ),
    "max_values": ConfigOption(
        kind="uniform",
        default=2000000,
        coerce=uniform(whole_number(0)),
        help=(
            "Value ceiling: the most Int32 values one client may move per sample. "
            "Large payloads are cut back to fit it, so a 4k array does not spend "
            "minutes moving 66 GB. Together with min_service_calls:\n"
            "  calls = min(iterations, max(min_service_calls, max_values // per-call))\n"
            "Single-value access is never cut. Set to 0 to remove the ceiling and "
            "always run 'iterations' calls, however long that takes."
        ),
    ),
    "min_service_calls": ConfigOption(
        kind="uniform",
        default=100,
        coerce=uniform(whole_number(1)),
        help=(
            "Call floor: the fewest service calls a sample may be cut to by the value "
            "ceiling. A sample of three calls measures scheduling noise rather than "
            "throughput, so the ceiling never reduces one below this."
        ),
    ),
    "c_binary_dir": ConfigOption(
        kind="uniform",
        default="throughput/open62541/build",
        coerce=uniform(path_string),
        help=(
            "Directory holding the compiled open62541 executables (server / client), "
            "relative to the repository root. Build them first per the README; pairs "
            "that need them fail if they are missing."
        ),
    ),
}
