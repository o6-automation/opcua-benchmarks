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

"""Singlecore Server Benchmark

Compare server SDKs in two scenarios:

- Reads: Across the configured SDKs, ten native clients keep 100 scalar Read requests in flight each,
  without encryption and with Basic256Sha256, to load the server and compare
  throughput and encryption overhead.
- Subscriptions: A linear sweep of monitored items uses 10 ms and 100 ms
  sampling with 1,000 ms publishing. Client-side progress shows how capacity
  changes with sampling frequency across the configured SDKs.

Servers use one logical CPU; clients use separate physical cores.
Requires Linux with at least two available physical cores.

Run: python singlecore_server_bench.py
Resume the latest saved run: python singlecore_server_bench.py --continue
Plot today's results: python singlecore_server_bench.py --plot
Plot another database: python singlecore_server_bench.py results/example.db --plot

Edit the settings below to adjust the preset. The script builds the workers
and by default saves results and reports to results/bench-singlecore-server-<YYYY-MM-DD>.db.
An optional database path selects another file for any mode.
A normal run clears previous results in that database; --continue keeps completed
measurements and resumes with the saved settings, including across dates.
Plotting reads saved results and writes PNGs to <database-name>.plots/ beside
the database: throughput, subscriptions at 10 ms, and subscriptions at 100 ms.
"""

import argparse
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
import json
from math import isclose, isfinite, log10
import os
from pathlib import Path
import shlex
import sqlite3
from statistics import mean, stdev
import subprocess
import sys

SERVERS = ["open62541", "o6-python", "asyncua", "node-opcua", "ua-dotnet", "milo", "gopcua", "s2opc", "async-opcua"]
SAMPLES = 7
BUILD_FLAGS = ["--node", "--node-opcua-fronts", "--dotnet", "--milo", "--gopcua", "--s2opc", "--async-opcua"]
RUN_NAME = "singlecore-server"
PLOT_DPI = 600
CONFIG = {
    "throughput": {
        "pair": [f"open62541:{server}" for server in SERVERS],
        "operation": ["read"],
        "payload": ["scalar"],
        "security": ["None", "Basic256Sha256"],
        "mode": ["async"],
        "clients": [10],
        "cpu_allocation": "singlecore",
        "max_outstanding": 100,  # per client
        "samples": SAMPLES,
        "iterations": 100000,
        "warmup": 10000,
    },
    "subscription": {
        "implementation": SERVERS,
        "measurement": ["client"],
        "search": "linear",
        "sampling_ms": [10, 100],
        "publishing_ms": [1000],
        "items_max": 262144,
    },
}
ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"

INK, GRAY, BACKGROUND = "#111315", "#697078", "#F4F5F6"

SDK_LABELS = {
    "ua-dotnet": ".NETStandard (C#)",
    "open62541": "open62541 (C)",
    "o6-python": "o6\\Python (Python)",
    "asyncua": "asyncua (Python)",
    "node-opcua": "node-opcua (TypeScript)",
    "node-opcua-fronts": "node-opcua fronts (TS)",
    "milo": "Milo (Java)",
    "gopcua": "gopcua (Go)",
    "s2opc": "S2OPC (C)",
    "async-opcua": "async-opcua (Rust)",
}

SDK_COLORS = {
    "open62541": "#697078",
    "o6-python": "#007F8F",
    "asyncua": "#257153",
    "node-opcua": "#A86100",
    "node-opcua-fronts": "#008300",
    "ua-dotnet": "#B6404C",
    "milo": "#7555A3",
    "gopcua": "#2878B5",
    "s2opc": "#A65383",
    "async-opcua": "#BD661A",
}

SECURITY_POLICIES = ("None", "Basic256Sha256")


def run(module, *arguments, check=True):
    command = [sys.executable, "-m", module, *map(str, arguments)]
    return subprocess.run(command, cwd=ROOT, check=check).returncode


def configure(database):
    from common.bench_db import BenchDB
    from common.suites import SUITE_OPTIONS

    with closing(BenchDB(database, "")) as store, store.transaction():
        for suite, settings in CONFIG.items():
            for name, option in SUITE_OPTIONS[suite].items():
                value = settings.get(name, option.default)
                if value is not None:
                    store.set_config(suite, name, json.dumps(value))
        if RUN_NAME in store.benchmark_names():
            store.restart_benchmark(RUN_NAME)
        else:
            store.create_benchmark(RUN_NAME)


def throughput_complete(database, samples):
    # The throughput CLI can return success with missing samples or failed cases.
    from common.bench_db import BenchDB

    with closing(BenchDB(database, "throughput", name=RUN_NAME)) as store:
        rows = list(store.results.values())
        return (
            bool(rows)
            and not store.failures
            and not store.stored_metadata.get("partial", True)
            and all(len(row.get("runs", [])) == samples for row in rows)
        )


def measure(database, samples):
    # A million encrypted reads can outlive the generic collection limit.
    previous = os.environ.get("BENCH_SAMPLE_TIMEOUT")
    os.environ.setdefault("BENCH_SAMPLE_TIMEOUT", "600")
    try:
        return _measure(database, samples)
    finally:
        if previous is None:
            os.environ.pop("BENCH_SAMPLE_TIMEOUT", None)


def _measure(database, samples):
    status = 0
    try:
        for suite, count in (("throughput", samples), ("subscription", 1)):
            code = run(f"bench.{suite}", "sample", count, database, "--name", RUN_NAME, "--amend", check=False)
            if code in (130, -2):
                status = 130
                break
            if code:
                status = 1
    except KeyboardInterrupt:
        print("Interrupted; completed measurements are saved.", file=sys.stderr)
        status = 130
    if not throughput_complete(database, samples):
        print("Throughput incomplete: inspect failures and sample counts in the report.", file=sys.stderr)
        status = status or 1
    report_status = run("bench.show", database, "--name", RUN_NAME, "--build-only", check=False)
    command = shlex.join([sys.executable, "-m", "bench.show", str(database), "--name", RUN_NAME])
    print(f"\nView the reports:\n  cd {shlex.quote(str(ROOT))} && {command}", flush=True)
    return status or report_status


@dataclass(frozen=True)
class ThroughputResult:
    sdk: str
    policy: str
    rates: tuple[float, ...]

    @property
    def middle(self) -> float:
        return mean(self.rates)

    @property
    def deviation(self) -> float:
        return stdev(self.rates) if len(self.rates) > 1 else 0


@dataclass(frozen=True)
class SubscriptionObservation:
    items: int
    worst_progress: float
    mean_progress: float
    passed: bool


def read_throughput_results(connection, name: str) -> tuple[list[ThroughputResult], dict[str, str]]:
    """Recompute rates from the named run's raw service-call counts and times."""
    grouped = {}
    conditions = set()
    for raw_key, raw_value in connection.execute(
        "SELECT config_key, value FROM results WHERE name = ? AND suite = 'throughput'", (name,)
    ):
        key, value = json.loads(raw_key), json.loads(raw_value)
        client, _, sdk = key.get("pair", "").partition(":")
        if (
            client != "open62541"
            or sdk not in SDK_LABELS
            or (key.get("operation"), key.get("payload"), key.get("mode")) != ("read", "scalar", "async")
            or key.get("security") not in SECURITY_POLICIES
        ):
            continue
        rates = []
        for run in value.get("runs", []):
            calls, elapsed = run["operations"], run["elapsed_ns"]
            if not all(isfinite(v) and v > 0 for v in (calls, elapsed)):
                raise ValueError(f"Invalid throughput count or elapsed time for {sdk}.")
            rates.append(calls * 1e9 / elapsed)
        if not rates:
            continue
        identity = sdk, key["security"]
        if identity in grouped:
            raise ValueError(f"Multiple throughput configurations for {sdk}/{key['security']}.")
        conditions.add(json.dumps({k: v for k, v in key.items() if k not in ("pair", "security")}, sort_keys=True))
        grouped[identity] = rates
    if len(conditions) > 1:
        raise ValueError("Throughput results have mixed workload settings; cannot compare them in one plot.")
    sdks = {sdk for sdk, policy in grouped}
    order = sorted(sdks, key=lambda sdk: (-mean(grouped.get((sdk, "None"), [0])), sdk))
    results = [
        ThroughputResult(sdk, policy, tuple(grouped[sdk, policy]))
        for sdk in order
        for policy in SECURITY_POLICIES
        if (sdk, policy) in grouped
    ]
    row = connection.execute("SELECT value FROM metadata WHERE name = ? AND suite = 'throughput'", (name,)).fetchone()
    metadata = json.loads(row[0]) if row else {}
    versions = {
        "o6-python": metadata.get("o6_version"),
        "asyncua": metadata.get("asyncua_version"),
        "open62541": metadata.get("open62541_version"),
        "ua-dotnet": metadata.get("ua_dotnet", {}).get("source_version"),
        "node-opcua": metadata.get("node_opcua", {})
        .get("runtime_info", {})
        .get("packages", {})
        .get("node_modules/node-opcua", {})
        .get("version"),
    }
    if metadata.get("open62541", {}).get("source_commit"):
        versions["open62541"] = metadata["open62541"]["source_commit"][:12]
    for sdk in ("milo", "gopcua", "s2opc", "async-opcua"):
        recorded = metadata.get(f"sdk_{sdk.replace('-', '_')}", {})
        versions[sdk] = recorded.get("runtime_info", {}).get("sdk") or recorded.get("sdk", "").rsplit(" ", 1)[-1]
    # Both Node variants use the same pinned installation recorded by this run.
    versions["node-opcua-fronts"] = versions["node-opcua"]
    # Missing runtime versions stay unknown; today's installed SDKs may differ.
    return results, {sdk: str(version) for sdk, version in versions.items() if version}


def read_subscription_results(connection, name: str) -> dict[int, dict[str, list[SubscriptionObservation]]]:
    """Recompute worst-item and mean progress from the saved counter deltas."""
    grouped = defaultdict(lambda: defaultdict(list))
    seen = set()
    conditions = defaultdict(set)
    for raw_key, raw_value in connection.execute(
        "SELECT config_key, value FROM results WHERE name = ? AND suite = 'subscription'", (name,)
    ):
        key = json.loads(raw_key)
        sampling_ms, sdk = key.get("sampling_ms"), key.get("implementation")
        if (
            key.get("measurement") != "client"
            or key.get("publishing_ms") != 1000
            or sampling_ms not in (10, 100)
            or sdk not in SDK_LABELS
        ):
            continue
        items = key["items"]
        identity = sampling_ms, sdk, items
        if identity in seen:
            raise ValueError(f"Duplicate subscription observation: {identity}.")
        seen.add(identity)
        observation = json.loads(raw_value)["observation"]
        summary = observation["summary"]
        counts = observation["recording"]["counts"]
        expected = summary["expected_per_item"]
        if (
            items <= 0
            or len(counts) != items
            or not isfinite(expected)
            or expected <= 0
            or any(not isfinite(value) or value < 0 for value in counts)
        ):
            raise ValueError(f"Invalid subscription counters for {identity}.")
        worst, mean = min(counts) / expected, sum(counts) / len(counts) / expected
        if not (
            isclose(worst, summary["progress_fraction_min"], abs_tol=1e-12)
            and isclose(mean, summary["progress_fraction_mean"], abs_tol=1e-12)
        ):
            raise ValueError(f"Subscription counters disagree with the summary for {identity}.")
        conditions[sampling_ms].add((summary["warmup_ms"], summary["measured_duration_ms"], observation["setup"]["queue_size"]))
        grouped[sampling_ms][sdk].append(SubscriptionObservation(items, 100 * worst, 100 * mean, worst >= 0.99))
    if any(len(values) > 1 for values in conditions.values()):
        raise ValueError("Subscription results have mixed measurement windows or queue sizes.")
    return {
        sampling_ms: {sdk: sorted(series[sdk], key=lambda row: row.items) for sdk in SDK_COLORS if sdk in series}
        for sampling_ms, series in grouped.items()
    }


def configure_plot_style():
    from matplotlib import font_manager, pyplot as plt
    from matplotlib.colors import to_rgba

    # DejaVu Sans ships with Matplotlib, so plots need no external font assets.
    font_family = "DejaVu Sans"
    title_font = font_manager.FontProperties(family=font_family, weight="bold", size=17)
    plt.rcParams.update(
        {
            "font.family": font_family,
            "font.size": 12,
            "axes.labelsize": 12,
            "axes.edgecolor": to_rgba(GRAY, 0.28),
            "axes.linewidth": 0.6,
            "text.color": INK,
            "axes.labelcolor": INK,
            "xtick.color": INK,
            "ytick.color": INK,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "svg.fonttype": "none",
            "figure.facecolor": BACKGROUND,
            "axes.facecolor": BACKGROUND,
            "savefig.facecolor": BACKGROUND,
        }
    )
    return title_font


def add_plot_watermark(fig):
    from matplotlib import pyplot as plt

    logo_path = ROOT / "o6-logo-final-notext.png"
    if logo_path.is_file():
        logo = plt.imread(logo_path)
        width = 0.05
        height = width * fig.get_figwidth() / fig.get_figheight() * logo.shape[0] / logo.shape[1]
        axes = fig.add_axes([0.96 - width, 0.04, width, height])
        axes.imshow(logo, alpha=1.0, interpolation="lanczos")
        axes.set_axis_off()


def draw_throughput(results: list[ThroughputResult], versions: dict[str, str]):
    """Plot the mean and ±1 sample standard deviation across repetitions."""
    from matplotlib import patheffects
    from matplotlib import pyplot as plt
    from matplotlib.colors import to_rgba
    from matplotlib.offsetbox import AnchoredOffsetbox, HPacker, TextArea, VPacker
    from matplotlib.patches import Patch
    from matplotlib.ticker import AutoMinorLocator, FuncFormatter, MaxNLocator

    title_font = configure_plot_style()
    sdks = list(dict.fromkeys(r.sdk for r in results))
    height = 4.1 + 0.6 * len(sdks)
    fig, ax = plt.subplots(figsize=(8.4, height))
    fig.subplots_adjust(left=0.31, right=0.96, bottom=2.7 / height, top=1 - 1.65 / height)
    fig.suptitle(
        "OPC UA Server Throughput\n(Single Scalar Reads)",
        fontproperties=title_font,
        y=1 - 0.25 / height,
        linespacing=1.25,
    )
    limits = []
    for result in results:
        encrypted = result.policy == "Basic256Sha256"
        position = sdks.index(result.sdk) + (0.17 if encrypted else -0.17)
        ax.barh(
            position,
            result.middle,
            height=0.29,
            color=SDK_COLORS[result.sdk],
            edgecolor=BACKGROUND,
            linewidth=0,
            hatch="////" if encrypted else None,
            zorder=3,
        )
        lower, upper = result.middle - result.deviation, result.middle + result.deviation
        limits.append((lower, upper))
        if len(result.rates) > 1:
            bars = ax.errorbar(
                result.middle,
                position,
                xerr=result.deviation,
                fmt="none",
                ecolor=INK,
                elinewidth=0.9,
                capsize=3,
                capthick=0.9,
                zorder=4,
            )
            # Keep ticks visible on dark and hatched bars.
            for artist in (*bars[1], *bars[2]):
                artist.set_path_effects([patheffects.Stroke(linewidth=2.1, foreground=BACKGROUND), patheffects.Normal()])
        ax.annotate(
            f"{result.middle:,.0f}",
            (upper, position),
            xytext=(5, 0),
            textcoords="offset points",
            ha="left",
            va="center",
            fontsize=11,
            color=INK,
            zorder=4,
        )
    measured = {(result.sdk, result.policy) for result in results}
    for index, sdk in enumerate(sdks):
        for policy, offset in (("None", -0.17), ("Basic256Sha256", 0.17)):
            if (sdk, policy) not in measured:
                ax.annotate(
                    "No result",
                    (0, index + offset),
                    xytext=(5, 0),
                    textcoords="offset points",
                    ha="left",
                    va="center",
                    fontsize=10,
                    color=GRAY,
                )
    fig.text(
        0.5,
        1 - 1.45 / height,
        "Bars and labels: mean · Ticks: ±1 sample standard deviation",
        ha="center",
        va="center",
        fontsize=10,
        color=GRAY,
    )
    fig.legend(
        handles=[
            Patch(facecolor=GRAY, label="Unencrypted"),
            Patch(
                facecolor=GRAY,
                edgecolor=BACKGROUND,
                linewidth=0,
                hatch="////",
                label="Encrypted",
            ),
        ],
        loc="upper center",
        bbox_to_anchor=(0.5, 1 - 1.0 / height),
        ncol=2,
        frameon=False,
        fontsize=11,
        handlelength=2.4,
        columnspacing=2.5,
    )
    ax.set_yticks(range(len(sdks)), [SDK_LABELS[sdk] for sdk in sdks])
    ax.set_ylim(len(sdks) - 0.5, -0.5)
    ax.set_xlim(min(0, min(lower for lower, _ in limits)), max(upper for _, upper in limits) * 1.18)
    ax.xaxis.set_major_locator(MaxNLocator(nbins=5, steps=[1, 2, 2.5, 5, 10]))
    ax.xaxis.set_minor_locator(AutoMinorLocator(10))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:,.0f}"))
    ax.set_xlabel("Requests/second", labelpad=10)
    ax.set_axisbelow(True)
    ax.grid(axis="x", which="major", color=GRAY, alpha=0.18, linewidth=0.6)
    ax.grid(axis="x", which="minor", color=GRAY, alpha=0.09, linewidth=0.45)
    ax.tick_params(axis="y", length=0, pad=10)
    ax.tick_params(axis="x", length=3, pad=5)
    ax.tick_params(axis="x", which="minor", length=2, color=to_rgba(GRAY, 0.28))
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)
    # Keep versions below the chart so they cannot cover bars, including when
    # plotting a partial run or all nine SDKs.
    legend_font = title_font.copy()
    legend_font.set_size(10.5)
    label_props = {"size": 10.5, "color": INK}
    columns = []
    rows_per_column = max(4, (len(sdks) + 1) // 2)
    for start in range(0, len(sdks), rows_per_column):
        column = sdks[start : start + rows_per_column]
        names = VPacker(
            children=[TextArea(SDK_LABELS[sdk].split(" (")[0], textprops=label_props) for sdk in column],
            align="left",
            pad=0,
            sep=6,
        )
        numbers = VPacker(
            children=[TextArea(versions.get(sdk, "Not recorded"), textprops=label_props) for sdk in column],
            align="left",
            pad=0,
            sep=6,
        )
        columns.append(HPacker(children=[names, numbers], align="top", pad=0, sep=18))
    table = HPacker(children=columns, align="top", pad=0, sep=35)
    content = VPacker(
        children=[
            TextArea("Library versions", textprops={"fontproperties": legend_font}),
            table,
        ],
        align="left",
        pad=0,
        sep=9,
    )
    version_box = AnchoredOffsetbox(
        loc="lower center",
        child=content,
        frameon=True,
        bbox_to_anchor=(0.5, 0.025),
        bbox_transform=fig.transFigure,
        pad=0.7,
        borderpad=0,
    )
    version_box.patch.set_facecolor(BACKGROUND)
    version_box.patch.set_edgecolor(to_rgba(GRAY, 0.28))
    version_box.patch.set_linewidth(0.6)
    fig.add_artist(version_box)
    add_plot_watermark(fig)
    return fig


def subscription_marker_indices(rows, width_points, height_points, sample_rate=100, axis_end=1000000, axis_start=1):
    """Thin only dense markers, never the curve or its underlying observations."""
    if not rows:
        return []
    # Project into the logarithmic axis before judging visible marker spacing.
    points = [
        (
            log10(r.items * sample_rate / axis_start) / log10(axis_end / axis_start) * width_points,
            (100 - r.worst_progress) / 83 * height_points,
        )
        for r in rows
    ]
    keep = {0, len(rows) - 1}
    previous = 0
    for index in range(1, len(rows) - 1):
        if points[index][0] - points[previous][0] >= 7:
            keep.add(index)
            previous = index
    last_passing = max((i for i, r in enumerate(rows) if r.passed), default=None)
    if last_passing is not None:
        keep.add(last_passing)
    # Distances are in typographic points at this figure's actual layout.
    # Douglas-Peucker selection preserves visible bends between retained markers.

    def select(first, last):
        if last - first < 2:
            return
        x0, y0 = points[first]
        dx, dy = points[last][0] - x0, points[last][1] - y0
        norm = (dx * dx + dy * dy) ** 0.5
        distances = [(abs(dy * (points[i][0] - x0) - dx * (points[i][1] - y0)) / norm, i) for i in range(first + 1, last)]
        distance, index = max(distances)
        if distance > 2.0:
            keep.add(index)
            select(first, index)
            select(index, last)

    anchors = sorted(keep)
    for first, last in zip(anchors, anchors[1:]):
        select(first, last)
    return sorted(keep)


def draw_subscriptions(series, sampling_ms=10):
    from matplotlib import pyplot as plt
    from matplotlib.colors import to_rgba
    from matplotlib.lines import Line2D
    from matplotlib.patches import ConnectionPatch
    from matplotlib.ticker import FuncFormatter, LogLocator, MultipleLocator, NullFormatter

    title_font = configure_plot_style()
    sample_rate = 1000 / sampling_ms
    smallest_load = min(row.items * sample_rate for rows in series.values() for row in rows)
    largest_load = max(row.items * sample_rate for rows in series.values() for row in rows)
    axis_start, axis_end = smallest_load / 1.25, largest_load * 1.15
    extra_height = max(0, len(series) - 5) * 0.25
    fig = plt.figure(figsize=(8.4, 8.1 + extra_height))
    grid = fig.add_gridspec(
        2, 1, height_ratios=[1.6 + extra_height, 3.3], hspace=0.25, left=0.29, right=0.935, bottom=0.20, top=0.73
    )
    ax = fig.add_subplot(grid[1])
    guides = fig.add_subplot(grid[0], sharex=ax)
    title = fig.suptitle(
        f"OPC UA Monitoring Samples\n({sample_rate:g}Hz/Variable)", fontproperties=title_font, y=0.96, linespacing=1.25
    )
    handles = []
    for sdk, rows in series.items():
        x, color = [r.items * sample_rate for r in rows], SDK_COLORS[sdk]
        y = [100 - r.worst_progress for r in rows]
        last_passing = max((i for i, r in enumerate(rows) if r.passed), default=None)
        if last_passing is not None:
            ax.plot(x[: last_passing + 1], y[: last_passing + 1], color=color, lw=2.1, zorder=3)
            ax.plot(x[last_passing:], y[last_passing:], color=color, lw=2.1, ls=":", zorder=3)
        else:
            ax.plot(x, y, color=color, lw=2.1, ls=":", zorder=3)
        position = ax.get_position()
        marked = subscription_marker_indices(
            rows,
            position.width * fig.get_figwidth() * 72,
            position.height * fig.get_figheight() * 72,
            sample_rate,
            axis_end,
            axis_start,
        )
        ax.plot(
            [x[i] for i in marked],
            [y[i] for i in marked],
            color=color,
            marker="|",
            markersize=7,
            linestyle="none",
            markeredgewidth=1.3,
            zorder=5,
        )
        handles.append(Line2D([], [], color=color, lw=2, label=SDK_LABELS[sdk], linestyle="-"))
    ax.axhline(1, color="#FF0000", linestyle=":", linewidth=1.3, zorder=2)
    handles.append(Line2D([], [], color="#FF0000", lw=1.3, linestyle=":", label="1% missed threshold"))
    ax.set_xscale("log")
    ax.set_xlim(axis_start, axis_end)
    ax.xaxis.set_major_locator(LogLocator(base=10))
    ax.xaxis.set_minor_locator(LogLocator(base=10, subs=range(2, 10)))
    ax.xaxis.set_minor_formatter(NullFormatter())
    ax.set_ylim(80, -3)  # Burndown: 0% missed at the top, 80% at the bottom.
    ax.yaxis.set_major_locator(MultipleLocator(10))
    ax.yaxis.set_minor_locator(MultipleLocator(5))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:.0f}%"))
    ax.tick_params(axis="x", labelsize=10.5, pad=5)
    ax.set_xlabel("Samples/second", labelpad=9)
    ax.set_ylabel("Percent Missed Samples", labelpad=10)
    ax.set_axisbelow(True)
    ax.grid(which="major", color=GRAY, alpha=0.18, linewidth=0.6)
    ax.grid(which="minor", color=GRAY, alpha=0.09, linewidth=0.45)
    ax.tick_params(which="major", length=3, pad=5)
    ax.tick_params(which="minor", length=2, color=to_rgba(GRAY, 0.28))
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    legend = fig.legend(
        handles=handles,
        loc="upper center",
        bbox_to_anchor=(0.5, 0.89),
        ncol=3,
        fontsize=10.5,
        framealpha=1,
        columnspacing=1.2,
        borderpad=0.6,
        labelspacing=0.4,
        facecolor=BACKGROUND,
    )
    legend.get_frame().set_edgecolor(to_rgba(GRAY, 0.28))
    legend.get_frame().set_linewidth(0.6)
    guides.set_title(
        "Largest tested load with ≤1% missed",
        loc="left",
        fontsize=11,
        pad=12,
        bbox={"facecolor": BACKGROUND, "edgecolor": "none", "pad": 2},
    )
    capacities = [(sdk, max((r.items * sample_rate for r in rows if r.passed), default=0)) for sdk, rows in series.items()]
    capacities.sort(key=lambda item: item[1], reverse=True)
    for index, (sdk, limit) in enumerate(capacities):
        color = SDK_COLORS[sdk]
        if limit > 0:
            guides.hlines(index, axis_start, limit, color=color, linewidth=1.8)
            guides.plot(limit, index, marker="|", color=color, markersize=8)
            guides.annotate(
                f"{limit:,.0f}", (limit, index), xytext=(5, 0), textcoords="offset points", fontsize=10.5, va="center"
            )
            # Same x-coordinate on both shared axes: link the 1% missed reference
            # at the last passing load to its capacity endpoint above.
            arrow = ConnectionPatch(
                xyA=(limit, 1),
                coordsA="data",
                axesA=ax,
                xyB=(limit, index),
                coordsB="data",
                axesB=guides,
                arrowstyle="-|>",
                mutation_scale=7,
                linewidth=0.65,
                color=color,
                alpha=0.55,
                shrinkA=2,
                shrinkB=1,
                clip_on=False,
                zorder=1.5,
            )
            guides.add_artist(arrow)
        else:
            guides.text(
                0.005,
                index,
                "No passing load",
                fontsize=10.5,
                transform=guides.get_yaxis_transform(),
                va="center",
                color=color,
            )
    guides.set_yticks(range(len(capacities)), [SDK_LABELS[sdk] for sdk, _ in capacities])
    guides.set_ylim(len(capacities) - 0.5, -0.5)
    guides.tick_params(axis="y", length=0, labelsize=10.5, pad=8)
    guides.tick_params(axis="x", which="both", labelbottom=False)
    guides.set_axisbelow(True)
    guides.grid(axis="x", which="major", color=GRAY, alpha=0.18, linewidth=0.6)
    guides.grid(axis="x", which="minor", color=GRAY, alpha=0.09, linewidth=0.45)
    for spine in ("top", "right", "left"):
        guides.spines[spine].set_visible(False)
    fig.text(
        guides.get_position().x0,
        0.04,
        "Solid: through last passing sample\nDotted: beyond passing load",
        fontsize=10.5,
        color=INK,
        linespacing=1.4,
        va="bottom",
    )
    add_plot_watermark(fig)
    # Balance the title over the full artwork, including the left-side labels.
    fig.canvas.draw()
    bounds = fig.get_tightbbox(fig.canvas.get_renderer())
    title.set_x((bounds.x0 + bounds.x1) / (2 * fig.get_figwidth()))
    return fig


def plot(database):
    # Open read-only before importing optional plotting dependencies. A typo must
    # never create an empty database or restart a benchmark.
    if not database.is_file():
        raise ValueError(f"Database not found: {database}")
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        if not connection.execute("SELECT 1 FROM benchmarks WHERE name = ?", (RUN_NAME,)).fetchone():
            raise ValueError(f"No {RUN_NAME!r} run found in {database}.")
        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ModuleNotFoundError as error:
            raise RuntimeError("Plotting requires matplotlib; install requirements.txt first.") from error
        connection.execute("BEGIN")
        reads, versions = read_throughput_results(connection, RUN_NAME)
        series = read_subscription_results(connection, RUN_NAME)

    if not reads and not series:
        raise ValueError("No matching throughput or subscription measurements found.")
    output_dir = database.with_suffix(".plots")
    output_dir.mkdir(parents=True, exist_ok=True)

    def save(figure, name, **settings):
        output = output_dir / name
        try:
            figure.savefig(output, dpi=PLOT_DPI, **settings)
        finally:
            plt.close(figure)
        print(f"Saved {output}", flush=True)

    if reads:
        save(draw_throughput(reads, versions), "throughput_publication.png")
    else:
        print("No throughput measurements; skipping throughput plot.", file=sys.stderr)
    for sampling_ms in (10, 100):
        if sampling_ms not in series:
            print(f"No {sampling_ms} ms subscription measurements; skipping plot.", file=sys.stderr)
            continue
        save(
            draw_subscriptions(series[sampling_ms], sampling_ms=sampling_ms),
            f"subscriptions_{sampling_ms}ms_publication.png",
            bbox_inches="tight",
            pad_inches=0.22,
        )
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "database",
        nargs="?",
        type=Path,
        metavar="DATABASE",
        help="database path (default: today's database, or the latest saved run with --continue)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--continue", dest="resume", action="store_true", help="resume saved results (default: the latest saved run)"
    )
    mode.add_argument(
        "--plot",
        action="store_true",
        help="plot saved results without running benchmarks (default: today's database)",
    )
    args = parser.parse_args(argv)
    try:
        database = RESULTS / f"bench-{RUN_NAME}-{datetime.now():%Y-%m-%d}.db"
        if args.database is not None:
            database = args.database.expanduser()
            if database.parent == Path(".") and not database.exists():
                database = RESULTS / database
        elif args.resume:
            database = max(RESULTS.glob(f"bench-{RUN_NAME}-*.db"), key=lambda path: path.stat().st_mtime_ns, default=None)
            if database is None:
                raise ValueError("No saved singlecore server benchmark found to continue.")
        if args.plot:
            return plot(database)

        from common.bench_db import BenchDB

        if args.resume:
            if not database.is_file():
                raise ValueError(f"Database not found: {database}")
        else:
            database.parent.mkdir(parents=True, exist_ok=True)
            configure(database)
        with closing(BenchDB(database, "throughput", name=RUN_NAME)) as store:
            if RUN_NAME not in store.benchmark_names():
                raise ValueError("No configured run found; start again without --continue.")
            uniform, _ = store.get_config("throughput")
            if uniform.get("cpu_allocation") != "singlecore":
                raise ValueError("Saved run does not use single-core CPU allocation; start a fresh run in a new database.")
            samples = uniform.get("samples") or SAMPLES
        print(f"Database: {database}", flush=True)
        run("bench.build", *BUILD_FLAGS)
        return measure(database, samples)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.CalledProcessError) as error:
        print(error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
