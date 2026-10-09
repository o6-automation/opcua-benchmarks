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

"""Runtime and evidence limits for an adaptive server-capacity estimate."""

from common.suites import ConfigOption, one_of, uniform, varying, whole_number

DEFAULT_IMPLEMENTATIONS = ("open62541", "o6-python", "asyncua", "ua-dotnet", "node-opcua")
# The optional server-only SDKs (common/sdk_workers.py) are opt-in.
IMPLEMENTATIONS = DEFAULT_IMPLEMENTATIONS + ("milo", "s2opc", "gopcua")

OPTIONS = {
    "implementation": ConfigOption(
        "varying",
        list(DEFAULT_IMPLEMENTATIONS),
        varying(one_of(*IMPLEMENTATIONS)),
        "Servers to search using the same native scalar Read client. milo, s2opc and gopcua are opt-in.",
    ),
    "probe_ms": ConfigOption("uniform", 1000, uniform(whole_number(250)), "Short discovery window in milliseconds."),
    "confirm_ms": ConfigOption(
        "uniform", 2000, uniform(whole_number(1000)), "Window for each fresh repeat of a selected candidate."
    ),
    "warmup_ms": ConfigOption("uniform", 500, uniform(whole_number(100)), "Uncounted warmup before each probe."),
    "timeout_ms": ConfigOption("uniform", 2000, uniform(whole_number(1)), "Per-request response timeout."),
    "grace_ms": ConfigOption("uniform", 2500, uniform(whole_number(1)), "Maximum drain duration; at least timeout_ms."),
    "start_outstanding": ConfigOption("uniform", 8, uniform(whole_number(1)), "Initial pipeline depth for one client."),
    "max_outstanding": ConfigOption("uniform", 512, uniform(whole_number(1)), "Pipeline-depth search cap."),
    "max_clients": ConfigOption(
        "uniform", 32, uniform(whole_number(1)), "Client-process search cap; actual CPU headroom is checked."
    ),
    "max_probes": ConfigOption("uniform", 40, uniform(whole_number(3)), "Maximum new observations per server per invocation."),
    "budget_seconds": ConfigOption(
        "uniform",
        90,
        uniform(whole_number(5)),
        "Wall-clock budget per server, including startup; teardown may add up to 10 seconds.",
    ),
    "plateau_percent": ConfigOption(
        "uniform", 5, uniform(one_of(3, 5, 10)), "Legacy plateau tolerance; unused by the ranked candidate search."
    ),
}
