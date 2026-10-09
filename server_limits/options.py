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

"""Server-limit workload settings and load-ladder bounds."""

from __future__ import annotations

from common.suites import (
    ConfigOption,
    one_of,
    path_string,
    positive_number,
    uniform,
    varying,
    whole_number,
)


_IMPLEMENTATIONS: tuple[str, ...] = (
    "open62541", "o6-python", "asyncua", "ua-dotnet", "node-opcua", "milo", "s2opc", "gopcua"
)
_MIN_SAMPLES = 7
_SAMPLES_DEFAULT = 7


OPTIONS: dict[str, ConfigOption] = {
    "implementation": ConfigOption(
        kind="varying",
        default=list(_IMPLEMENTATIONS[:3]),
        coerce=varying(one_of(*_IMPLEMENTATIONS)),
        help=(
            "Server implementations to push to their limit, each measured against the "
            "same fixed open62541 C client. ua-dotnet, node-opcua, milo, s2opc and gopcua are opt-in. "
            "Delete the ones you do not want measured."
        ),
    ),
    "step_seconds": ConfigOption(
        kind="uniform",
        default=10,
        coerce=uniform(whole_number(1)),
        help=(
            "How long the timed measurement window lasts at each load level. Longer "
            "settles noise but makes every step (and therefore the whole ramp) slower. "
            "The noise this settles is not sampling noise — even a short window sees "
            "millions of calls — but the machine's own: another process waking, a "
            "frequency change, a scheduler decision that lasts a while. A window has to "
            "be long enough to average over those rather than land inside one, which is "
            "why this is measured in seconds and not in calls."
        ),
    ),
    "warmup_seconds": ConfigOption(
        kind="uniform",
        default=1,
        coerce=uniform(whole_number(1)),
        help=(
            "Untimed time before measurement begins at each load level, to let sessions "
            "and pipelines fill."
        ),
    ),
    "grace_seconds": ConfigOption(
        kind="uniform",
        default=2,
        coerce=uniform(whole_number(1)),
        help=(
            "Extra time given to calls still in flight when step_seconds ends before they "
            "are given up on as abandoned (counted the same as a connection failure). "
            "Wants to be at least request_timeout_ms so a slow-but-alive server is not "
            "mistaken for one that gave up."
        ),
    ),
    "settle_seconds": ConfigOption(
        kind="uniform",
        default=1,
        coerce=uniform(whole_number(0)),
        help="Pause between load levels, to let the server work off whatever the last level queued.",
    ),
    "request_timeout_ms": ConfigOption(
        kind="uniform",
        default=2000,
        coerce=uniform(whole_number(1)),
        help=(
            "Per-call response timeout. Shorter makes the client declare defeat (and move "
            "on to the next call) faster once the server is struggling; longer avoids "
            "calling a merely-slow server 'timed out'."
        ),
    ),
    "min_clients": ConfigOption(
        kind="uniform",
        default=1,
        coerce=uniform(whole_number(1)),
        help="Client-process count the ramp starts at.",
    ),
    "max_clients": ConfigOption(
        kind="uniform",
        default=128,
        coerce=uniform(whole_number(1)),
        help=(
            "Client-process count the ramp will not exceed. The ladder between "
            "min_clients and this doubles each step, so raising it costs one more step, "
            "not many."
        ),
    ),
    "min_outstanding": ConfigOption(
        kind="uniform",
        default=1,
        coerce=uniform(whole_number(1)),
        help="Async requests-in-flight per client the ramp starts at.",
    ),
    "max_outstanding": ConfigOption(
        kind="uniform",
        default=256,
        coerce=uniform(whole_number(1)),
        help="Async requests-in-flight per client the ramp will not exceed.",
    ),
    "error_rate_threshold": ConfigOption(
        kind="uniform",
        default=0.01,
        coerce=uniform(positive_number),
        help=(
            "Fraction of calls at a load level that may time out, error, or go unanswered "
            "before the level counts as having beaten the server. 0.01 is 1%."
        ),
    ),
    "latency_blowup_factor": ConfigOption(
        kind="uniform",
        default=10,
        coerce=uniform(positive_number),
        help=(
            "A load level also counts as having beaten the server if its p99 latency is "
            "more than this many times the p99 latency measured at the lightest load "
            "(1 client, 1 outstanding) — a server can be answering everything and still "
            "have given up on answering promptly."
        ),
    ),
    "throughput_collapse_ratio": ConfigOption(
        kind="uniform",
        default=0.5,
        coerce=uniform(positive_number),
        help=(
            "A load level also counts as having beaten the server if its successful "
            "calls/second falls below this fraction of the last load level that did not — "
            "i.e. more load bought less completed work rather than more. This is the "
            "criterion most easily fired by noise rather than by load: it compares one "
            "measured median against another, so it carries both of their errors, and a "
            "machine whose repeats of one unchanged level vary by 1.9x will trip a "
            "tighter ratio than this on a bad draw alone. Raise it only for a run whose "
            "repeats agree closely."
        ),
    ),
    "samples": ConfigOption(
        kind="uniform",
        default=_SAMPLES_DEFAULT,
        coerce=uniform(whole_number(_MIN_SAMPLES)),
        help=(
            "Times each load level is measured and *kept*; the median of those decides "
            "whether it beat the server. Higher smooths a load level's own noise at the "
            "cost of taking that many times as long per level.\n"
            "The first new sample is discarded: each cell is measured "
            "one extra time beyond this number, and the first of those passes is dropped "
            "before the median is computed. A run that stores "
            f"{_SAMPLES_DEFAULT} samples per cell therefore fires the worker "
            f"{_SAMPLES_DEFAULT + 1} times. Under "
            "``--amend`` only the first of the *new* passes is discarded, so a topped-up "
            "cell ends up with this many kept entries and is never shrunk by a re-run.\n"
            "Raise it for a server whose own timing wanders — a pure-Python one, or a "
            "busy machine. Repeated measurements of one unchanged level on an idle asyncua "
            "server varied by 2.3x in p99, which at one repeat is enough to decide a "
            "verdict on its own. A run where a lighter load level failed while a heavier "
            "one held is one to re-run with this raised; the report flags that case."
        ),
    ),
    "c_binary_dir": ConfigOption(
        kind="uniform",
        default="server_limits/open62541/build",
        coerce=uniform(path_string),
        help=(
            "Directory holding the compiled open62541 executables (server / client), "
            "relative to the repository root. Build them first per the README."
        ),
    ),
}
