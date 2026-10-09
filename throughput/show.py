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

"""Render an interactive Plotly report from a throughput bench document.

Reads the bench document written by ``bench.throughput sample`` and emits a
single self-contained HTML file: plotly.js, the measurements, and the control
logic are all inlined, so the report can be copied around or attached to a
mail without losing its interactivity.

The report answers four questions, one chart each, all scoped by one shared
control row:

1. Who is fastest?              ranking bar
2. Client cost or server cost?  5x5 heatmap over the pairing matrix
3. Does it hold under load?     throughput vs. concurrent clients
4. What does a feature cost?    ratio bars (encryption / pipelining / vs. C)

plus a read-vs-write scatter that justifies not faceting on the operation.
"""

from __future__ import annotations

import json
from pathlib import Path

import plotly.graph_objects as go
from plotly.offline import get_plotlyjs

from common.bench_db import BenchDB
from common.sdk_workers import LABELS as SDK_LABELS, NAMES as SDK_NAMES

# Keep the named sizes in one place: the runner resolves them to element
# counts, and the report turns them back into "array hd" for its labels.
NAMED_ARRAY_SIZES = {
    "vga": 640 * 480,
    "hd": 1280 * 720,
    "full_hd": 1920 * 1080,
    "4k": 3840 * 2160,
}

PLOTLY_CDN = "https://cdn.plot.ly/plotly-3.0.1.min.js"

# Implementations ordered best-known-first; this fixes the heatmap axes and the
# categorical color slots, so a pair keeps its hue no matter what is filtered.
IMPLEMENTATION_ORDER: tuple[str, ...] = ("open62541", "o6-python", "asyncua", "ua-dotnet", "node-opcua") + SDK_NAMES
PAIR_ORDER: tuple[str, ...] = (
    "open62541/open62541",
    "o6-python/open62541",
    "open62541/o6-python",
    "o6-python/o6-python",
    "asyncua/open62541",
    "open62541/asyncua",
    "asyncua/asyncua",
    "open62541/ua-dotnet",
    "ua-dotnet/open62541",
    "o6-python/ua-dotnet",
    "ua-dotnet/o6-python",
    "asyncua/ua-dotnet",
    "ua-dotnet/asyncua",
    "ua-dotnet/ua-dotnet",
    "open62541/node-opcua",
    "node-opcua/open62541",
    "o6-python/node-opcua",
    "node-opcua/o6-python",
    "asyncua/node-opcua",
    "node-opcua/asyncua",
    "ua-dotnet/node-opcua",
    "node-opcua/ua-dotnet",
    "node-opcua/node-opcua",
)
BASELINE_PAIR = "open62541/open62541"

# Slots 1..7 retain the validated default palette; the added .NET series use
# labels, tables, and dashed scaling curves as additional distinctions.
# Verified with the data-viz validator on the adjacent pairlist: light worst
# CVD dE 9.1 / normal-vision 19.6, dark 8.4 / 19.3, both PASS. Three light-mode
# fills sit below 3:1 against the surface, which obliges the relief channel --
# hence the always-on bar-tip labels and the per-chart table view.
SERIES_LIGHT: tuple[str, ...] = (
    "#2a78d6",
    "#eb6834",
    "#1baf7a",
    "#eda100",
    "#e87ba4",
    "#008300",
    "#4a3aa7",
    "#007f86",
    "#a54800",
    "#806500",
    "#aa3377",
    "#667722",
    "#555555",
    "#663399",
    "#206060",
    "#804040",
    "#406080",
    "#806040",
    "#408040",
    "#804080",
    "#604080",
    "#408080",
    "#607020",
)
SERIES_DARK: tuple[str, ...] = (
    "#3987e5",
    "#d95926",
    "#199e70",
    "#c98500",
    "#d55181",
    "#008300",
    "#9085e9",
    "#44bbbb",
    "#ee9955",
    "#bbaa44",
    "#ee88bb",
    "#aabb66",
    "#bbbbbb",
    "#bb88ee",
    "#60bbbb",
    "#dd8888",
    "#88aacc",
    "#ccaa88",
    "#88cc88",
    "#cc88cc",
    "#aa88cc",
    "#88cccc",
    "#b9bd62",
)

# Sequential blue ramp, steps 100 -> 700. Light surfaces read light->dark; the
# dark surface flips the anchor so "more" is still the louder end.
SEQUENTIAL = ("#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b")

THEMES: dict[str, dict[str, str]] = {
    "light": {
        "surface": "#fcfcfb",
        "plane": "#f9f9f7",
        "primary": "#0b0b0b",
        "secondary": "#52514e",
        "muted": "#898781",
        "grid": "#e1e0d9",
        "axis": "#c3c2b7",
        "border": "rgba(11,11,11,0.10)",
        "hover": "#ffffff",
    },
    "dark": {
        "surface": "#1a1a19",
        "plane": "#0d0d0d",
        "primary": "#ffffff",
        "secondary": "#c3c2b7",
        "muted": "#898781",
        "grid": "#2c2c2a",
        "axis": "#383835",
        "border": "rgba(255,255,255,0.10)",
        "hover": "#242422",
    },
}

FONT_STACK = 'system-ui, -apple-system, "Segoe UI", sans-serif'

IMPL_LABELS = {
    "open62541": "open62541 (C)",
    "o6-python": "o6\\Python",
    "asyncua": "asyncua",
    "ua-dotnet": "OPC Foundation (.NET)",
    "node-opcua": "node-opcua (Node.js)",
    **SDK_LABELS,
}


def payload_shape(token: object) -> tuple[int, int] | None:
    """Decode a matrix ``payload`` token into ``(batch_size, array_size)``.

    The runner stores the shape as the token the matrix was written with —
    ``scalar``, ``batch:N``, ``array:M`` — and nothing else, because the two
    sizes follow from it. This is the reader's half of that: the same three
    forms, with the named pixel counts resolved through
    :data:`NAMED_ARRAY_SIZES`. A token in none of those forms yields ``None``,
    so the row is skipped rather than plotted under a guessed shape.
    """
    text = str(token).strip().lower()
    if text == "scalar":
        return 1, 1
    kind, separator, size = text.partition(":")
    if not separator:
        return None
    if kind == "batch":
        return (int(size), 1) if size.isdigit() else None
    if kind == "array":
        if size in NAMED_ARRAY_SIZES:
            return 1, NAMED_ARRAY_SIZES[size]
        return (1, int(size)) if size.isdigit() else None
    return None


# What a recorded failure is shown as, longest-lived cause first. The document
# stores the worker's whole output — thousands of characters of traceback — and
# a chart has room for a phrase, so each cause is recognised by the sentence
# the failing stack actually prints. A failure matching none of them falls back
# to its last traceback line, which is where an unrecognised one names itself.
FAILURE_CAUSES: tuple[tuple[str, str], ...] = (
    ("ran out of memory", "the client ran out of memory"),
    ("BAD_OUT_OF_MEMORY", "the client ran out of memory"),
    # 0x80030000, the same BadOutOfMemory the two above report in words.
    ("2147680256", "the client ran out of memory"),
    ("client is disconnected", "the client's secure channel died"),
    ("BAD_SECURE_CHANNEL_CLOSED", "the client's secure channel died"),
    ("BAD_TIMEOUT", "the service call timed out"),
    ("BadInternalError", "the server answered BadInternalError"),
    ("pipelined array read failed", "the pipelined array read failed"),
    ("did not start", "the server would not start"),
)


def failure_cause(error: object) -> str:
    """One phrase for why a configuration could not be measured.

    See :data:`FAILURE_CAUSES`. The fallback is the last line of the stored
    output that starts a line of its own, which for a Python worker is the
    exception and for a C one the message it died with.
    """
    text = str(error or "")
    for needle, phrase in FAILURE_CAUSES:
        if needle in text:
            return phrase
    tail = [line for line in text.strip().splitlines() if line and not line.startswith((" ", "\t", "Traceback"))]
    return (tail[-1][:120] if tail else "no reason recorded").strip()


def load_document(database_path: Path) -> tuple[dict, dict, list[dict], list[dict]]:
    """Read ``database_path`` and flatten it into compact plotting records.

    A result row stores three things — the ``config_key`` it was measured
    under, the individual ``runs``, and the ``stats`` over them — so
    everything else a chart needs is derived here: the operation and mode, the
    pairing, the payload shape, and how short of its sample target the row is.
    The target is the suite's ``samples`` config option, which is the run's
    record of what ``run.py sample <n>`` asked for; it and the rows and
    failures themselves all live in the suite's slice of ``database_path``.
    """
    store = BenchDB(database_path, suite="throughput")
    metadata = store.stored_metadata
    uniform, _varying = store.get_config("throughput")
    requested = int(uniform.get("samples", 0) or 0)
    rows: list[dict] = []
    for entry in store.results.values():
        key = entry.get("config_key")
        stats = entry.get("stats")
        # A configuration can be stored with no usable sample at all — an
        # interrupted run flushes the row before the first one lands, leaving
        # an empty ``stats``. There is nothing to plot for it.
        if not isinstance(key, dict) or not stats:
            continue
        shape = payload_shape(key.get("payload"))
        if shape is None:
            continue
        batch, array = shape
        per_call = batch * array
        pair = str(key["pair"]).replace(":", "/")
        client, _, server = pair.partition("/")
        policy = str(key["security"])
        recorded = len(entry.get("runs") or [])
        rows.append(
            {
                "pair": pair,
                "client": client,
                "server": server,
                "op": str(key["operation"]),
                "mode": str(key["mode"]),
                "sec": "enc" if policy != "None" else "none",
                "policy": policy,
                "n": int(key["clients"]),
                "batch": batch,
                "array": array,
                "per_call": per_call,
                "shape": payload_key(batch, array),
                # Samples behind this row. A row short of its target is
                # provisional and is flagged the same way a wide spread is.
                "ns": recorded,
                "want": max(recorded, requested),
                "med": float(stats["median_ops_per_second"]),
                "lo": float(stats["min_ops_per_second"]),
                "hi": float(stats["max_ops_per_second"]),
                "mbmed": float(stats["median_mb_per_second"]),
                "mblo": float(stats["min_mb_per_second"]),
                "mbhi": float(stats["max_mb_per_second"]),
            }
        )
    if not rows:
        raise ValueError(f"{database_path}: no recognizable benchmark rows")

    # The configurations the run could not measure, keyed exactly as the rows
    # above are. Without them an empty cell has two meanings the reader cannot
    # tell apart: a pairing outside the matrix, and one that was measured and
    # fell over. The second is a result — often the most interesting one on a
    # large payload — so it is carried through and labelled rather than left as
    # a blank the charts silently skip.
    failures: list[dict] = []
    for entry in store.failures:
        key = entry.get("config_key") if isinstance(entry, dict) else None
        if not isinstance(key, dict):
            continue
        shape = payload_shape(key.get("payload"))
        if shape is None:
            continue
        batch, array = shape
        recorded = int(entry.get("samples_recorded") or 0)
        # A configuration with samples of its own is on the charts already; its
        # failure only says the run stopped short, which the row's own sample
        # count records. Only the ones with nothing to plot are gaps.
        if recorded:
            continue
        pair = str(key["pair"]).replace(":", "/")
        client, _, server = pair.partition("/")
        failures.append(
            {
                "pair": pair,
                "client": client,
                "server": server,
                "op": str(key["operation"]),
                "mode": str(key["mode"]),
                "sec": "enc" if str(key["security"]) != "None" else "none",
                "n": int(key["clients"]),
                "shape": payload_key(batch, array),
                "why": failure_cause(entry.get("error")),
            }
        )
    return metadata, uniform, rows, failures


def payload_key(batch: int, array: int) -> str:
    """Canonical id for a payload shape: scalar, a node batch, or one array."""
    if array > 1:
        return f"a{array}"
    if batch > 1:
        return f"b{batch}"
    return "s"


def payload_label(batch: int, array: int) -> str:
    if array > 1:
        for name, size in NAMED_ARRAY_SIZES.items():
            if size == array:
                return f"array {name.replace('_', ' ')}"
        return f"array {array:,}"
    if batch > 1:
        return f"batch {batch}"
    return "single value"


def payload_options(rows: list[dict]) -> list[dict]:
    """Every payload shape in the document, batches first then arrays."""
    seen: dict[str, dict] = {}
    for row in rows:
        if row["shape"] in seen:
            continue
        seen[row["shape"]] = {
            "key": row["shape"],
            "batch": row["batch"],
            "array": row["array"],
            "per_call": row["per_call"],
            "label": payload_label(row["batch"], row["array"]),
            "kind": "array" if row["array"] > 1 else "batch",
        }
    return sorted(seen.values(), key=lambda entry: (entry["kind"] == "array", entry["per_call"]))


def series_palette(rows: list[dict]) -> list[dict]:
    """Assign a fixed categorical slot to every pair present in the data.

    Slots follow :data:`PAIR_ORDER`, not measured speed, so hiding a series
    never repaints the survivors. Pairs outside the known matrix (a future
    addition) fall in after it and cycle through the available slots.
    """
    present = {row["pair"] for row in rows}
    ordered = [pair for pair in PAIR_ORDER if pair in present]
    ordered += sorted(present - set(PAIR_ORDER))
    entries = []
    for index, pair in enumerate(ordered):
        client, _, server = pair.partition("/")
        entries.append(
            {
                "pair": pair,
                "client": client,
                "server": server,
                "light": SERIES_LIGHT[PAIR_ORDER.index(pair) if pair in PAIR_ORDER else index % len(SERIES_LIGHT)],
                "dark": SERIES_DARK[PAIR_ORDER.index(pair) if pair in PAIR_ORDER else index % len(SERIES_DARK)],
            }
        )
    return entries


def base_layout(theme: str) -> dict:
    """Chart chrome shared by every figure, built (and validated) by plotly."""
    tokens = THEMES[theme]
    axis = {
        "gridcolor": tokens["grid"],
        "gridwidth": 1,
        "griddash": "solid",
        "linecolor": tokens["axis"],
        "linewidth": 1,
        "zerolinecolor": tokens["axis"],
        "zerolinewidth": 1,
        "tickfont": {"color": tokens["muted"], "size": 11},
        "title": {"font": {"color": tokens["secondary"], "size": 11}},
        "automargin": True,
    }
    layout = go.Layout(
        paper_bgcolor=tokens["surface"],
        plot_bgcolor=tokens["surface"],
        font={"family": FONT_STACK, "size": 12, "color": tokens["secondary"]},
        margin={"l": 8, "r": 24, "t": 8, "b": 8},
        showlegend=False,
        bargap=0.45,
        hoverlabel={
            "bgcolor": tokens["hover"],
            "bordercolor": tokens["axis"],
            "font": {"family": FONT_STACK, "size": 12, "color": tokens["primary"]},
            "align": "left",
        },
        xaxis=axis,
        yaxis=axis,
        transition={"duration": 0},
    )
    return layout.to_plotly_json()


def build_theme_payload() -> dict:
    payload = {}
    for name, tokens in THEMES.items():
        ramp = list(SEQUENTIAL) if name == "light" else list(reversed(SEQUENTIAL))
        payload[name] = {
            "tokens": tokens,
            "layout": base_layout(name),
            "sequential": [[index / (len(ramp) - 1), color] for index, color in enumerate(ramp)],
            # Fraction above which a cell fill is dark enough to need light text.
            "onFillFlip": 0.55 if name == "light" else 0.45,
            "onFillDark": tokens["primary"] if name == "light" else "#0b0b0b",
            "onFillLight": "#ffffff",
        }
    return payload


CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: __FONT__;
  background: var(--plane);
  color: var(--primary);
  -webkit-font-smoothing: antialiased;
}
.viz-root {
  min-height: 100vh;
  background: var(--plane);
  color: var(--primary);
  color-scheme: light;
  --surface: #fcfcfb; --plane: #f9f9f7; --primary: #0b0b0b; --secondary: #52514e;
  --muted: #898781; --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
  --accent: #2a78d6; --ghost: rgba(11,11,11,0.05);
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) .viz-root {
    color-scheme: dark;
    --surface: #1a1a19; --plane: #0d0d0d; --primary: #ffffff; --secondary: #c3c2b7;
    --muted: #898781; --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --accent: #3987e5; --ghost: rgba(255,255,255,0.07);
  }
}
:root[data-theme="dark"] .viz-root {
  color-scheme: dark;
  --surface: #1a1a19; --plane: #0d0d0d; --primary: #ffffff; --secondary: #c3c2b7;
  --muted: #898781; --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
  --accent: #3987e5; --ghost: rgba(255,255,255,0.07);
}
.wrap { max-width: 1180px; margin: 0 auto; padding: 28px 20px 72px; }
header.page { display: flex; gap: 20px; align-items: flex-start; flex-wrap: wrap; }
header.page h1 { font-size: 22px; font-weight: 600; margin: 0 0 6px; letter-spacing: -0.01em; }
header.page p { margin: 0; color: var(--secondary); font-size: 13px; line-height: 1.55; max-width: 68ch; }
.spacer { flex: 1 1 auto; }
button, select { font: inherit; color: inherit; }

/* ---- control row -------------------------------------------------------- */
.controls {
  position: sticky; top: 0; z-index: 20;
  margin: 22px 0 26px; padding: 12px 14px;
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  display: flex; flex-wrap: wrap; gap: 8px 22px; align-items: center;
  backdrop-filter: saturate(1.2) blur(6px);
}
.group { display: flex; align-items: center; gap: 8px; }
.group > .glabel {
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted);
}
.seg { display: inline-flex; border: 1px solid var(--border); border-radius: 7px; overflow: hidden; }
.seg button {
  border: 0; background: transparent; padding: 5px 10px; font-size: 12.5px;
  color: var(--secondary); cursor: pointer; line-height: 1.4;
}
.seg button + button { border-left: 1px solid var(--border); }
.seg button:hover:not(:disabled) { background: var(--ghost); }
.seg button[aria-pressed="true"] { background: var(--accent); color: #fff; }
.seg button:disabled { opacity: 0.38; cursor: not-allowed; }
.seg button:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }

/* ---- series legend ------------------------------------------------------ */
.legendbar {
  display: flex; flex-wrap: wrap; gap: 6px 8px; align-items: center;
  margin: -12px 0 26px; padding: 12px 14px;
  border: 1px solid var(--border); border-top: 0; border-radius: 0 0 10px 10px;
  background: var(--surface);
}
.chip {
  display: inline-flex; align-items: center; gap: 7px;
  border: 1px solid var(--border); border-radius: 999px; background: transparent;
  padding: 4px 11px 4px 8px; font-size: 12.5px; color: var(--primary); cursor: pointer;
  min-height: 26px;
}
.chip:hover { background: var(--ghost); }
.chip:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.chip .sw { width: 10px; height: 10px; border-radius: 3px; flex: 0 0 auto; }
.chip[aria-pressed="false"] { color: var(--muted); }
.chip[aria-pressed="false"] .sw { background: transparent !important; box-shadow: inset 0 0 0 1.5px var(--axis); }
.presets { display: flex; gap: 6px; margin-left: auto; }
.presets button {
  border: 1px solid var(--border); background: transparent; border-radius: 7px;
  padding: 4px 9px; font-size: 12px; color: var(--secondary); cursor: pointer;
}
.presets button:hover { background: var(--ghost); }

/* ---- chart cards -------------------------------------------------------- */
.card {
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  padding: 18px 18px 14px; margin-bottom: 22px;
}
.card > header { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 2px; }
.card h2 { font-size: 15px; font-weight: 600; margin: 0; }
.card .q { font-size: 12.5px; color: var(--muted); }
.card .scope { font-size: 12px; color: var(--muted); margin: 6px 0 12px; }
.card .scope .ign { text-decoration: line-through; opacity: 0.75; }
.card .scope b { font-weight: 600; color: var(--secondary); }
.card .caption {
  font-size: 13px; color: var(--secondary); line-height: 1.55; margin: 12px 2px 0;
  border-top: 1px solid var(--border); padding-top: 11px;
}
.card .tools { margin-left: auto; display: flex; gap: 6px; }
.card .tools button {
  border: 1px solid var(--border); background: transparent; border-radius: 7px;
  padding: 3px 9px; font-size: 12px; color: var(--secondary); cursor: pointer;
}
.card .tools button[aria-pressed="true"] { background: var(--accent); color: #fff; border-color: transparent; }
.plot { width: 100%; }
.tablewrap { overflow-x: auto; margin-top: 4px; }
table { border-collapse: collapse; width: 100%; font-size: 12.5px; }
th, td { text-align: right; padding: 6px 10px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
th:first-child, td:first-child { text-align: left; }
th { color: var(--muted); font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }
td { font-variant-numeric: tabular-nums; color: var(--secondary); }
td.key { color: var(--primary); }
td .sw { display: inline-block; width: 9px; height: 9px; border-radius: 2px; margin-right: 7px; }
.hidden { display: none !important; }
footer.page { color: var(--muted); font-size: 12px; line-height: 1.6; margin-top: 34px; }
footer.page code { font-size: 11.5px; }
.warnnote { color: var(--secondary); }
.warnnote .hatch {
  display: inline-block; width: 11px; height: 11px; vertical-align: -1px; margin-right: 4px;
  border: 1px solid var(--axis); border-radius: 2px;
  background: repeating-linear-gradient(45deg, var(--axis) 0 1.5px, transparent 1.5px 4px);
}
.empty { color: var(--muted); font-size: 13px; padding: 26px 2px; }
"""


CONTROLLER_JS = r"""
'use strict';

const DATA = __DATA__;
const THEME = __THEME__;
const ROWS = DATA.rows;
const SERIES = DATA.series;
const OPTIONS = DATA.options;
const BASELINE = DATA.baseline;

const PLOT_CONFIG = {
  responsive: true,
  displaylogo: false,
  modeBarButtonsToRemove: ['select2d', 'lasso2d', 'autoScale2d', 'toggleSpikelines'],
};

const IMPL_LABEL = __IMPL_LABELS__;
const POLICY_LABEL = { none: 'SecurityPolicy None', enc: 'Basic256Sha256' };
const colorOf = new Map(SERIES.map((s) => [s.pair, s]));
const nf0 = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });
// Slots 1 and 2 -- the read/write scatter is an all-pairs form, so it is capped
// at the three leading slots that validate pairwise; it uses two.
const SERIES_LIGHT_0 = '__S_L0__', SERIES_LIGHT_1 = '__S_L1__';
const SERIES_DARK_0 = '__S_D0__', SERIES_DARK_1 = '__S_D1__';

/* ------------------------------------------------------------------ state */

const DEFAULTS = {
  op: OPTIONS.op.includes('read') ? 'read' : OPTIONS.op[0],
  mode: OPTIONS.mode.includes('async') ? 'async' : OPTIONS.mode[0],
  sec: OPTIONS.sec.includes('none') ? 'none' : OPTIONS.sec[0],
  n: OPTIONS.n[OPTIONS.n.length - 1],
  payload: OPTIONS.payloads.some((p) => p.key === 's') ? 's' : OPTIONS.payloads[0].key,
  metric: 'ops',
  scale: 'abs',
  axis: 'linear',
  whiskers: true,
  ratio: OPTIONS.sec.length > 1 ? 'security' : (OPTIONS.mode.length > 1 ? 'pipeline' : 'baseline'),
  hidden: [],
};
const state = Object.assign({}, DEFAULTS);
const tableOpen = new Set();

function readHash() {
  const raw = location.hash.replace(/^#/, '');
  if (!raw) return;
  for (const part of raw.split('&')) {
    const [key, value] = part.split('=');
    if (value === undefined) continue;
    const decoded = decodeURIComponent(value);
    if (key === 'n') { const v = parseInt(decoded, 10); if (OPTIONS.n.includes(v)) state.n = v; }
    else if (key === 'hide') state.hidden = decoded ? decoded.split(',') : [];
    else if (key === 'whiskers') state.whiskers = decoded === '1';
    else if (key in state) state[key] = decoded;
  }
  if (!OPTIONS.op.includes(state.op)) state.op = DEFAULTS.op;
  if (!OPTIONS.mode.includes(state.mode)) state.mode = DEFAULTS.mode;
  if (!OPTIONS.sec.includes(state.sec)) state.sec = DEFAULTS.sec;
  if (!OPTIONS.payloads.some((p) => p.key === state.payload)) {
    state.payload = OPTIONS.payloads[0].key;
  }
}

function writeHash() {
  const parts = [
    'op=' + state.op, 'mode=' + state.mode, 'sec=' + state.sec, 'n=' + state.n,
    'payload=' + state.payload,
    'metric=' + state.metric, 'scale=' + state.scale, 'axis=' + state.axis,
    'whiskers=' + (state.whiskers ? '1' : '0'), 'ratio=' + state.ratio,
  ];
  if (state.hidden.length) parts.push('hide=' + encodeURIComponent(state.hidden.join(',')));
  history.replaceState(null, '', '#' + parts.join('&'));
}

/* ------------------------------------------------------- data + formatting */

const index = new Map();
for (const row of ROWS) {
  index.set([row.pair, row.op, row.mode, row.sec, row.n, row.shape].join('|'), row);
}
// The configurations that were measured and failed, keyed the same way. Kept
// apart from `index` so no chart can plot one by accident: they carry a reason,
// not a number.
const failedIndex = new Map();
for (const entry of (DATA.failures || [])) {
  failedIndex.set([entry.pair, entry.op, entry.mode, entry.sec, entry.n, entry.shape].join('|'), entry);
}
const failedAt = (pair, op, mode, sec, n, shape) =>
  failedIndex.get([pair, op, mode, sec, n, shape === undefined ? state.payload : shape].join('|'));
const at = (pair, op, mode, sec, n, shape) =>
  index.get([pair, op, mode, sec, n, shape === undefined ? state.payload : shape].join('|'));

const visiblePairs = () => SERIES.filter((s) => !state.hidden.includes(s.pair)).map((s) => s.pair);
const isLatency = () => state.metric === 'lat' && state.mode === 'sync';
const isData = () => state.metric === 'data';
/** A median is provisional for either reason: too few samples, or too wide a set. */
const wideSpread = (row) => row.hi > 2 * row.lo;
const shortSamples = (row) => row.ns < row.want;
const unstable = (row) => wideSpread(row) || shortSamples(row);
const shapeOf = (key) => OPTIONS.payloads.find((p) => p.key === key) || OPTIONS.payloads[0];

/** Median in the selected metric space: calls/s, MB/s, or µs per call. */
function metricOf(row) {
  if (isLatency()) return 1e6 / row.med;
  return isData() ? row.mbmed : row.med;
}

/** [low, median, high] in metric space, ordered ascending. */
function bandOf(row) {
  const lo = isData() ? row.mblo : row.lo;
  const hi = isData() ? row.mbhi : row.hi;
  const a = isLatency() ? 1e6 / row.hi : lo;
  const b = isLatency() ? 1e6 / row.lo : hi;
  return [Math.min(a, b), metricOf(row), Math.max(a, b)];
}

/** The plotted value: absolute metric, or the same metric over the C baseline. */
function plotted(row) {
  if (state.scale !== 'rel') return metricOf(row);
  const base = at(BASELINE, row.op, row.mode, row.sec, row.n, row.shape);
  return base ? metricOf(row) / metricOf(base) : NaN;
}

function plottedBand(row) {
  const band = bandOf(row);
  if (state.scale !== 'rel') return band;
  const base = at(BASELINE, row.op, row.mode, row.sec, row.n, row.shape);
  if (!base) return [NaN, NaN, NaN];
  const divisor = metricOf(base);
  return band.map((v) => v / divisor);
}

function unitLabel() {
  if (state.scale === 'rel') return isLatency() ? '× baseline latency' : '× C baseline';
  if (isLatency()) return 'µs per service call';
  return isData() ? 'megabytes per second' : 'service calls per second';
}

/** A MB/s figure. The scale spans thousandths of a megabyte per second (one
    Int32 per call) to thousands (a 4k array), so a fixed number of decimals is
    either noise at the top or a bare 0 at the bottom; pick it by magnitude. */
function fmtMb(v) {
  if (!isFinite(v)) return '—';
  if (v >= 100) return nf0.format(Math.round(v));
  if (v >= 1) return v.toFixed(1);
  if (v >= 0.01) return v.toFixed(3);
  return v.toPrecision(2);
}

function fmtValue(v) {
  if (!isFinite(v)) return '—';
  if (state.scale === 'rel') return (v < 0.1 ? v.toFixed(3) : v.toFixed(2)) + '×';
  if (isLatency()) return (v >= 100 ? nf0.format(Math.round(v)) : v.toFixed(1)) + ' µs';
  if (isData()) return fmtMb(v);
  return nf0.format(Math.round(v));
}

function fmtRatio(v) {
  if (!isFinite(v)) return '—';
  return (v >= 10 ? v.toFixed(0) : v >= 1 ? v.toFixed(2) : v.toFixed(2)) + '×';
}

const pairLabel = (pair) => {
  const s = colorOf.get(pair);
  return s ? IMPL_LABEL[s.client] + ' → ' + IMPL_LABEL[s.server] : pair;
};

/* --------------------------------------------------------------- theming */

function activeTheme() {
  const stamped = document.documentElement.getAttribute('data-theme');
  if (stamped === 'dark' || stamped === 'light') return stamped;
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}
const tokens = () => THEME[activeTheme()].tokens;
const hue = (pair) => {
  const s = colorOf.get(pair);
  return s ? s[activeTheme()] : tokens().muted;
};

/** Deep-merge chart-specific layout bits onto the themed chrome. */
function layoutFor(overrides) {
  const merge = (target, source) => {
    for (const key of Object.keys(source)) {
      const value = source[key];
      if (value && typeof value === 'object' && !Array.isArray(value)) {
        target[key] = merge(Object.assign({}, target[key] || {}), value);
      } else {
        target[key] = value;
      }
    }
    return target;
  };
  const base = JSON.parse(JSON.stringify(THEME[activeTheme()].layout));
  // The chrome carries one axis spec; charts that use both need it on each.
  base.yaxis = merge(JSON.parse(JSON.stringify(base.xaxis)), base.yaxis || {});
  return merge(base, overrides || {});
}

/** A hatched fill marks a measurement whose max exceeded twice its min. */
function patternFor(rows) {
  return {
    shape: rows.map((row) => (unstable(row) ? '/' : '')),
    fgcolor: tokens().surface,
    size: 5,
    solidity: 0.28,
  };
}

/* ---------------------------------------------------------------- charts */

/** 1. Ranking bar -- who is fastest in the selected slice. */
function chartRanking() {
  const rows = visiblePairs()
    .map((pair) => at(pair, state.op, state.mode, state.sec, state.n))
    .filter(Boolean);
  if (!rows.length) return null;
  rows.sort((a, b) => (isLatency() ? plotted(a) - plotted(b) : plotted(b) - plotted(a)));
  // Best on top: plotly stacks the first category at the bottom of a bar chart.
  rows.reverse();

  const values = rows.map(plotted);
  const bands = rows.map(plottedBand);
  const log = state.axis === 'log';
  const labels = rows.map((row) => pairLabel(row.pair));

  const trace = {
    type: 'bar',
    orientation: 'h',
    x: values,
    y: labels,
    marker: {
      color: rows.map((row) => hue(row.pair)),
      pattern: patternFor(rows),
      line: { width: 0 },

    },
    width: rows.map(() => 0.5),
    text: values.map(fmtValue),
    textposition: 'outside',
    cliponaxis: false,
    textfont: { color: tokens().secondary, size: 11.5 },
    // An unstable measurement always shows its range, whatever the toggle says.
    error_x: {
      type: 'data',
      symmetric: false,
      array: bands.map((band, i) => (state.whiskers || unstable(rows[i]) ? band[2] - values[i] : 0)),
      arrayminus: bands.map((band, i) => (state.whiskers || unstable(rows[i]) ? values[i] - band[0] : 0)),
      color: tokens().muted,
      thickness: 1,
      width: 4,
      visible: true,
    },
    customdata: rows.map((row, i) => [fmtValue(bands[i][0]), fmtValue(bands[i][2]), row.pair]),
    hovertemplate:
      '<b>%{text}</b><br>%{y}<br><span style="color:' + tokens().muted +
      '">range %{customdata[0]} – %{customdata[1]}</span><extra></extra>',
  };

  const top = Math.max(...bands.map((b) => b[2]));
  const layout = layoutFor({
    height: rows.length * 46 + 74,
    margin: { l: 8, r: 96, t: 10, b: 42 },
    xaxis: {
      type: log ? 'log' : 'linear',
      title: { text: unitLabel() + (log ? ' (log scale — bar length is not proportional)' : '') },
      tickformat: state.scale === 'rel' ? '' : '~s',
      rangemode: 'tozero',
      range: log ? undefined : [0, top * 1.16],
    },
    yaxis: { showgrid: false, ticksuffix: '  ', automargin: true },
  });

  return {
    data: [trace],
    layout,
    caption: captionRanking(rows.slice().reverse()),
    table: {
      columns: ['Pairing', 'Median', 'Min', 'Max', 'Spread', 'Samples'],
      rows: rows.slice().reverse().map((row) => {
        const band = plottedBand(row);
        return {
          key: row.pair,
          cells: [
            pairLabel(row.pair), fmtValue(plotted(row)), fmtValue(band[0]), fmtValue(band[2]),
            (row.hi / row.lo).toFixed(2) + '×' + (wideSpread(row) ? ' ⚠' : ''),
            row.ns + '/' + row.want + (shortSamples(row) ? ' ⚠' : ''),
          ],
        };
      }),
    },
  };
}

function captionRanking(rows) {
  if (!rows.length) return '';
  const best = rows[0];
  const parts = [sliceWords() + ': ' + pairLabel(best.pair) + ' leads at ' + fmtValue(plotted(best)) + '.'];
  if (rows.length > 1) {
    const next = rows[1];
    const factor = isLatency() ? plotted(next) / plotted(best) : plotted(best) / plotted(next);
    parts.push(pairLabel(next.pair) + ' follows at ' + fmtValue(plotted(next)) +
      ' (' + factor.toFixed(2) + '× ' + (isLatency() ? 'the latency' : 'off the lead') + ').');
  }
  const wide = rows.filter(wideSpread).length;
  if (wide) parts.push(wide === 1
    ? 'One hatched bar had a fastest sample above twice its slowest — read that median with its whiskers.'
    : wide + ' hatched bars had a fastest sample above twice their slowest — read those medians with their whiskers.');
  const short = rows.filter(shortSamples).length;
  if (short) parts.push(short === 1
    ? 'One bar holds fewer samples than the run asked for; its median is provisional.'
    : short + ' bars hold fewer samples than the run asked for; those medians are provisional.');
  const hiddenCount = SERIES.length - rows.length;
  if (hiddenCount > 0) parts.push(hiddenCount + ' pairing' + (hiddenCount > 1 ? 's' : '') + ' hidden; the axis is rescaled to what is shown.');
  return parts.join(' ');
}

/** 2. Heatmap -- is the cost on the client side or the server side? */
function chartHeatmap() {
  const impls = OPTIONS.impl;
  const z = [], text = [], hoverText = [];
  for (const client of impls) {
    const zrow = [], trow = [], hrow = [];
    for (const server of impls) {
      const pair = client + '/' + server;
      const row = at(pair, state.op, state.mode, state.sec, state.n);
      const failed = row ? null : failedAt(pair, state.op, state.mode, state.sec, state.n);
      const value = row ? plotted(row) : NaN;
      zrow.push(row && isFinite(value) ? Math.log10(value) : null);
      // '✕' is a configuration that ran and fell over, '—' one that was never
      // in the matrix. Both are empty cells; only one of them is a result.
      trow.push(row ? fmtValue(value) : (failed ? '✕' : '—'));
      hrow.push(row
        ? fmtValue(value) + '<br>' + IMPL_LABEL[client] + ' client → ' + IMPL_LABEL[server] + ' server'
        : IMPL_LABEL[client] + ' → ' + IMPL_LABEL[server] + '<br>' +
          (failed ? 'measured, but it failed: ' + failed.why : 'not in the benchmark matrix'));
    }
    z.push(zrow); text.push(trow); hoverText.push(hrow);
  }
  const flat = z.flat().filter((v) => v !== null);
  if (!flat.length) return null;
  const zmin = Math.min(...flat), zmax = Math.max(...flat);
  const span = zmax - zmin || 1;
  const conf = THEME[activeTheme()];

  const annotations = [];
  for (let r = 0; r < impls.length; r++) {
    for (let c = 0; c < impls.length; c++) {
      const raw = z[r][c];
      const fraction = raw === null ? 0 : (raw - zmin) / span;
      annotations.push({
        x: IMPL_LABEL[impls[c]], y: IMPL_LABEL[impls[r]], text: text[r][c],
        showarrow: false,
        font: {
          size: raw === null ? 12 : 13,
          color: raw === null ? tokens().muted
            : (fraction > conf.onFillFlip ? conf.onFillLight : conf.onFillDark),
          family: '__FONT__',
        },
      });
    }
  }

  const ticks = niceLogTicks(zmin, zmax);
  const trace = {
    type: 'heatmap',
    z, x: impls.map((i) => IMPL_LABEL[i]), y: impls.map((i) => IMPL_LABEL[i]),
    colorscale: conf.sequential,
    zmin, zmax,
    xgap: 2, ygap: 2,
    hoverongaps: false,
    text: hoverText,
    hovertemplate: '<b>%{text}</b><extra></extra>',
    colorbar: {
      thickness: 10, len: 0.82, outlinewidth: 0, ticklen: 3,
      tickvals: ticks.values, ticktext: ticks.labels,
      tickfont: { color: tokens().muted, size: 11 },
      title: { text: unitLabel(), font: { color: tokens().secondary, size: 11 }, side: 'right' },
    },
  };

  const layout = layoutFor({
    height: 316,
    margin: { l: 8, r: 8, t: 52, b: 14 },
    annotations,
    xaxis: { title: { text: 'Server implementation' }, side: 'top', showgrid: false, zeroline: false, ticks: '' },
    yaxis: { title: { text: 'Client implementation' }, showgrid: false, zeroline: false, ticks: '', autorange: 'reversed' },
  });

  return { data: [trace], layout, caption: captionHeatmap(impls), table: tableHeatmap(impls) };
}

function niceLogTicks(zmin, zmax) {
  const values = [], labels = [];
  for (let exp = Math.floor(zmin); exp <= Math.ceil(zmax); exp++) {
    for (const mult of [1, 2, 5]) {
      const value = Math.log10(mult * Math.pow(10, exp));
      if (value < zmin - 0.02 || value > zmax + 0.02) continue;
      values.push(value);
      labels.push(fmtValue(mult * Math.pow(10, exp)));
    }
  }
  return { values, labels };
}

function captionHeatmap(impls) {
  const cells = [];
  for (const client of impls) {
    for (const server of impls) {
      const row = at(client + '/' + server, state.op, state.mode, state.sec, state.n);
      if (row) cells.push({ client, server, v: plotted(row) });
    }
  }
  if (!cells.length) return '';
  const better = (a, b) => (isLatency() ? a.v < b.v : a.v > b.v);
  const best = cells.reduce((a, b) => (better(a, b) ? a : b));
  const parts = ['Read across a row to price the client, down a column to price the server. ' +
    'Fastest cell: ' + IMPL_LABEL[best.client] + ' → ' + IMPL_LABEL[best.server] +
    ' at ' + fmtValue(best.v) + '.'];
  // Same server, swapped client -- the cleanest available client-cost read.
  const anchor = 'open62541';
  const sameServer = cells.filter((c) => c.server === anchor && c.client !== anchor);
  if (sameServer.length) {
    const self = cells.find((c) => c.client === anchor && c.server === anchor);
    if (self) {
      const worded = sameServer.map((c) => IMPL_LABEL[c.client] + ' ' +
        (isLatency() ? (c.v / self.v).toFixed(1) + '× slower' : (self.v / c.v).toFixed(1) + '× slower'));
      parts.push('Against the same C server, swapping only the client costs: ' + worded.join(', ') + '.');
    }
  }
  const failed = failedHere(impls);
  if (failed.length) {
    parts.push(failed.length + ' pairing' + (failed.length > 1 ? 's' : '') +
      ' could not be measured at this setting (✕): ' + failedWorded(failed) + '.');
  }
  return parts.join(' ');
}

/** The failures at the current setting, over the pairings the heatmap draws. */
function failedHere(impls) {
  const out = [];
  for (const client of impls) {
    for (const server of impls) {
      const pair = client + '/' + server;
      if (at(pair, state.op, state.mode, state.sec, state.n)) continue;
      const failed = failedAt(pair, state.op, state.mode, state.sec, state.n);
      if (failed) out.push(failed);
    }
  }
  return out;
}

/** "o6\\Python → C and one other, the client ran out of memory" -- grouped by reason. */
function failedWorded(failed) {
  const byReason = new Map();
  for (const entry of failed) {
    if (!byReason.has(entry.why)) byReason.set(entry.why, []);
    byReason.get(entry.why).push(pairLabel(entry.pair));
  }
  return [...byReason].map(([why, pairs]) => pairs.join(', ') + ' — ' + why).join('; ');
}

function tableHeatmap(impls) {
  const rows = [];
  for (const client of impls) {
    for (const server of impls) {
      const pair = client + '/' + server;
      const row = at(pair, state.op, state.mode, state.sec, state.n);
      if (row) {
        const band = plottedBand(row);
        rows.push({
          key: row.pair,
          cells: [IMPL_LABEL[client], IMPL_LABEL[server], fmtValue(plotted(row)), fmtValue(band[0]), fmtValue(band[2])],
        });
        continue;
      }
      // A pairing that was measured and failed belongs in the table too, with
      // the reason where its median would be. Left out, the table reads as a
      // matrix that never included it.
      const failed = failedAt(pair, state.op, state.mode, state.sec, state.n);
      if (failed) {
        rows.push({ key: failed.pair, cells: [IMPL_LABEL[client], IMPL_LABEL[server], '✕ ' + failed.why, '—', '—'] });
      }
    }
  }
  return { columns: ['Client', 'Server', 'Median', 'Min', 'Max'], rows };
}

/** 3. Scaling -- throughput against the number of concurrent client processes. */
function chartScaling() {
  const pairs = visiblePairs();
  const counts = OPTIONS.n;
  if (counts.length < 2) {
    return { empty: 'This dataset was measured at a single client count (' + counts[0] +
      '), so there is no load curve to draw. Re-run with --clients 1,3,10.' };
  }
  const traces = [], annotations = [];
  const endpoints = [];

  for (const pair of pairs) {
    const xs = [], ys = [], custom = [];
    for (const n of counts) {
      const row = at(pair, state.op, state.mode, state.sec, n);
      if (!row) continue;
      const band = plottedBand(row);
      xs.push(n); ys.push(plotted(row));
      custom.push([fmtValue(band[0]), fmtValue(band[2]), unstable(row) ? ' ⚠' : '']);
    }
    if (!xs.length) continue;
    traces.push({
      type: 'scatter', mode: 'lines+markers', name: pairLabel(pair),
      x: xs, y: ys, customdata: custom,
      line: { color: hue(pair), width: 2, shape: 'linear', dash: pair.includes('node-opcua') ? 'dashdot' : pair.includes('ua-dotnet') ? 'dash' : 'solid' },
      marker: { size: 9, color: hue(pair), symbol: pair.includes('node-opcua') ? 'square' : 'circle', line: { color: tokens().surface, width: 2 } },
      hovertemplate: '<b>%{y:,.0f}</b> — ' + pairLabel(pair) +
        '<br><span style="color:' + tokens().muted + '">range %{customdata[0]} – %{customdata[1]}%{customdata[2]}</span><extra></extra>',
    });
    endpoints.push({ pair, x: xs[xs.length - 1], y: ys[ys.length - 1], first: ys[0] });
  }
  if (!traces.length) return null;

  // Direct labels only where they can be read: everything when few series are
  // shown, otherwise just the extremes. Converging labels are worse than none.
  let labelled = endpoints;
  if (endpoints.length > 4) {
    const sorted = endpoints.slice().sort((a, b) => a.y - b.y);
    labelled = [sorted[0], sorted[sorted.length - 1]];
  }
  for (const point of labelled) {
    annotations.push({
      x: Math.log10(point.x), y: Math.log10(point.y),
      text: '  ' + pairLabel(point.pair), showarrow: false, xanchor: 'left',
      font: { color: tokens().secondary, size: 11, family: '__FONT__' },
    });
  }

  // Isolating one pairing earns its ideal-scaling reference. Only throughput
  // has an "N clients should be N times as much" ideal -- latency does not.
  const shapes = [];
  if (endpoints.length === 1 && !isLatency() && state.scale === 'abs') {
    const only = endpoints[0];
    const x0 = counts[0], x1 = counts[counts.length - 1];
    shapes.push({
      type: 'line', xref: 'x', yref: 'y',
      x0: Math.log10(x0), y0: Math.log10(only.first),
      x1: Math.log10(x1), y1: Math.log10(only.first * (x1 / x0)),
      line: { color: tokens().muted, width: 1.5, dash: 'dot' },
    });
    annotations.push({
      x: Math.log10(x1), y: Math.log10(only.first * (x1 / x0)),
      text: 'linear scaling  ', showarrow: false, xanchor: 'right', yanchor: 'bottom',
      font: { color: tokens().muted, size: 11, family: '__FONT__' },
    });
  }

  const layout = layoutFor({
    height: 380,
    margin: { l: 8, r: 190, t: 12, b: 46 },
    hovermode: 'x unified',
    shapes,
    annotations,
    xaxis: {
      type: 'log', title: { text: 'concurrent client processes' },
      tickvals: counts, ticktext: counts.map(String), showgrid: true,
    },
    yaxis: { type: 'log', title: { text: unitLabel() }, tickformat: '~s' },
  });

  return {
    data: traces, layout,
    caption: captionScaling(endpoints, counts),
    table: {
      columns: ['Pairing'].concat(counts.map((n) => n + ' client' + (n > 1 ? 's' : ''))).concat(['Scaling']),
      rows: pairs.map((pair) => {
        const cells = [pairLabel(pair)];
        const values = counts.map((n) => {
          const row = at(pair, state.op, state.mode, state.sec, n);
          return row ? plotted(row) : NaN;
        });
        values.forEach((v) => cells.push(fmtValue(v)));
        cells.push(isFinite(values[0]) && isFinite(values[values.length - 1])
          ? (values[values.length - 1] / values[0]).toFixed(2) + '×' : '—');
        return { key: pair, cells };
      }),
    },
  };
}

function captionScaling(endpoints, counts) {
  if (!endpoints.length) return '';
  const first = counts[0], last = counts[counts.length - 1];
  if (isLatency()) {
    const worst = endpoints.reduce((a, b) => (b.y / b.first > a.y / a.first ? b : a));
    return 'Per-operation latency from ' + first + ' to ' + last + ' clients. ' +
      pairLabel(worst.pair) + ' degrades most, ×' + (worst.y / worst.first).toFixed(2) +
      ' — sync latency under contention, not a throughput ceiling.';
  }
  const factors = endpoints.map((p) => ({ pair: p.pair, factor: p.y / p.first }));
  factors.sort((a, b) => b.factor - a.factor);
  const best = factors[0], worst = factors[factors.length - 1];
  const parts = ['From ' + first + ' to ' + last +
    ' clients (perfect scaling would be ' + (last / first).toFixed(0) + '×): ' +
    pairLabel(best.pair) + ' gains ' + best.factor.toFixed(2) + '×'];
  if (factors.length > 1) {
    parts[0] += ', ' + pairLabel(worst.pair) + ' ' +
      (worst.factor < 1 ? 'loses throughput and ends at ' + (worst.factor * 100).toFixed(0) + '% of its 1-client rate'
        : 'gains only ' + worst.factor.toFixed(2) + '×');
  }
  parts[0] += '.';
  const saturated = factors.filter((f) => f.factor >= 1 && f.factor < 1.15).length;
  const regressed = factors.filter((f) => f.factor < 1).length;
  if (saturated) parts.push(saturated + ' pairing' + (saturated > 1 ? 's are' : ' is') +
    ' already saturated at ' + first + ' client' + (first > 1 ? 's' : '') + ' — more clients buy nothing.');
  if (regressed) parts.push(regressed + ' pairing' + (regressed > 1 ? 's go' : ' goes') +
    ' backwards under load.');
  if (endpoints.length > 1 && state.scale === 'abs') {
    parts.push('Isolate a single pairing to see its linear-scaling reference.');
  }
  return parts.join(' ');
}

/** 4. Ratio bars -- what one feature costs, as a multiple of throughput. */
const RATIO_SPECS = {
  security: {
    label: 'Encryption cost',
    need: () => OPTIONS.sec.length > 1,
    axis: 'Basic256Sha256 ÷ SecurityPolicy None',
    overrides: 'both security policies',
    of: (pair) => {
      const a = at(pair, state.op, state.mode, 'enc', state.n);
      const b = at(pair, state.op, state.mode, 'none', state.n);
      return a && b ? a.med / b.med : NaN;
    },
  },
  pipeline: {
    label: 'Pipelining gain',
    need: () => OPTIONS.mode.length > 1,
    axis: 'async (32 outstanding) ÷ sync (1 outstanding)',
    overrides: 'both request modes',
    of: (pair) => {
      const a = at(pair, state.op, 'async', state.sec, state.n);
      const b = at(pair, state.op, 'sync', state.sec, state.n);
      return a && b ? a.med / b.med : NaN;
    },
  },
  baseline: {
    label: 'Against C',
    need: () => true,
    axis: 'pairing ÷ open62541 C client and server',
    overrides: null,
    of: (pair) => {
      const a = at(pair, state.op, state.mode, state.sec, state.n);
      const b = at(BASELINE, state.op, state.mode, state.sec, state.n);
      return a && b ? a.med / b.med : NaN;
    },
  },
};

function chartRatio() {
  const spec = RATIO_SPECS[state.ratio] || RATIO_SPECS.baseline;
  if (!spec.need()) return { empty: 'This dataset holds only one ' +
    (state.ratio === 'security' ? 'security policy' : 'request mode') + ', so the ratio has nothing to divide.' };
  const entries = visiblePairs()
    .map((pair) => ({ pair, ratio: spec.of(pair) }))
    .filter((e) => isFinite(e.ratio) && e.ratio > 0);
  if (!entries.length) return null;
  entries.sort((a, b) => a.ratio - b.ratio);

  // Bars grow from 1.0 in log2 space, so halving and doubling read symmetric
  // and the sign of the bar is the sign of the effect.
  const xs = entries.map((e) => Math.log2(e.ratio));
  const reach = Math.max(0.6, ...xs.map((v) => Math.abs(v) * 1.35));
  const ticks = [];
  for (let exp = Math.ceil(-reach); exp <= Math.floor(reach); exp++) ticks.push(exp);

  const trace = {
    type: 'bar', orientation: 'h',
    x: xs, y: entries.map((e) => pairLabel(e.pair)),
    marker: { color: entries.map((e) => hue(e.pair)), line: { width: 0 } },
    width: entries.map(() => 0.55),
    text: entries.map((e) => fmtRatio(e.ratio)),
    textposition: 'outside',
    cliponaxis: false,
    textfont: { color: tokens().secondary, size: 11.5 },
    customdata: entries.map((e) => [fmtRatio(e.ratio), pairLabel(e.pair)]),
    hovertemplate: '<b>%{customdata[0]}</b><br>%{customdata[1]}<br>' +
      '<span style="color:' + tokens().muted + '">' + spec.axis + '</span><extra></extra>',
  };

  const layout = layoutFor({
    height: entries.length * 46 + 82,
    margin: { l: 8, r: 90, t: 26, b: 46 },
    xaxis: {
      title: { text: spec.axis },
      range: [-reach, reach],
      tickvals: ticks,
      ticktext: ticks.map((t) => fmtRatio(Math.pow(2, t))),
      zeroline: true, zerolinecolor: tokens().secondary, zerolinewidth: 1.5,
    },
    yaxis: { showgrid: false, ticksuffix: '  ' },
    annotations: [
      { x: -reach, y: 1.06, xref: 'x', yref: 'paper', text: '← worse', showarrow: false,
        xanchor: 'left', font: { color: tokens().muted, size: 11, family: '__FONT__' } },
      { x: reach, y: 1.06, xref: 'x', yref: 'paper', text: 'better →', showarrow: false,
        xanchor: 'right', font: { color: tokens().muted, size: 11, family: '__FONT__' } },
    ],
  });

  return {
    data: [trace], layout,
    caption: captionRatio(spec, entries),
    table: {
      columns: ['Pairing', spec.label, 'Change'],
      rows: entries.slice().reverse().map((e) => ({
        key: e.pair,
        cells: [pairLabel(e.pair), fmtRatio(e.ratio),
          (e.ratio >= 1 ? '+' : '') + ((e.ratio - 1) * 100).toFixed(0) + '%'],
      })),
    },
  };
}

function captionRatio(spec, entries) {
  const best = entries[entries.length - 1], worst = entries[0];
  const median = entries[Math.floor(entries.length / 2)].ratio;
  const parts = [spec.label + ' at ' + sliceWords(RATIO_DIMENSION[state.ratio]) + '. ' +
    'Median across the shown pairings ' + fmtRatio(median) + '; best ' +
    pairLabel(best.pair) + ' at ' + fmtRatio(best.ratio) + ', worst ' +
    pairLabel(worst.pair) + ' at ' + fmtRatio(worst.ratio) + '.'];
  if (state.ratio === 'security') {
    const lost = entries.filter((e) => e.ratio < 0.9).length;
    parts.push(lost ? lost + ' of ' + entries.length + ' pairings lose more than 10% to SignAndEncrypt.'
      : 'No shown pairing loses more than 10% to SignAndEncrypt.');
  }
  if (state.ratio === 'pipeline') {
    const flat = entries.filter((e) => e.ratio < 1.2).length;
    if (flat) parts.push(flat + ' pairing' + (flat > 1 ? 's gain' : ' gains') +
      ' almost nothing from 32 outstanding requests — the server side is serialising them.');
  }
  return parts.join(' ');
}

/** Least-squares fit of log(y) against log(x): y = a * x^b.
 *
 * The exponent b is the whole point of the fit. Values moved per second grows
 * as payload^b, so b = 1 means a bigger payload is free (the per-call cost
 * vanishes) and b = 0 means it buys nothing at all. */
function powerFit(xs, ys) {
  const points = xs.map((x, i) => [Math.log(x), Math.log(ys[i])])
    .filter(([lx, ly]) => isFinite(lx) && isFinite(ly));
  if (points.length < 2) return null;
  const n = points.length;
  const mx = points.reduce((s, p) => s + p[0], 0) / n;
  const my = points.reduce((s, p) => s + p[1], 0) / n;
  let num = 0, den = 0;
  for (const [lx, ly] of points) { num += (lx - mx) * (ly - my); den += (lx - mx) ** 2; }
  if (den === 0) return null;
  const b = num / den;
  const a = Math.exp(my - b * mx);
  // Coefficient of determination, so a fit that does not describe the data
  // can be recognised as such rather than read as a law.
  let ssRes = 0, ssTot = 0;
  for (const [lx, ly] of points) {
    ssRes += (ly - (Math.log(a) + b * lx)) ** 2;
    ssTot += (ly - my) ** 2;
  }
  return { a, b, r2: ssTot === 0 ? 1 : 1 - ssRes / ssTot };
}

/** 5. Payload scaling -- what batching and arrays actually buy. */
function chartPayload() {
  const shapes = OPTIONS.payloads;
  if (shapes.length < 2) {
    return { empty: 'This dataset holds a single payload shape. Re-run with ' +
      '--batchsize 1,10,100,1000 and --arraysize 1000,hd to draw the curve.' };
  }
  const palette = tokens();
  const traces = [], annotations = [], notes = [];

  // Batch and array sweeps share one x axis -- values carried by a single
  // service call -- so the two ways of moving more per call are directly
  // comparable. Line style, not colour, separates them: colour stays identity.
  const KINDS = [
    { kind: 'batch', dash: 'solid', label: 'nodes per call' },
    { kind: 'array', dash: 'dash', label: 'elements per node' },
  ];

  for (const pair of visiblePairs()) {
    for (const { kind, dash } of KINDS) {
      const picked = shapes.filter((s) => s.kind === kind || s.key === 's');
      const xs = [], ys = [], custom = [];
      for (const shape of picked) {
        const row = at(pair, state.op, state.mode, state.sec, state.n, shape.key);
        if (!row || !(row.mbmed > 0)) continue;
        xs.push(shape.per_call);
        ys.push(row.mbmed);
        custom.push([shape.label, nf0.format(Math.round(row.med)), fmtMb(row.mbmed)]);
      }
      if (xs.length < 2) continue;
      traces.push({
        type: 'scatter', mode: 'lines+markers', name: pairLabel(pair) + ' · ' + kind,
        x: xs, y: ys, customdata: custom,
        line: { color: hue(pair), width: 2, dash },
        marker: { size: 9, color: hue(pair), symbol: kind === 'array' ? 'diamond' : 'circle',
                  line: { color: palette.surface, width: 2 } },
        hovertemplate: '<b>%{customdata[2]}</b> MB/s — ' + pairLabel(pair) +
          '<br>%{customdata[0]} · %{x:,} per call · %{customdata[1]} calls/s<extra></extra>',
      });
      const fit = powerFit(xs, ys);
      if (fit && fit.r2 >= 0.5) {
        const x0 = Math.min(...xs), x1 = Math.max(...xs);
        traces.push({
          type: 'scatter', mode: 'lines', showlegend: false, hoverinfo: 'skip',
          x: [x0, x1], y: [fit.a * Math.pow(x0, fit.b), fit.a * Math.pow(x1, fit.b)],
          line: { color: hue(pair), width: 1, dash: 'dot' }, opacity: 0.55,
        });
        notes.push({ pair, kind, b: fit.b, r2: fit.r2 });
      }
    }
  }
  if (!traces.length) return { empty: 'No pairing selected.' };

  // Perfect scaling: every extra value in a call is free. Anchored at the
  // slowest visible single-value rate so it reads as a slope, not a target.
  const singles = visiblePairs()
    .map((pair) => at(pair, state.op, state.mode, state.sec, state.n, 's'))
    .filter((row) => row && row.mbmed > 0);
  const shapesX = shapes.map((s) => s.per_call);
  const xMin = Math.min(...shapesX), xMax = Math.max(...shapesX);
  const shapesList = [];
  if (singles.length) {
    const anchor = Math.min(...singles.map((row) => row.mbmed));
    shapesList.push({
      type: 'line', xref: 'x', yref: 'y',
      x0: Math.log10(xMin), y0: Math.log10(anchor),
      x1: Math.log10(xMax), y1: Math.log10(anchor * (xMax / xMin)),
      line: { color: palette.axis, width: 1.5, dash: 'dot' }, layer: 'below',
    });
    annotations.push({
      x: Math.log10(xMax), y: Math.log10(anchor * (xMax / xMin)),
      text: 'slope 1: extra values are free  ', showarrow: false,
      xanchor: 'right', yanchor: 'bottom',
      font: { color: palette.muted, size: 11, family: '__FONT__' },
    });
  }

  const layout = layoutFor({
    height: 420,
    margin: { l: 8, r: 16, t: 12, b: 46 },
    showlegend: true,
    legend: { orientation: 'h', y: -0.22, yanchor: 'top', x: 0, font: { size: 11 } },
    shapes: shapesList,
    annotations,
    xaxis: {
      type: 'log', title: { text: 'values carried by one service call' },
      tickvals: shapesX, ticktext: shapesX.map((v) => nf0.format(v)),
    },
    yaxis: { type: 'log', title: { text: 'megabytes per second' }, tickformat: '~s' },
  });

  return { data: traces, layout, caption: captionPayload(notes, shapes), table: tablePayload(shapes) };
}

function captionPayload(notes, shapes) {
  const parts = [sliceWords('payload') +
    '. Solid lines vary the nodes per Read/Write call; dashed lines vary the elements in one ' +
    'array node. The dotted fit through each is a power law, MB/s ∝ payload^b.'];
  if (notes.length) {
    const best = notes.reduce((a, b) => (b.b > a.b ? b : a));
    const worst = notes.reduce((a, b) => (b.b < a.b ? b : a));
    parts.push('Steepest: ' + pairLabel(best.pair) + ' ' + best.kind + ', b = ' + best.b.toFixed(2) +
      ' (r² ' + best.r2.toFixed(2) + ') — close to 1 means the per-call cost all but disappears. ' +
      'Flattest: ' + pairLabel(worst.pair) + ' ' + worst.kind + ', b = ' + worst.b.toFixed(2) + '.');
    const arrays = notes.filter((n) => n.kind === 'array');
    const batches = notes.filter((n) => n.kind === 'batch');
    if (arrays.length && batches.length) {
      const mean = (list) => list.reduce((s, n) => s + n.b, 0) / list.length;
      const ab = mean(arrays), bb = mean(batches);
      parts.push('Averaged over the shown pairings, arrays scale at b = ' + ab.toFixed(2) +
        ' against b = ' + bb.toFixed(2) + ' for batches: ' +
        (ab > bb ? 'one big array beats many nodes in one call at equal value count.'
                 : 'batching keeps up with arrays here.'));
    }
  }
  const biggest = shapes[shapes.length - 1];
  parts.push('Largest payload measured: ' + biggest.label + ' at ' +
    nf0.format(biggest.per_call) + ' values per call.');
  return parts.join(' ');
}

function tablePayload(shapes) {
  const rows = [];
  for (const pair of visiblePairs()) {
    for (const shape of shapes) {
      const row = at(pair, state.op, state.mode, state.sec, state.n, shape.key);
      if (!row) continue;
      rows.push({ key: pair, cells: [
        pairLabel(pair), shape.label, nf0.format(shape.per_call),
        nf0.format(Math.round(row.med)), fmtMb(row.mbmed),
      ]});
    }
  }
  return { columns: ['Pairing', 'Payload', 'Values per call', 'Calls/s', 'MB/s'], rows };
}

/** 6. Read vs. write -- the check that says the operation axis can be ignored. */
function chartOperation() {
  if (!(OPTIONS.op.includes('read') && OPTIONS.op.includes('write'))) {
    return { empty: 'Both read and write are needed for this comparison; this dataset has only one.' };
  }
  const pairs = new Set(visiblePairs());
  const byMode = { sync: [], async: [] };
  for (const row of ROWS) {
    if (row.op !== 'read' || !pairs.has(row.pair)) continue;
    // Match like with like: the same payload shape on both axes, or a scalar
    // read gets compared against an array write.
    const write = at(row.pair, 'write', row.mode, row.sec, row.n, row.shape);
    if (!write) continue;
    (byMode[row.mode] || (byMode[row.mode] = [])).push({
      x: row.med, y: write.med, pair: row.pair, mode: row.mode, sec: row.sec, n: row.n,
      shape: row.shape, shapeLabel: shapeOf(row.shape).label,
    });
  }
  const points = [].concat(byMode.sync || [], byMode.async || []);
  if (!points.length) return null;
  const all = points.flatMap((p) => [p.x, p.y]);
  const lo = Math.min(...all) * 0.8, hi = Math.max(...all) * 1.25;
  const conf = activeTheme();
  // Two series only -- inside the three-slot cap that all-pairs forms carry.
  const modeColor = { sync: conf === 'dark' ? SERIES_DARK_0 : SERIES_LIGHT_0,
                      async: conf === 'dark' ? SERIES_DARK_1 : SERIES_LIGHT_1 };

  const traces = ['sync', 'async'].filter((m) => (byMode[m] || []).length).map((mode) => ({
    type: 'scatter', mode: 'markers', name: mode + ' (' + (mode === 'sync' ? '1' : '32') + ' outstanding)',
    x: byMode[mode].map((p) => p.x), y: byMode[mode].map((p) => p.y),
    customdata: byMode[mode].map((p) => [pairLabel(p.pair), POLICY_LABEL[p.sec], p.n, p.shapeLabel]),
    marker: {
      size: 9, color: modeColor[mode], opacity: 0.85,
      line: { color: tokens().surface, width: 2 },
    },
    hovertemplate: 'read <b>%{x:,.0f}</b> · write <b>%{y:,.0f}</b> ops/s' +
      '<br>%{customdata[0]}<br><span style="color:' + tokens().muted +
      '">%{customdata[1]} · %{customdata[2]} client(s) · %{customdata[3]}</span><extra></extra>',
  }));

  const layout = layoutFor({
    height: 400,
    margin: { l: 8, r: 16, t: 12, b: 46 },
    showlegend: true,
    legend: { orientation: 'h', y: 1.02, yanchor: 'bottom', x: 0, font: { size: 11.5 } },
    shapes: [{
      type: 'line', xref: 'x', yref: 'y',
      x0: Math.log10(lo), y0: Math.log10(lo), x1: Math.log10(hi), y1: Math.log10(hi),
      line: { color: tokens().axis, width: 1.5, dash: 'dot' }, layer: 'below',
    }],
    annotations: [{
      x: Math.log10(hi), y: Math.log10(hi), text: 'read = write  ', showarrow: false,
      xanchor: 'right', yanchor: 'top', font: { color: tokens().muted, size: 11, family: '__FONT__' },
    }],
    xaxis: { type: 'log', title: { text: 'read, operations per second' }, range: [Math.log10(lo), Math.log10(hi)], tickformat: '~s' },
    yaxis: { type: 'log', title: { text: 'write, operations per second' }, range: [Math.log10(lo), Math.log10(hi)], tickformat: '~s' },
  });

  const within = points.filter((p) => Math.abs(p.y / p.x - 1) <= 0.1).length;
  const worst = points.reduce((a, b) => (Math.abs(Math.log(b.y / b.x)) > Math.abs(Math.log(a.y / a.x)) ? b : a));
  // The verdict follows the data rather than restating a prior conclusion:
  // large payloads can pull read and write apart where scalars did not.
  const agrees = within >= points.length / 2;
  const caption = within + ' of ' + points.length + ' configurations sit within ±10% of the diagonal, ' +
    (agrees
      ? 'so read and write cost about the same and the operation axis can stay parked. '
      : 'so read and write do not track each other here — check both rather than assuming one stands in for the other. ') +
    'Largest gap: ' + pairLabel(worst.pair) + ' ' + worst.mode + ', ' + POLICY_LABEL[worst.sec] + ', ' +
    worst.n + ' client(s), ' + worst.shapeLabel +
    ' — write is ' + (worst.y / worst.x).toFixed(2) + '× read.';

  return {
    data: traces, layout, caption,
    table: {
      columns: ['Pairing', 'Mode', 'Security', 'Clients', 'Payload', 'Read', 'Write', 'Write ÷ read'],
      rows: points.map((p) => ({
        key: p.pair,
        cells: [pairLabel(p.pair), p.mode, POLICY_LABEL[p.sec], String(p.n), p.shapeLabel,
          nf0.format(Math.round(p.x)), nf0.format(Math.round(p.y)), (p.y / p.x).toFixed(2) + '×'],
      })),
    },
  };
}

/* ------------------------------------------------------------------ shell */

// ``ignores`` strikes a dimension out of the card's scope line; ``notes`` says
// why, so a reader never wonders whether a control silently failed.
const CARDS = [
  { id: 'ranking', build: chartRanking, ignores: [], notes: [] },
  { id: 'heatmap', build: chartHeatmap, ignores: ['pairs'],
    notes: ['ignores the pairing filter — the grid is the pairing'] },
  { id: 'scaling', build: chartScaling, ignores: ['n'],
    notes: ['ignores the client count — it is the x axis'] },
  { id: 'ratio', build: chartRatio, ignores: [], notes: [] },
  { id: 'payload', build: chartPayload, ignores: ['payload'],
    notes: ['ignores the payload control — the payload size is the x axis'] },
  { id: 'operation', build: chartOperation, ignores: ['op', 'mode', 'sec', 'n', 'payload'],
    notes: ['every measured configuration is plotted at once, so the slice above does not scope it'] },
];

/** The slice in words. ``skip`` drops a dimension a chart divides out. */
function sliceWords(skip) {
  const bits = [];
  if (skip !== 'op') bits.push(state.op);
  if (skip !== 'mode') bits.push(state.mode);
  if (skip !== 'sec') bits.push(POLICY_LABEL[state.sec]);
  if (skip !== 'n') bits.push(state.n + ' client' + (state.n > 1 ? 's' : ''));
  if (skip !== 'payload' && OPTIONS.payloads.length > 1) bits.push(shapeOf(state.payload).label);
  return bits.join(' · ');
}

const RATIO_DIMENSION = { security: 'sec', pipeline: 'mode', baseline: null };

/** The per-card scope line: what slice it shows, and what it deliberately ignores. */
function renderScope(card) {
  const host = document.querySelector('#card-' + card.id + ' .scope');
  host.textContent = '';
  const spec = card.id === 'ratio' ? RATIO_SPECS[state.ratio] : null;
  const ignores = new Set(card.ignores);
  if (spec && RATIO_DIMENSION[state.ratio]) ignores.add(RATIO_DIMENSION[state.ratio]);
  const dims = [
    ['op', state.op],
    ['mode', state.mode],
    ['sec', POLICY_LABEL[state.sec]],
    ['n', state.n + ' client' + (state.n > 1 ? 's' : '')],
  ];
  if (OPTIONS.payloads.length > 1) dims.push(['payload', shapeOf(state.payload).label]);
  const append = (text, ignored) => {
    const node = document.createElement(ignored ? 'span' : 'b');
    if (ignored) node.className = 'ign';
    node.textContent = text;
    host.appendChild(node);
  };
  dims.forEach(([key, text], i) => {
    if (i) host.appendChild(document.createTextNode(' · '));
    append(text, ignores.has(key));
  });
  const notes = card.notes ? card.notes.slice() : [];
  if (spec && spec.overrides) notes.push('spans ' + spec.overrides + ' — both sides of the ratio');
  if (notes.length) host.appendChild(document.createTextNode('  — ' + notes.join('; ')));
}

function renderTable(card, table) {
  const host = document.querySelector('#table-' + card.id);
  host.textContent = '';
  if (!table) return;
  const el = document.createElement('table');
  const head = el.createTHead().insertRow();
  for (const column of table.columns) {
    const th = document.createElement('th');
    th.textContent = column;
    head.appendChild(th);
  }
  const body = el.createTBody();
  for (const row of table.rows) {
    const tr = body.insertRow();
    row.cells.forEach((value, i) => {
      const td = tr.insertCell();
      if (i === 0) {
        td.className = 'key';
        if (row.key && colorOf.has(row.key)) {
          const swatch = document.createElement('span');
          swatch.className = 'sw';
          swatch.style.background = hue(row.key);
          td.appendChild(swatch);
        }
      }
      td.appendChild(document.createTextNode(value));
    });
  }
  host.appendChild(el);
}

function renderCard(card) {
  const root = document.getElementById('card-' + card.id);
  const plot = root.querySelector('.plot');
  const tableHost = root.querySelector('.tablewrap');
  const caption = root.querySelector('.caption');
  renderScope(card);

  let figure = null;
  try {
    figure = card.build();
  } catch (error) {
    console.error(card.id, error);
  }

  const showTable = tableOpen.has(card.id);
  plot.classList.toggle('hidden', showTable);
  tableHost.classList.toggle('hidden', !showTable);
  if (!figure || figure.empty) {
    Plotly.purge(plot);
    plot.textContent = '';
    const note = document.createElement('p');
    note.className = 'empty';
    note.textContent = figure && figure.empty ? figure.empty : 'Nothing to show for this selection.';
    plot.appendChild(note);
    caption.textContent = '';
    renderTable(card, null);
    return;
  }

  plot.querySelectorAll(':scope > .empty').forEach((note) => note.remove());
  if (!showTable) Plotly.react(plot, figure.data, figure.layout, PLOT_CONFIG);
  renderTable(card, figure.table);
  caption.textContent = figure.caption || '';
}

function render() {
  syncControls();
  for (const card of CARDS) renderCard(card);
  writeHash();
}

/* --------------------------------------------------------------- controls */

function syncControls() {
  for (const button of document.querySelectorAll('[data-set]')) {
    const [key, value] = button.dataset.set.split(':');
    const current = String(state[key]);
    button.setAttribute('aria-pressed', current === value ? 'true' : 'false');
  }
  // Latency is only a latency when exactly one request is in flight.
  const latency = document.querySelector('[data-set="metric:lat"]');
  if (latency) {
    const allowed = state.mode === 'sync';
    latency.disabled = !allowed;
    latency.title = allowed ? 'Microseconds per operation'
      : 'Only meaningful in sync mode — async pipelines 32 requests, so the reciprocal is not a round trip.';
    if (!allowed && state.metric === 'lat') state.metric = 'ops';
  }
  const whisker = document.querySelector('[data-toggle="whiskers"]');
  if (whisker) whisker.setAttribute('aria-pressed', state.whiskers ? 'true' : 'false');
  for (const chip of document.querySelectorAll('[data-pair]')) {
    const shown = !state.hidden.includes(chip.dataset.pair);
    chip.setAttribute('aria-pressed', shown ? 'true' : 'false');
    chip.querySelector('.sw').style.background = shown ? hue(chip.dataset.pair) : 'transparent';
  }
  for (const [key, spec] of Object.entries(RATIO_SPECS)) {
    const button = document.querySelector('[data-set="ratio:' + key + '"]');
    if (button) button.disabled = !spec.need();
  }
}

function wire() {
  document.addEventListener('click', (event) => {
    const setter = event.target.closest('[data-set]');
    if (setter && !setter.disabled) {
      const [key, value] = setter.dataset.set.split(':');
      state[key] = key === 'n' ? parseInt(value, 10) : value;
      render();
      return;
    }
    const toggle = event.target.closest('[data-toggle]');
    if (toggle) {
      state[toggle.dataset.toggle] = !state[toggle.dataset.toggle];
      render();
      return;
    }
    const chip = event.target.closest('[data-pair]');
    if (chip) {
      const pair = chip.dataset.pair;
      const position = state.hidden.indexOf(pair);
      if (position >= 0) state.hidden.splice(position, 1); else state.hidden.push(pair);
      render();
      return;
    }
    const preset = event.target.closest('[data-preset]');
    if (preset) {
      const keep = {
        all: () => SERIES.map((s) => s.pair),
        self: () => SERIES.filter((s) => s.client === s.server).map((s) => s.pair),
        client: () => SERIES.filter((s) => s.server === 'open62541').map((s) => s.pair),
        server: () => SERIES.filter((s) => s.client === 'open62541').map((s) => s.pair),
      }[preset.dataset.preset]();
      state.hidden = SERIES.map((s) => s.pair).filter((p) => !keep.includes(p));
      render();
      return;
    }
    const tableButton = event.target.closest('[data-table]');
    if (tableButton) {
      const id = tableButton.dataset.table;
      if (tableOpen.has(id)) tableOpen.delete(id); else tableOpen.add(id);
      tableButton.setAttribute('aria-pressed', tableOpen.has(id) ? 'true' : 'false');
      renderCard(CARDS.find((c) => c.id === id));
      return;
    }
    const themeButton = event.target.closest('[data-themetoggle]');
    if (themeButton) {
      const next = activeTheme() === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      themeButton.textContent = next === 'dark' ? 'Light theme' : 'Dark theme';
      render();
    }
  });

  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (!document.documentElement.getAttribute('data-theme')) render();
  });
}

readHash();
wire();
render();
"""


def build_page(
    metadata: dict,
    uniform: dict,
    rows: list[dict],
    failures: list[dict],
    series: list[dict],
    plotly_tag: str,
) -> str:
    """Assemble the standalone HTML document."""
    options = {
        "op": [value for value in ("read", "write") if any(r["op"] == value for r in rows)],
        "mode": [value for value in ("sync", "async") if any(r["mode"] == value for r in rows)],
        "sec": [value for value in ("none", "enc") if any(r["sec"] == value for r in rows)],
        "n": sorted({r["n"] for r in rows}),
        "payloads": payload_options(rows),
        "impl": [value for value in IMPLEMENTATION_ORDER if any(r["client"] == value or r["server"] == value for r in rows)],
    }
    payload = {
        "rows": rows,
        "failures": failures,
        "series": series,
        "options": options,
        "baseline": BASELINE_PAIR,
        "metadata": metadata,
    }

    controller = (
        CONTROLLER_JS.replace("__DATA__", json.dumps(payload, separators=(",", ":")))
        .replace("__THEME__", json.dumps(build_theme_payload(), separators=(",", ":")))
        .replace("__IMPL_LABELS__", json.dumps(IMPL_LABELS, separators=(",", ":")))
        .replace("__S_L0__", SERIES_LIGHT[0])
        .replace("__S_L1__", SERIES_LIGHT[1])
        .replace("__S_D0__", SERIES_DARK[0])
        .replace("__S_D1__", SERIES_DARK[1])
        .replace("__FONT__", FONT_STACK)
    )
    css = CSS.replace("__FONT__", FONT_STACK)

    def segment(group_label: str, key: str, choices: list[tuple[str, str, str]]) -> str:
        buttons = "".join(
            f'<button type="button" data-set="{key}:{value}" title="{title}">{label}</button>'
            for value, label, title in choices
        )
        return (
            f'<div class="group"><span class="glabel">{group_label}</span>'
            f'<div class="seg" role="group" aria-label="{group_label}">{buttons}</div></div>'
        )

    op_choices = [(v, v.capitalize(), f"Benchmark the {v} service") for v in options["op"]]
    mode_choices = [(v, v, "One request at a time" if v == "sync" else "Up to 32 requests in flight") for v in options["mode"]]
    sec_choices = [
        (
            v,
            "None" if v == "none" else "Basic256Sha256",
            "Plaintext" if v == "none" else "SignAndEncrypt with self-signed certificates",
        )
        for v in options["sec"]
    ]
    client_choices = [(str(n), str(n), f"{n} concurrent client process(es)") for n in options["n"]]
    payload_choices = [
        (
            entry["key"],
            entry["label"],
            f"{entry['per_call']:,} value(s) per service call",
        )
        for entry in options["payloads"]
    ]

    controls = "".join(
        [
            segment("Operation", "op", op_choices) if len(op_choices) > 1 else "",
            segment("Mode", "mode", mode_choices) if len(mode_choices) > 1 else "",
            segment("Security", "sec", sec_choices) if len(sec_choices) > 1 else "",
            segment("Clients", "n", client_choices) if len(client_choices) > 1 else "",
            segment("Payload", "payload", payload_choices) if len(payload_choices) > 1 else "",
            segment(
                "Metric",
                "metric",
                [
                    ("ops", "calls/s", "Read/Write service calls per second"),
                    ("data", "MB/s", "Int32 payload moved per second, 4 bytes a value"),
                    ("lat", "µs/call", "Microseconds per service call"),
                ],
            ),
            segment(
                "Scale",
                "scale",
                [
                    ("abs", "Absolute", "Measured values"),
                    ("rel", "× C", "Normalised to the open62541 C client and server"),
                ],
            ),
            segment(
                "Axis",
                "axis",
                [
                    ("linear", "Linear", "Bar length stays proportional to the value"),
                    ("log", "Log", "Fits the full 190x range, at the cost of proportional bars"),
                ],
            ),
            '<div class="group"><div class="seg">'
            '<button type="button" data-toggle="whiskers" title="Show the min-max range '
            'across samples">Whiskers</button></div></div>',
            segment(
                "Ratio",
                "ratio",
                [
                    ("security", "Encryption", "Basic256Sha256 divided by SecurityPolicy None"),
                    ("pipeline", "Pipelining", "async divided by sync"),
                    ("baseline", "vs C", "This pairing divided by the C client and server"),
                ],
            ),
        ]
    )

    chips = "".join(
        f'<button type="button" class="chip" data-pair="{escape(entry["pair"])}">'
        f'<span class="sw" style="background:{entry["light"]}"></span>'
        f'{escape(IMPL_LABELS.get(entry["client"], entry["client"]))} &rarr; '
        f'{escape(IMPL_LABELS.get(entry["server"], entry["server"]))}</button>'
        for entry in series
    )

    cards = "".join(
        card_html(identifier, number, title, question)
        for identifier, number, title, question in (
            ("ranking", "1", "Who is fastest?", "one bar per pairing, in the selected slice"),
            (
                "heatmap",
                "2",
                "Is that the client or the server?",
                "the pairing matrix, one cell per combination",
            ),
            ("scaling", "3", "Does it hold under load?", "throughput against concurrent clients"),
            ("ratio", "4", "What does the feature cost?", "ratios against 1.0"),
            (
                "payload",
                "5",
                "What do batching and arrays buy?",
                "megabytes per second against payload size, with a power-law fit",
            ),
            (
                "operation",
                "†",
                "Does read vs. write matter?",
                "the check behind parking the operation axis",
            ),
        )
    )

    machine = metadata.get("cpu_model") or "unknown CPU"
    host = metadata.get("hostname") or "unknown host"
    stamp = metadata.get("timestamp_utc") or "unknown date"
    # The run's settings live in the document's 'config.uniform' section, which
    # is where run.py writes them and what --amend reads back. They were once
    # copied into 'metadata.parameters'; a document written by the current
    # runner has no such key, and looking there printed the whole line as '?'.
    parameters = uniform or metadata.get("parameters") or {}
    # Report the samples actually behind the charts rather than the run's target:
    # a resumed or partly failed matrix can hold fewer, and the aggregates are
    # computed from what is there.
    counts = sorted({row["ns"] for row in rows})
    if not counts:
        sample_note = "no samples"
    elif len(counts) == 1:
        sample_note = f"median of {counts[0]} sample(s)"
    else:
        sample_note = f"median of {counts[0]}–{counts[-1]} samples per configuration"
    short_rows = sum(1 for row in rows if row["ns"] < row["want"])
    run_note = (
        f"{parameters.get('iterations', '?')} timed operations per client, "
        f"{parameters.get('warmup', '?')} warm-up, "
        f"{sample_note}, "
        f"async pipeline depth {parameters.get('max_outstanding', '?')}."
    )
    if short_rows:
        run_note += (
            f" {short_rows} configuration(s) hold fewer samples than the run asked " "for; re-running the matrix tops them up."
        )
    # Said in the header rather than the footer: which configurations are absent
    # decides what the charts below can be compared across, and a reader who
    # averages a payload without knowing that some pairings dropped out of it
    # gets a number weighted towards whoever survived.
    failure_note = ""
    if failures:
        payloads = {entry["shape"] for entry in failures}
        worst = max(payloads, key=lambda shape: sum(1 for e in failures if e["shape"] == shape))
        worst_label = next((p["label"] for p in options["payloads"] if p["key"] == worst), worst)
        failure_note = (
            f" {len(failures)} further configuration(s) were measured but could not "
            f"complete and are absent from every chart, {sum(1 for e in failures if e['shape'] == worst)} of "
            f"them on {escape(worst_label)}: read a cell, not an average across one."
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OPC UA client/server benchmark — {escape(host)}</title>
<style>{css}</style>
</head>
<body>
<div class="viz-root">
<div class="wrap">
<header class="page">
  <div>
    <h1>OPC UA client/server throughput</h1>
    <p>{escape(str(len(rows)))} measured configurations across
    {escape(str(len(series)))} client/server pairings, read and write, sync and async,
    plaintext and SignAndEncrypt, 1 to {escape(str(max(options['n'])))} concurrent clients.
    Every chart below reads from the same control row.{failure_note}</p>
  </div>
  <div class="spacer"></div>
  <div class="presets"><button type="button" data-themetoggle>Dark theme</button></div>
</header>

<div class="controls">{controls}</div>
<div class="legendbar">
  {chips}
  <div class="presets">
    <button type="button" data-preset="all">All</button>
    <button type="button" data-preset="self">Same impl</button>
    <button type="button" data-preset="client">Isolate client</button>
    <button type="button" data-preset="server">Isolate server</button>
  </div>
</div>

{cards}

<footer class="page">
  <p>Node.js sync mode uses sequential asynchronous I/O with one outstanding service.</p>
  <p class="warnnote"><span class="hatch"></span>A hatched bar or a
  &#9888; in a table marks a provisional median: either the fastest sample was more
  than twice the slowest, or the configuration holds fewer samples than the run
  asked for. The Samples column gives the count behind each row.</p>
  <p>{escape(machine)} · {escape(str(metadata.get('cpu_count_logical', '?')))} logical cores ·
  {escape(str(metadata.get('platform', '')))} · Python {escape(str(metadata.get('python_version', '?')))}
  · {escape(host)} · {escape(stamp)}</p>
  <p>{escape(run_note)} Generated from <code>run.py sample</code> by
  <code>throughput/show.py</code>. Every chart has a table view; the current
  selection lives in the page URL.</p>
</footer>
</div>
</div>
{plotly_tag}
<script>{controller}</script>
</body>
</html>
"""


def escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def card_html(identifier: str, number: str, title: str, question: str) -> str:
    return f"""
<section class="card" id="card-{identifier}">
  <header>
    <h2>{number}. {escape(title)}</h2>
    <span class="q">{escape(question)}</span>
    <div class="tools">
      <button type="button" data-table="{identifier}" aria-pressed="false">Table</button>
    </div>
  </header>
  <p class="scope"></p>
  <div class="plot"></div>
  <div class="tablewrap hidden" id="table-{identifier}"></div>
  <p class="caption"></p>
</section>
"""
