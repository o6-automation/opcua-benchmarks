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

"""The complete user-facing configuration of the counter workload."""

from common.options import ConfigOption, one_of, uniform, varying

from subscription import strategy

MAX_ITEMS = 1_048_576

SERVERS = ("open62541", "o6-python", "asyncua", "node-opcua", "ua-dotnet", "milo", "gopcua", "s2opc")
# node-opcua through FrontThreadEngine (common/node-fronts): opt-in, application-update counters.
FRONTS = ("node-opcua-fronts",)


def bounded(minimum, maximum):
    def validate(value):
        return (type(value) is int and minimum <= value <= maximum) or f"must be an integer in [{minimum}, {maximum}]"

    return validate


def sampling_interval(value):
    return (type(value) is int and value in (1, 2, 5, 10, 20, 50, 100)) or "must be one of 1, 2, 5, 10, 20, 50, 100 integer ms"


OPTIONS = {
    "measurement": ConfigOption(
        "varying",
        ["client", "server"],
        varying(one_of("client", "server")),
        "Separate searches for each selected observer; never measure both in one run.",
    ),
    "implementation": ConfigOption(
        "varying",
        list(SERVERS),
        varying(one_of(*SERVERS, *FRONTS, "async-opcua")),
        "Server SDKs; every case uses the same native setup/client worker. "
        "node-opcua-fronts and async-opcua are opt-in.",
    ),
    **strategy.OPTIONS,
    "items_start": ConfigOption(
        "uniform", 128, uniform(bounded(1, MAX_ITEMS)), "Initial count for adaptive search; linear search starts at one."
    ),
    "items_max": ConfigOption("uniform", 65536, uniform(bounded(1, MAX_ITEMS)), "Upper bound for the monitored-item search."),
    "min_publishes": ConfigOption(
        "uniform",
        5,
        uniform(bounded(5, 1000)),
        "Minimum measured publishing intervals; increase for a longer run without changing the workload intervals.",
    ),
    "sampling_ms": ConfigOption(
        "varying",
        [10, 100],
        varying(sampling_interval),
        "Requested sampling interval (application update period for asyncua, gopcua and S2OPC) in milliseconds.",
    ),
    "publishing_ms": ConfigOption(
        "varying",
        [100, 1000, 5000],
        varying(bounded(1, 60000)),
        "Requested publishing intervals in milliseconds; each gets an independent capacity search.",
    ),
}


def measurement_window(sampling_ms, publishing_ms, min_publishes=5):
    """Expect at least 1000 increments, with 10% extra warm-up rounded to whole publishes."""
    if publishing_ms < sampling_ms:
        raise ValueError("publishing_ms must be greater than or equal to sampling_ms")
    intervals = max(min_publishes, (1000 * sampling_ms + publishing_ms - 1) // publishing_ms)
    warmup_intervals = (intervals + 9) // 10
    return warmup_intervals * publishing_ms, (warmup_intervals + intervals) * publishing_ms


def cases(uniform_values, varying_values):
    """Cross SDKs, sampling intervals, publishing intervals and observers."""
    settings = {**strategy.DEFAULTS, **uniform_values, **varying_values}
    if set(settings) != set(OPTIONS):
        raise ValueError("Configuration predates the current counter suite; start a new run after running 'new'")
    import json

    for name, option in OPTIONS.items():
        option.coerce(json.dumps(settings[name]))
        if option.kind == "varying":
            values = settings[name]
            if not values or len(values) != len(set(values)):
                raise ValueError(f"{name} must contain distinct values and cannot be empty")
    strategy.validate(settings)
    result = []
    for sdk in settings["implementation"]:
        for interval in settings["sampling_ms"]:
            for publishing in settings["publishing_ms"]:
                _, duration = measurement_window(interval, publishing, settings["min_publishes"])
                for side in settings["measurement"]:
                    result.append(
                        dict(
                            implementation=sdk,
                            sampling_ms=interval,
                            duration_ms=duration,
                            publishing_ms=publishing,
                            measurement=side,
                            min_publishes=settings["min_publishes"],
                        )
                    )
    return result
