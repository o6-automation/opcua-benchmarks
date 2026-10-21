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

"""Plot statistics must describe the saved repetitions, including partial runs."""

import json
import math
import os
import sqlite3

import matplotlib

matplotlib.use("Agg")
from matplotlib import pyplot as plt
from matplotlib.container import ErrorbarContainer
import pytest

import singlecore_server_bench as preset


def test_skewed_samples_show_mean_and_sample_deviation():
    result = preset.ThroughputResult("open62541", "None", (1, 2, 9))
    assert result.middle == 4
    assert result.deviation == pytest.approx(math.sqrt(19))
    figure = preset.draw_throughput([result], {})
    try:
        axis = figure.axes[0]
        assert axis.patches[0].get_width() == 4
        (errorbar,) = [item for item in axis.containers if isinstance(item, ErrorbarContainer)]
        (segment,) = errorbar.lines[2][0].get_segments()
        assert segment[:, 0] == pytest.approx([4 - math.sqrt(19), 4 + math.sqrt(19)])
        assert axis.texts[0].get_text() == "4"
        assert axis.texts[0].xy[0] == pytest.approx(4 + math.sqrt(19))
        assert [label.get_text() for label in axis.texts] == ["4", "No result"]
        assert "sample standard deviation" in figure.texts[-1].get_text()
        assert axis.get_xlim()[0] <= 4 - math.sqrt(19)
    finally:
        plt.close(figure)


def test_single_sample_has_no_variance_tick():
    figure = preset.draw_throughput([preset.ThroughputResult("ua-dotnet", "Basic256Sha256", (7,))], {})
    try:
        assert not any(isinstance(item, ErrorbarContainer) for item in figure.axes[0].containers)
        assert figure.axes[0].patches[0].get_width() == 7
    finally:
        plt.close(figure)


def test_saved_rows_are_ranked_by_mean_and_use_recorded_native_revision():
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE results (name, suite, config_key, value)")
        connection.execute("CREATE TABLE metadata (name, suite, value)")
        for sdk, rates in [("open62541", (1, 2, 9)), ("asyncua", (3, 3, 3))]:
            key = dict(pair=f"open62541:{sdk}", operation="read", payload="scalar", mode="async", security="None")
            value = {"runs": [dict(operations=rate, elapsed_ns=1e9) for rate in rates]}
            connection.execute(
                "INSERT INTO results VALUES (?, ?, ?, ?)", ("test", "throughput", json.dumps(key), json.dumps(value))
            )
        metadata = {"open62541": {"source_commit": "0123456789abcdef"}}
        connection.execute("INSERT INTO metadata VALUES (?, ?, ?)", ("test", "throughput", json.dumps(metadata)))
        rows, versions = preset.read_throughput_results(connection, "test")
    assert [row.sdk for row in rows] == ["open62541", "asyncua"]
    assert versions["open62541"] == "0123456789ab"


@pytest.mark.parametrize("override,expected", [(None, "600"), ("900", "900")])
@pytest.mark.parametrize("fails", [False, True])
def test_preset_timeout_restores_environment(monkeypatch, override, expected, fails):
    monkeypatch.delenv("BENCH_SAMPLE_TIMEOUT", raising=False)
    if override is not None:
        monkeypatch.setenv("BENCH_SAMPLE_TIMEOUT", override)

    def collect(database, samples):
        assert os.environ["BENCH_SAMPLE_TIMEOUT"] == expected
        if fails:
            raise RuntimeError("failed")
        return 0

    monkeypatch.setattr(preset, "_measure", collect)
    if fails:
        with pytest.raises(RuntimeError, match="failed"):
            preset.measure(None, 7)
    else:
        assert preset.measure(None, 7) == 0
    assert os.environ.get("BENCH_SAMPLE_TIMEOUT") == override
