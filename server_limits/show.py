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

"""Render server resource-limit measurements."""

from __future__ import annotations

import json
from pathlib import Path

from plotly.offline import get_plotlyjs

from common.bench_db import BenchDB
from common.sdk_workers import COLORS as SDK_COLORS, LABELS as SDK_LABELS, NAMES as SDK_NAMES

PLOTLY_CDN = "https://cdn.plot.ly/plotly-3.0.1.min.js"
FONT_STACK = 'system-ui, -apple-system, "Segoe UI", sans-serif'

# Fixed order, matching server_limits/run.py's IMPLEMENTATIONS — a color means
# the same implementation on every report this repo generates. Three is also
# the most an all-pairs chart form (the scatter in card 1, where any two
# points can sit side by side) can carry and still clear the CVD floors, which
# this suite never exceeds.
IMPLEMENTATION_ORDER: tuple[str, ...] = ("open62541", "o6-python", "asyncua", "ua-dotnet", "node-opcua") + SDK_NAMES
IMPLEMENTATION_LABELS = {
    "open62541": "open62541 (C)",
    "o6-python": "o6\\Python",
    "asyncua": "asyncua",
    "ua-dotnet": "OPC Foundation (.NET)",
    "node-opcua": "node-opcua (Node.js)",
    **SDK_LABELS,
}

# Status never follows a theme (see the data-viz skill): pass/give-up is state,
# not identity, so it gets its own fixed steps rather than a categorical slot.
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
        "open62541": "#2a78d6",
        "o6-python": "#eb6834",
        "asyncua": "#1baf7a",
        "ua-dotnet": "#663399",
        "node-opcua": "#007f86",
        "good": "#0ca30c",
        "critical": "#d03b3b",
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
        "open62541": "#3987e5",
        "o6-python": "#d95926",
        "asyncua": "#199e70",
        "ua-dotnet": "#bb88ee",
        "node-opcua": "#44bbbb",
        "good": "#0ca30c",
        "critical": "#d03b3b",
    },
}

for _name, (_light, _dark) in SDK_COLORS.items():
    THEMES["light"][_name] = _light
    THEMES["dark"][_name] = _dark

REASON_LABELS = {
    "error_rate": "error/timeout rate",
    "latency_blowup": "p99 latency blow-up",
    "throughput_collapse": "throughput collapse",
    "connection_broken": "connection broken",
    "process_death": "server process died",
}
# Three or four characters, because this is written inside a heatmap cell that
# is about 30px wide on a three-across layout. The legend under the card spells
# out the ones a document actually contains.
REASON_CODES = {
    "error_rate": "err",
    "latency_blowup": "p99",
    "throughput_collapse": "thr",
    "connection_broken": "net",
    "process_death": "dead",
}
# Which reason to show when a level tripped several at once — hardest evidence
# first: the server being gone outright beats the connection dropping, which
# beats calls failing, which beats work going down, which beats it merely
# having become slow. The cell marks the most severe and says there were more;
# the hover and the table carry the full list.
REASON_SEVERITY: tuple[str, ...] = (
    "process_death",
    "connection_broken",
    "error_rate",
    "throughput_collapse",
    "latency_blowup",
)


def load_document(database: Path) -> tuple[dict, dict, list[dict], list[dict]]:
    """Read ``database`` into metadata, run settings, results, and failures.

    This suite's config/results/failures/metadata all live in its slice of
    the shared ``database`` (see :class:`common.bench_db.BenchDB`). The
    run settings come back as well as the results because a verdict is
    only meaningful against the thresholds it was judged by — "gave up"
    means nothing without the number it crossed.
    """
    store = BenchDB(database, suite="server_limits")
    metadata = store.stored_metadata
    uniform, _varying = store.get_config("server_limits")
    results = [row for row in store.results.values() if isinstance(row.get("config_key"), dict)]
    failures = list(store.failures)
    if not results:
        raise ValueError(
            f"{database} holds no measured load levels; run 'python -m bench.server_limits sample <n> {database}' first"
        )
    return metadata, uniform, results, failures


def build_points(results: list[dict]) -> list[dict]:
    """One entry per measured load level, flattened for the page's JS to chart directly."""
    points = []
    for row in results:
        key = row["config_key"]
        clients = int(key.get("clients") or 0)
        outstanding = int(key.get("outstanding") or 0)
        if clients <= 0 or outstanding <= 0:
            continue  # a server-start failure carries no load level, only a note under 'failures'
        stats = row.get("stats") or {}
        verdict = row.get("verdict") or {}
        # Which *kind* of failure a level saw, summed over its repeats. The
        # verdict says a level beat the server; this says how, and is what
        # separates a server that stopped answering from one nothing could
        # reach in the first place — the two read identically as an error rate.
        runs = [run for run in (row.get("runs") or []) if isinstance(run, dict)]
        breakdown = {
            field: sum(int(run.get(field) or 0) for run in runs)
            for field in ("timeouts", "connection_lost", "other_errors", "clients_connect_failed")
        }
        points.append(
            {
                "implementation": str(key.get("implementation", "?")),
                "clients": clients,
                "outstanding": outstanding,
                "concurrency": clients * outstanding,
                "opsPerSecond": float(stats.get("median_ops_per_second") or 0.0),
                "errorRate": float(stats.get("median_error_rate") or 0.0),
                "p99Ms": stats.get("median_p99_latency_ms"),
                "failed": bool(verdict.get("failed")),
                "reasons": [str(reason) for reason in (verdict.get("reasons") or [])],
                # Zero attempts is its own diagnosis, not a missing number: the
                # level's 100% error rate is then the "nothing got through"
                # fallback rather than a count of calls that came back bad.
                "attempted": sum(int(run.get("attempted") or 0) for run in runs),
                "timeouts": breakdown["timeouts"],
                "connectionLost": breakdown["connection_lost"],
                "otherErrors": breakdown["other_errors"],
                # Counted per client process per repeat: a level of ten clients
                # measured twice reports twenty if none of them ever connected.
                "connectFailures": breakdown["clients_connect_failed"],
            }
        )
    return points


# How close to the peak a level must come to count as already at capacity.
# Kept equal to KNEE_FRACTION in run.py, so the report and the line the runner
# printed on stderr cannot name two different knees for one measurement.
KNEE_FRACTION = 0.95


def _knee(held: list[dict]) -> dict:
    """Peak throughput and the cheapest load that essentially reaches it."""
    if not held:
        return {"peakOpsPerSecond": 0.0, "peak": None, "knee": None}
    peak = max(held, key=lambda point: point["opsPerSecond"])
    knee = min(
        (point for point in held if point["opsPerSecond"] >= peak["opsPerSecond"] * KNEE_FRACTION),
        key=lambda point: point["concurrency"],
    )
    return {"peakOpsPerSecond": peak["opsPerSecond"], "peak": peak, "knee": knee}


def build_summary(points: list[dict], implementations: list[str]) -> list[dict]:
    """The frontier each implementation reached: its best sustained level and its edge.

    Mirrors the "frontier summary" run.py prints on stderr after a run, so the
    report and the terminal never disagree about what "sustained" and "gave
    up" mean.
    """
    summary = []
    for implementation in implementations:
        rows = [point for point in points if point["implementation"] == implementation]
        if not rows:
            continue
        passed = [point for point in rows if not point["failed"]]
        failed = [point for point in rows if point["failed"]]
        best = max(passed, key=lambda point: point["concurrency"], default=None)
        edge = min(failed, key=lambda point: point["concurrency"], default=None)
        # The latency verdict is a multiple of this one figure, so the report
        # names it: "10x the baseline" is not a readable threshold without it.
        lightest = min(rows, key=lambda point: (point["concurrency"], point["clients"]))
        summary.append(
            {
                "implementation": implementation,
                "sustained": best,
                "edge": edge,
                "baselineP99Ms": lightest["p99Ms"],
                # Where the work stops growing, which is a different question
                # from where the server stops coping: the knee is the cheapest
                # load that already reaches (near) peak throughput, so past it
                # concurrency buys latency rather than completed work. Read off
                # the levels that held — a level past the edge can post a high
                # rate while erroring, and capacity reached by failing is not
                # capacity.
                **_knee(passed),
                # A lighter level failing while a heavier one held is not a
                # frontier, it is scatter: the run did not separate the server's
                # limit from its own noise. Worth saying out loud, because the
                # two numbers otherwise read as a contradiction.
                "nonMonotone": bool(best and edge and edge["concurrency"] < best["concurrency"]),
            }
        )
    return summary


def build_payload(metadata: dict, results: list[dict], failures: list[dict]) -> dict:
    points = build_points(results)
    present = [name for name in IMPLEMENTATION_ORDER if any(point["implementation"] == name for point in points)]
    present += sorted({point["implementation"] for point in points} - set(present))
    return {
        "points": points,
        "summary": build_summary(points, present),
        "implementations": present,
        "labels": {name: IMPLEMENTATION_LABELS.get(name, name) for name in present},
        "outstandingAxis": sorted({point["outstanding"] for point in points}),
        "clientsAxis": sorted({point["clients"] for point in points}),
        "failures": [
            {"configuration": str(entry.get("configuration", "?")), "error": str(entry.get("error", "")).splitlines()[0]}
            for entry in failures
        ],
        "reasonLabels": REASON_LABELS,
        "reasonCodes": REASON_CODES,
        "reasonSeverity": list(REASON_SEVERITY),
        "themes": THEMES,
        "font": FONT_STACK,
    }


def number(value: object, suffix: str = "", fallback: str = "—") -> str:
    """Format a threshold for prose, dropping the noise off a float ("10", not "10.0")."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return fallback
    return f"{value:g}{suffix}"


def percent(value: object, fallback: str = "—") -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return fallback
    return f"{value * 100:g}%"


# The five verdicts, in the order a reader meets them rather than by severity:
# what each one measures, and which run setting sets its threshold. The prose
# is here rather than in the page template so the description and the number
# beside it come from one place.
CRITERIA: tuple[tuple[str, str, str], ...] = (
    (
        "error_rate",
        "The share of this level's calls that timed out, came back an error, or were never "
        "answered at all. A client that could not even connect counts as one failed call rather "
        "than dropping out of the totals, so a server that has stopped accepting sessions shows "
        "up here rather than as missing data — which also means a level nothing could reach at "
        "all lands here at 100%. The 'how it failed' column separates the two.",
        "more than {error_rate} of calls failed",
    ),
    (
        "latency_blowup",
        "This level's 99th-percentile latency measured against the p99 at the lightest load this "
        "implementation was run at (1 client, 1 outstanding). It catches the server that is still "
        "answering every single call, and correctly, but has stopped answering promptly — which "
        "no error count would ever show. Read it together with the throughput: a level that "
        "tripped this while its calls/second sat at the implementation's peak, with no errors, is "
        "a server at capacity and queueing normally — latency rising in proportion to the "
        "requests in flight is arithmetic, not a fault — rather than one in trouble. The lightest-"
        "load p99 it is compared against is named under each frontier above, and is measured "
        "rather than configured, so a noisy server wants 'samples' raised before it is trusted.",
        "p99 grew past {latency_blowup} that implementation's own lightest-load p99",
    ),
    (
        "throughput_collapse",
        "Successful calls per second against the previous pipeline depth at the same client count. "
        "It catches the point where offering the server more work makes it complete less — the "
        "queue is now costing more than it buys. Compared within one client count only, since a "
        "new row restarts at depth 1 and is genuinely less concurrent than the row before it.",
        "completed work fell below {throughput_collapse} of the previous depth",
    ),
    (
        "connection_broken",
        "A connection that was already established died in the middle of the level — the secure "
        "channel closed, or the socket dropped — rather than any individual call coming back bad. "
        "It does not mean the server never ran: a server that failed to start never produces a "
        "load level at all (it is listed in the banner at the top of this page), and one that "
        "refused the connection outright is counted as a failed call, so it reads as err. A call "
        "the load generator itself declined to send is not a verdict at all — it is recorded as a "
        "harness failure in the banner above. Read a net at the very top of a ramp with some "
        "suspicion even so: the machine running out of sockets or file descriptors looks the same "
        "from the client side as the server hanging up.",
        "any client lost a connection it had already made",
    ),
    (
        "process_death",
        "The server process did not survive the level. Unlike the four above, which infer distress "
        "from what the client saw, this is the server observably gone, so it ends that "
        "implementation's ramp on the spot instead of only ending the row. Not always a capacity "
        "limit: an o6\\Python server on an evaluation licence exits by itself after ten minutes, which "
        "looks identical from here, so check the run's wall-clock before reading it as one.",
        "the server process exited",
    ),
)


def criteria_table(uniform: dict) -> str:
    """The verdict reference table, carrying the thresholds this run was judged by.

    A document that does not record a threshold (one written by hand, or by an
    older runner) still gets a sentence that reads: the fallback is the phrase
    the number would have replaced, not a dash dropped into the middle of it.
    """
    unknown = "the configured share"
    filled = {
        "error_rate": percent(uniform.get("error_rate_threshold"), fallback=unknown),
        "latency_blowup": number(uniform.get("latency_blowup_factor"), suffix="x", fallback="the configured multiple of"),
        "throughput_collapse": percent(uniform.get("throughput_collapse_ratio"), fallback=unknown),
    }
    rows = "".join(
        "<tr>"
        f"<td><code>{escape(REASON_CODES[key])}</code></td>"
        f"<td>{escape(REASON_LABELS[key])}</td>"
        f'<td class="name">{escape(description)}</td>'
        f'<td class="name">{escape(threshold.format(**filled))}</td>'
        "</tr>"
        for key, description, threshold in CRITERIA
    )
    return (
        "<table><thead><tr><th>Code</th><th>Verdict</th><th>What it measures</th>"
        "<th>Tripped in this run when…</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def timing_note(uniform: dict) -> str:
    """One sentence on the clock every verdict above was measured against."""
    if not any(uniform.get(field) for field in ("step_seconds", "request_timeout_ms", "grace_seconds")):
        # Rather than a sentence made of dashes: say the settings are absent,
        # and say what produces a document that carries them.
        return (
            "This database does not record the run settings it was measured under, so the "
            "thresholds above cannot be filled in. A database written by "
            "'server_limits/run.py sample' carries them in its 'config' table."
        )
    return (
        f"Each load level was measured for {number(uniform.get('step_seconds'), ' s')} after a "
        f"{number(uniform.get('warmup_seconds'), ' s')} untimed warm-up, with a per-call timeout of "
        f"{number(uniform.get('request_timeout_ms'), ' ms')} and "
        f"{number(uniform.get('grace_seconds'), ' s')} of grace for calls still in flight when the "
        "window closed — anything still unanswered by then is counted as failed, not waited for. "
        f"Each level was measured {number(uniform.get('samples'), fallback='1')}x, and the median "
        "of those decides the verdict."
    )


def build_page(metadata: dict, payload: dict, uniform: dict, plotly_tag: str) -> str:
    payload_json = json.dumps(payload, separators=(",", ":"))
    machine = " · ".join(str(metadata.get(field)) for field in ("cpu_model", "platform") if metadata.get(field))
    footer = " · ".join(
        part
        for part in (
            escape(machine),
            f"o6 {escape(str(metadata.get('o6_version')))}" if metadata.get("o6_version") else "",
            escape(str(metadata.get("timestamp_utc") or "")),
        )
        if part
    )
    criteria = criteria_table(uniform)
    timing = escape(timing_note(uniform))
    failures_block = ""
    if payload["failures"]:
        items = "".join(
            f"<li><code>{escape(entry['configuration'])}</code> — {escape(entry['error'])}</li>"
            for entry in payload["failures"]
        )
        failures_block = f"""
<details class="warning">
  <summary>{len(payload["failures"])} configuration(s) could not be measured at all</summary>
  <ul>{items}</ul>
</details>
"""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>server_limits — how hard can you push it?</title>
<style>{CSS}</style>
</head>
<body>
<div class="viz-root">
<div class="wrap">

<header class="page">
  <div>
    <h1>How hard can you push each server before it gives up?</h1>
    <p>One fixed open62541 C client, escalating load (client processes x async pipeline
    depth) against each server implementation's default configuration until it errors,
    its tail latency blows up, its throughput collapses, or it dies. See
    <code>server_limits/README.md</code> for exactly what each of those means.</p>
  </div>
  <div class="spacer"></div>
  <div class="presets"><button type="button" id="themetoggle">Dark theme</button></div>
</header>

{failures_block}

<section class="card lead">
  <header><h2>The short version</h2>
    <span class="q">Every server was given more and more work until it could not keep up.
    Each bar is the most work that server got through while still answering everything
    on time.</span>
  </header>
  <div class="plot short" id="plot-headline"></div>
  <p class="caption" id="cap-headline"></p>
</section>

<section class="card">
  <header><h2>1. How does completed work scale with load?</h2>
    <span class="q">Each point is one measured load level. A hollow marker is a level that
    beat the server.</span>
    <div class="tools">
      <button type="button" class="metricbtn" data-metric="ops" aria-pressed="true">Throughput</button>
      <button type="button" class="metricbtn" data-metric="latency" aria-pressed="false">Latency</button>
      <button type="button" data-table="scale" aria-pressed="false">Table</button>
    </div>
  </header>
  <div class="legendbar" id="legend-scale"></div>
  <div class="plot tall" id="plot-scale"></div>
  <div class="tablewrap hidden" id="table-scale"></div>
  <p class="caption" id="cap-scale"></p>
</section>

<section class="card">
  <header><h2>2. Where does each implementation's edge sit?</h2>
    <span class="q">Every load level tried, laid out by client-process count and per-client
    pipeline depth.</span>
    <div class="tools"><button type="button" data-table="frontier" aria-pressed="false">Table</button></div>
  </header>
  <div class="legendbar" id="legend-frontier"></div>
  <div class="frontiergrid" id="plot-frontier"></div>
  <div class="tablewrap hidden" id="table-frontier"></div>
  <p class="caption" id="cap-frontier"></p>
</section>

<section class="card">
  <header><h2>3. Which implementation went furthest?</h2>
    <span class="q">Concurrent requests in flight (clients x outstanding) at the heaviest
    load level that still held. The table adds the capacity reading: peak throughput, and
    the cheapest load that already reaches it.</span>
    <div class="tools"><button type="button" data-table="summary" aria-pressed="false">Table</button></div>
  </header>
  <div class="plot" id="plot-summary"></div>
  <div class="tablewrap hidden" id="table-summary"></div>
  <p class="caption" id="cap-summary"></p>
</section>

<section class="card">
  <header><h2>What each verdict means</h2>
    <span class="q">The five criteria a load level is judged against, and the thresholds
    this particular run used. A level trips as many of them as apply.</span>
  </header>
  <div class="tablewrap tall">{criteria}</div>
  <p class="caption">{timing}</p>
  <p class="caption">Where a level tripped more than one criterion, the heatmap cell above
  shows the most severe and marks it with a <code>+</code>; severity runs in the order
  listed here read upwards — the server being gone outright outranks a dropped connection,
  which outranks failing calls, which outranks work going down, which outranks merely
  having become slow. The full list is always in the hover and in the tables.</p>
</section>

<footer class="page">
  <p>{footer}</p>
  <p>Generated from <code>server_limits/run.py sample</code> by <code>server_limits/show.py</code>.
  Every card has a table view.</p>
</footer>

</div>
</div>
<script id="payload" type="application/json">{payload_json}</script>
{plotly_tag}
<script>{CONTROLLER}</script>
</body>
</html>
"""


def escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


CSS = """
*, *::before, *::after { box-sizing: border-box; }
.viz-root {
  color-scheme: light;
  --surface: #fcfcfb; --plane: #f9f9f7; --primary: #0b0b0b; --secondary: #52514e;
  --muted: #898781; --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
  --open62541: #2a78d6; --o6-python: #eb6834; --asyncua: #1baf7a; --ua-dotnet: #663399; --node-opcua: #007f86;
  --milo: #e87ba4; --s2opc: #eda100; --gopcua: #4a3aa7;
  --good: #0ca30c; --critical: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) .viz-root {
    color-scheme: dark;
    --surface: #1a1a19; --plane: #0d0d0d; --primary: #ffffff; --secondary: #c3c2b7;
    --muted: #898781; --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --open62541: #3987e5; --o6-python: #d95926; --asyncua: #199e70; --ua-dotnet: #bb88ee; --node-opcua: #44bbbb;
    --milo: #d55181; --s2opc: #c98500; --gopcua: #9085e9;
    --good: #0ca30c; --critical: #d03b3b;
  }
}
:root[data-theme="dark"] .viz-root {
  color-scheme: dark;
  --surface: #1a1a19; --plane: #0d0d0d; --primary: #ffffff; --secondary: #c3c2b7;
  --muted: #898781; --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
  --open62541: #3987e5; --o6-python: #d95926; --asyncua: #199e70; --ua-dotnet: #bb88ee; --node-opcua: #44bbbb;
  --milo: #d55181; --s2opc: #c98500; --gopcua: #9085e9;
  --good: #0ca30c; --critical: #d03b3b;
}
html, body { margin: 0; padding: 0; }
body { background: var(--plane); }
.viz-root {
  background: var(--plane); color: var(--primary);
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
  min-height: 100vh;
}
.wrap { max-width: 1220px; margin: 0 auto; padding: 32px 24px 64px; }
header.page { display: flex; align-items: flex-start; gap: 20px; margin-bottom: 26px; }
header.page .spacer { flex: 1; }
h1 { font-size: 25px; line-height: 1.25; margin: 0 0 8px; letter-spacing: -0.01em; }
header.page p { margin: 0; color: var(--secondary); max-width: 72ch; }
h2 { font-size: 16px; margin: 0; }
button, select {
  font: inherit; font-size: 13px; color: var(--primary); background: var(--surface);
  border: 1px solid var(--border); border-radius: 7px; padding: 6px 11px; cursor: pointer;
}
button:hover, select:hover { border-color: var(--axis); }
button[aria-pressed="true"] { background: var(--primary); color: var(--surface); border-color: var(--primary); }
.tools { display: flex; gap: 8px; flex-wrap: wrap; }
.legendbar { display: flex; flex-wrap: wrap; gap: 8px 18px; align-items: center;
  margin: 0 4px 10px; font-size: 13px; color: var(--secondary); }
.chip { display: inline-flex; align-items: center; gap: 7px; }
.chip i { width: 11px; height: 11px; border-radius: 3px; display: inline-block; }
.chip i.ring { background: transparent; border: 2px solid var(--secondary); border-radius: 50%; width: 9px; height: 9px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 13px;
  padding: 18px 18px 10px; margin-bottom: 22px; }
.card > header { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 4px; }
.card .q { color: var(--secondary); font-size: 13px; flex: 1; min-width: 16ch; }
.plot { width: 100%; height: 380px; }
.plot.tall { height: 460px; }
/* One bar per implementation and nothing else, so the card wants the height of
   its bars rather than a chart-shaped box. */
.plot.short { height: 240px; }
/* The lead card answers the question in the page title on its own; the numbered
   cards below it are the working. A heavier heading and a plainer body are what
   say so to a reader who is not going to read the rest. */
.card.lead { border-color: var(--axis); }
.card.lead > header h2 { font-size: 19px; }
.card.lead .q { font-size: 14px; color: var(--primary); }
.frontiergrid { display: flex; flex-wrap: wrap; gap: 18px; }
.frontiergrid .cell { flex: 1 1 260px; min-width: 220px; }
.frontiergrid .cell h3 { font-size: 13px; font-weight: 600; margin: 0 0 2px; color: var(--secondary); }
.frontiergrid .cell .edge { font-size: 12px; margin: 0 0 4px; color: var(--muted); min-height: 2.6em; }
.frontiergrid .plot { height: 260px; }
.hidden { display: none; }
.caption { color: var(--muted); font-size: 12.5px; margin: 6px 2px 10px; }
.tablewrap { max-height: 420px; overflow: auto; margin: 6px 0 12px; }
.tablewrap.tall { max-height: none; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
td code { background: var(--plane); border: 1px solid var(--border); border-radius: 4px; padding: 1px 5px; }
table { border-collapse: collapse; width: 100%; font-size: 12.5px; }
th, td { text-align: left; padding: 5px 10px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
th { position: sticky; top: 0; background: var(--surface); color: var(--secondary); font-weight: 600; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
td.name { white-space: normal; word-break: break-word; max-width: 52ch; }
.warning { background: var(--surface); border: 1px solid var(--border); border-radius: 11px;
  padding: 10px 16px; margin-bottom: 18px; color: var(--secondary); font-size: 13px; }
.warning summary { cursor: pointer; color: var(--primary); font-weight: 600; }
.warning ul { margin: 8px 0 0; padding-left: 20px; }
.warning code { font-size: 12px; }
footer.page { color: var(--muted); font-size: 12.5px; margin-top: 30px; }
footer.page p { max-width: 96ch; }
footer.page code { font-size: 12px; }
"""


CONTROLLER = r"""
(function () {
  const DATA = JSON.parse(document.getElementById("payload").textContent);
  const POINTS = DATA.points;
  const $ = (id) => document.getElementById(id);

  const themeName = () => {
    const stamped = document.documentElement.getAttribute("data-theme");
    if (stamped) return stamped;
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  };
  const theme = () => DATA.themes[themeName()];
  const implColor = (name) => theme()[name] || theme().muted;

  const compact = (v) => {
    const a = Math.abs(v);
    if (a >= 1e9) return (v / 1e9).toFixed(a >= 1e10 ? 0 : 1) + "G";
    if (a >= 1e6) return (v / 1e6).toFixed(a >= 1e7 ? 0 : 1) + "M";
    if (a >= 1e3) return (v / 1e3).toFixed(a >= 1e4 ? 0 : 1) + "k";
    return v.toFixed(v < 10 ? 2 : 0);
  };
  const commas = (v) => Math.round(v).toLocaleString("en-US");
  // Three significant figures, comma-grouped: "281,000" rather than "280,805".
  // For a figure a reader is meant to hold in their head, and whose repeats
  // disagree by more than the digits being dropped.
  const roughly = (v) => {
    if (!v) return "0";
    const scale = Math.pow(10, Math.max(0, Math.floor(Math.log10(Math.abs(v))) - 2));
    return commas(Math.round(v / scale) * scale);
  };
  const pct = (v) => (100 * v).toFixed(v * 100 < 1 && v > 0 ? 2 : 1) + "%";
  const reasonText = (reasons) => reasons.map((r) => DATA.reasonLabels[r] || r).join(", ");
  // The most severe reason a level tripped, as the short code a heatmap cell
  // has room for; a trailing + says the hover has more.
  const worstReason = (reasons) =>
    DATA.reasonSeverity.find((r) => reasons.indexOf(r) >= 0) || reasons[0];
  const reasonCode = (reasons) => {
    if (!reasons || !reasons.length) return "";
    const worst = worstReason(reasons);
    return (DATA.reasonCodes[worst] || worst) + (reasons.length > 1 ? "+" : "");
  };
  // How a level's calls failed, which the verdict alone does not say. The
  // connect count leads when there is one: a level nothing could dial into is
  // a different animal from one whose calls timed out, and both otherwise
  // surface only as an error rate.
  const failureBreakdown = (p) => {
    const parts = [];
    if (p.connectFailures) parts.push(p.connectFailures + " could not connect");
    if (p.timeouts) parts.push(commas(p.timeouts) + " timed out");
    if (p.connectionLost) parts.push(commas(p.connectionLost) + " lost the connection");
    if (p.otherErrors) parts.push(commas(p.otherErrors) + " other error" + (p.otherErrors === 1 ? "" : "s"));
    // No calls at all is the loudest thing this can say, so it is said first
    // and on its own: the level's error rate is 100% because nothing got
    // through to count, not because that many calls came back bad.
    if (!p.attempted) {
      return "no calls issued in the timed window" + (parts.length ? " — " + parts.join(" · ") : "");
    }
    return parts.join(" · ");
  };
  const levelText = (p) => p.clients + "c x " + p.outstanding + "o";

  const base = () => {
    const t = theme();
    return {
      paper_bgcolor: t.surface, plot_bgcolor: t.surface,
      font: { family: DATA.font, size: 12, color: t.secondary },
      margin: { l: 12, r: 20, t: 10, b: 36 },
      hoverlabel: { bgcolor: t.hover, bordercolor: t.border,
                    font: { family: DATA.font, size: 12, color: t.primary } },
      showlegend: false,
    };
  };
  const axis = (extra) => Object.assign({
    gridcolor: theme().grid, zerolinecolor: theme().axis, linecolor: theme().axis,
    tickfont: { color: theme().muted, size: 11 }, automargin: true,
  }, extra || {});
  const CONFIG = { displayModeBar: false, responsive: true };

  // --- lead card: the whole benchmark as one number per implementation -----
  //
  // Everything below this card answers "where is the limit" in the units the
  // limit is actually expressed in — concurrency, tail latency, error rate —
  // and every one of those needs a paragraph before it means anything. This
  // card answers "who got more done" instead, which needs none: a bar twice as
  // long is twice as much work.
  //
  // Deliberately one encoded quantity and a linear axis. The log axes further
  // down are there because a frontier spans three orders of magnitude and the
  // shape matters; here the ratio between the bars *is* the finding, and a log
  // axis would flatten a 24x difference into "a bit longer". The cost is that
  // the smallest implementation's bar is very short — which is the honest
  // picture, and the direct labels carry its number regardless.
  function headlineFigure() {
    const t = theme();
    // Ascending: Plotly stacks horizontal categories bottom-up, so this puts
    // the biggest bar at the top where a reader starts.
    const rows = DATA.summary.filter((s) => s.peakOpsPerSecond > 0)
      .slice().sort((a, b) => a.peakOpsPerSecond - b.peakOpsPerSecond);
    return {
      data: [{
        type: "bar", orientation: "h",
        x: rows.map((r) => r.peakOpsPerSecond),
        y: rows.map((r) => DATA.labels[r.implementation]),
        marker: { color: rows.map((r) => t[r.implementation] || t.muted), cornerradius: 4,
                  line: { color: t.surface, width: 2 } },
        // Direct-labelled because the bar lengths span 24x: the short bars are
        // unreadable against the axis, and this is also the relief the light
        // theme's green owes for sitting under 3:1 against the surface.
        //
        // Rounded, unlike everywhere else in this report. Repeats of one
        // unchanged load level on a busy machine have come back up to 1.9x
        // apart, so "280,805" claims five digits of precision the measurement
        // does not have; three is already generous. The hover keeps the figure
        // the rest of the report uses.
        text: rows.map((r) => roughly(r.peakOpsPerSecond)),
        textposition: "outside", cliponaxis: false,
        outsidetextfont: { color: t.primary, size: 15 },
        customdata: rows.map((r) => [
          r.peak ? levelText(r.peak) : "—",
          r.edge ? commas(r.edge.concurrency) : null,
        ]),
        hovertemplate: "<b>%{y}</b><br>%{x:,} requests finished every second"
          + "<br>its best load level was %{customdata[0]}"
          + "<extra></extra>",
      }],
      layout: Object.assign(base(), {
        margin: { l: 12, r: 120, t: 6, b: 46 },
        // tozero so the bars start at nothing and their lengths can be compared
        // by eye, which is the entire job of this card.
        xaxis: axis({ rangemode: "tozero", tickformat: ",",
                      title: { text: "requests finished every second", font: { size: 11 } } }),
        // The one card where the implementation name is the reader's entry
        // point rather than a series key, so it gets body-text weight instead
        // of the muted tick styling the detail charts use.
        yaxis: axis({ automargin: true, tickfont: { color: t.primary, size: 14 } }),
        bargap: 0.45,
      }),
    };
  }

  // --- card 1: throughput / latency vs concurrency -----------------------
  let metric = "ops";

  // Both axes are quantised, so marks collide exactly rather than merely
  // crowding: x is a product of powers of two, and a p99 read off a
  // log-bucketed histogram lands on one of a fixed ladder of microsecond
  // edges — 16 sub-buckets inside each octave means the gap between
  // adjacent bucket edges is about 7%, well below the gap between
  // adjacent x values. Two implementations that measured the same
  // bucket at the same load would land on the identical pixel and the
  // later trace would paint the earlier one out of existence — a dot
  // that is in the table and not on the chart. Nudging each
  // implementation off the true x by a few percent keeps every mark
  // visible; the gap between adjacent x values is 100%, so the dodge
  // stays far inside it, and the tooltip reports the true concurrency.
  const dodgeFactor = (index, count) => (count > 1 ? 1 + 0.11 * (index - (count - 1) / 2) : 1);

  function scaleFigure() {
    const t = theme();
    const count = DATA.implementations.length;
    const data = DATA.implementations.map((impl, index) => {
      const rows = POINTS.filter((p) => p.implementation === impl && (metric === "ops" ? p.opsPerSecond > 0 : p.p99Ms != null));
      const y = rows.map((p) => (metric === "ops" ? p.opsPerSecond : p.p99Ms));
      const nudge = dodgeFactor(index, count);
      return {
        type: "scatter", mode: "markers", name: DATA.labels[impl],
        x: rows.map((p) => p.concurrency * nudge), y,
        marker: {
          size: 10, color: t[impl] || t.muted, opacity: 0.85,
          symbol: rows.map((p) => (p.failed ? "circle-open" : "circle")),
          // A held mark gets the 2px surface ring, so levels of one
          // implementation that measured the same bucket read as a stack
          // rather than as one dot; a mark past the limit is hollow already
          // and outlines itself in the implementation's own colour.
          line: {
            color: rows.map((p) => (p.failed ? t[impl] || t.muted : t.surface)),
            width: 2,
          },
        },
        // The breakdown carries its own line break, because a hovertemplate
        // has no way to omit one for a value that turned out to be empty.
        customdata: rows.map((p) => {
          const breakdown = failureBreakdown(p);
          return [levelText(p), p.opsPerSecond, p.p99Ms == null ? null : p.p99Ms,
                  pct(p.errorRate), p.failed ? reasonText(p.reasons) : "held", p.concurrency,
                  breakdown ? "<br>" + breakdown : ""];
        }),
        hovertemplate: "<b>" + esc(DATA.labels[impl]) + "</b> " + "%{customdata[0]}"
          + "<br>%{customdata[5]:,} concurrent requests"
          + "<br>%{customdata[1]:,.0f} ops/s · err %{customdata[3]}"
          + (metric === "latency" ? "<br>p99 %{customdata[2]:.2f} ms" : "")
          + "<br>%{customdata[4]}"
          + "%{customdata[6]}<extra></extra>",
      };
    }).filter((trace) => trace.x.length);
    return {
      data,
      layout: Object.assign(base(), {
        margin: { l: 14, r: 16, t: 6, b: 40 },
        xaxis: axis({ type: "log", title: { text: "concurrent requests in flight (clients x outstanding)", font: { size: 11 } } }),
        yaxis: axis({ type: "log", title: { text: metric === "ops" ? "successful calls/second" : "p99 latency (ms)", font: { size: 11 } } }),
      }),
    };
  }

  // --- card 2: per-implementation frontier heatmaps -----------------------
  function frontierFigure(impl) {
    const t = theme();
    const xs = DATA.outstandingAxis, ys = DATA.clientsAxis;
    const rows = POINTS.filter((p) => p.implementation === impl);
    const byCell = new Map(rows.map((p) => [p.outstanding + ":" + p.clients, p]));
    const z = ys.map((clients) => xs.map((outstanding) => {
      const p = byCell.get(outstanding + ":" + clients);
      return p ? (p.failed ? 0 : 1) : null;
    }));
    // Colour carries the state (held / gave up) and the label inside a failed
    // cell carries why, so the reason does not need five more hues — and a
    // level that tripped several criteria is not forced to pick one colour.
    const label = ys.map((clients) => xs.map((outstanding) => {
      const p = byCell.get(outstanding + ":" + clients);
      return p && p.failed ? reasonCode(p.reasons) : "";
    }));
    const detail = ys.map((clients) => xs.map((outstanding) => {
      const p = byCell.get(outstanding + ":" + clients);
      if (!p) return "not tested";
      const breakdown = failureBreakdown(p);
      return commas(p.opsPerSecond) + " ops/s"
        + (p.failed ? " — gave up: " + reasonText(p.reasons) : " — held")
        + (breakdown ? "<br>" + breakdown : "");
    }));
    return {
      data: [{
        type: "heatmap", x: xs.map(String), y: ys.map(String), z,
        text: label, texttemplate: "%{text}", customdata: detail,
        // White on the critical red; the held cells carry no label at all, so
        // one ink colour is enough and never lands on the green.
        textfont: { size: 9, color: "#ffffff", family: DATA.font },
        hovertemplate: impl + " · %{y}c x %{x}o<br>%{customdata}<extra></extra>",
        colorscale: [[0, t.critical], [0.5, t.critical], [0.5, t.good], [1, t.good]],
        zmin: 0, zmax: 1, showscale: false, xgap: 2, ygap: 2,
      }],
      layout: Object.assign(base(), {
        margin: { l: 32, r: 8, t: 4, b: 30 },
        xaxis: axis({ type: "category", title: { text: "outstanding", font: { size: 10 } } }),
        yaxis: axis({ type: "category", title: { text: "clients", font: { size: 10 } }, autorange: "reversed" }),
      }),
    };
  }

  // --- card 3: headline comparison -----------------------------------------
  function summaryFigure() {
    const t = theme();
    const rows = DATA.summary.filter((s) => s.sustained).slice().sort((a, b) => a.sustained.concurrency - b.sustained.concurrency);
    return {
      data: [{
        type: "bar", orientation: "h",
        x: rows.map((r) => r.sustained.concurrency),
        y: rows.map((r) => DATA.labels[r.implementation]),
        marker: { color: rows.map((r) => t[r.implementation] || t.muted), cornerradius: 4,
                  line: { color: t.surface, width: 2 } },
        text: rows.map((r) => levelText(r.sustained) + " = " + commas(r.sustained.concurrency)),
        textposition: "outside", cliponaxis: false,
        outsidetextfont: { color: t.primary, size: 12 },
        customdata: rows.map((r) => [
          commas(r.sustained.opsPerSecond),
          r.edge ? levelText(r.edge) + " (" + reasonText(r.edge.reasons) + ")" : "ramp cap reached — no limit found",
        ]),
        hovertemplate: "<b>%{y}</b><br>sustained %{x:,} concurrent requests, %{customdata[0]} ops/s"
          + "<br>gave up at %{customdata[1]}<extra></extra>",
      }],
      layout: Object.assign(base(), {
        margin: { l: 12, r: 150, t: 6, b: 40 },
        xaxis: axis({ type: "log", title: { text: "sustained concurrent requests (log scale)", font: { size: 11 } } }),
        yaxis: axis({ automargin: true }),
        bargap: 0.4,
      }),
    };
  }

  // --- tables --------------------------------------------------------------
  const table = (head, rows) =>
    "<table><thead><tr>" + head.map((h) => "<th>" + h + "</th>").join("") + "</tr></thead><tbody>"
    + rows.map((r) => "<tr>" + r.join("") + "</tr>").join("") + "</tbody></table>";
  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const cell = (v) => "<td>" + esc(v) + "</td>";
  const name = (v) => '<td class="name">' + esc(v) + "</td>";
  const num = (v) => '<td class="num">' + esc(v) + "</td>";

  function renderTables() {
    $("table-scale").innerHTML = table(
      ["Implementation", "Level", "Concurrency", "ops/s", "Error rate", "p99 ms", "Verdict", "How it failed"],
      POINTS.slice().sort((a, b) => a.concurrency - b.concurrency || a.implementation.localeCompare(b.implementation)).map((p) => [
        cell(DATA.labels[p.implementation]), cell(levelText(p)), num(commas(p.concurrency)),
        num(commas(p.opsPerSecond)), num(pct(p.errorRate)),
        num(p.p99Ms == null ? "—" : p.p99Ms.toFixed(2)),
        cell(p.failed ? reasonText(p.reasons) : "held"),
        name(failureBreakdown(p) || "—"),
      ]));

    $("table-frontier").innerHTML = table(
      ["Implementation", "Clients", "Outstanding", "ops/s", "Verdict"],
      POINTS.slice().sort((a, b) => a.implementation.localeCompare(b.implementation) || a.clients - b.clients || a.outstanding - b.outstanding)
        .map((p) => [cell(DATA.labels[p.implementation]), num(p.clients), num(p.outstanding),
                     num(commas(p.opsPerSecond)), cell(p.failed ? reasonText(p.reasons) : "held")]));

    $("table-summary").innerHTML = table(
      ["Implementation", "Peak ops/s", "Knee (cheapest load at peak)", "Sustained level",
       "Sustained concurrency", "Gave up at"],
      DATA.summary.map((s) => [
        cell(DATA.labels[s.implementation]),
        num(s.peak ? commas(s.peak.opsPerSecond) : "—"),
        cell(s.knee ? levelText(s.knee) + " (" + s.knee.concurrency + " concurrent)" : "—"),
        cell(s.sustained ? levelText(s.sustained) : "—"),
        num(s.sustained ? commas(s.sustained.concurrency) : "—"),
        cell(s.edge ? levelText(s.edge) + " — " + reasonText(s.edge.reasons) : "ramp cap reached, no limit found"),
      ]));
  }

  // --- legends and captions -------------------------------------------------
  function renderLegends() {
    const t = theme();
    $("legend-scale").innerHTML = DATA.implementations.map((impl) =>
      '<span class="chip"><i style="background:' + t[impl] + '"></i>' + esc(DATA.labels[impl]) + "</span>").join("")
      + '<span class="chip"><i class="ring"></i>past its limit</span>';
    // Only the codes actually written on a cell — which is not the same as the
    // reasons the document contains. A reason that always loses the severity
    // tie-break (an error rate beside a broken connection, say) never appears
    // as a label, and keying a code the reader cannot find is worse than
    // leaving it to the hover, which spells every reason out in full anyway.
    const failed = POINTS.filter((p) => p.failed && p.reasons.length);
    const present = DATA.reasonSeverity.filter((reason) =>
      failed.some((p) => worstReason(p.reasons) === reason));
    $("legend-frontier").innerHTML =
      '<span class="chip"><i style="background:' + t.good + '"></i>held</span>'
      + '<span class="chip"><i style="background:' + t.critical + '"></i>gave up</span>'
      + '<span class="chip"><i style="background:transparent;border:1px dashed ' + t.axis + '"></i>not tested</span>'
      + present.map((reason) =>
          '<span class="chip"><b>' + esc(DATA.reasonCodes[reason] || reason) + "</b> "
          + esc(DATA.reasonLabels[reason] || reason) + "</span>").join("")
      + (failed.some((p) => p.reasons.length > 1)
          ? '<span class="chip"><b>+</b> also tripped others (hover)</span>' : "");
  }

  function renderCaptions() {
    // Written for someone who will read this card and nothing else, so it
    // spends its words on what the number means rather than on how it was
    // measured: no criterion names, no units beyond "requests a second", and
    // the ratio spelled out because "281,000 vs 12,000" is a division most
    // readers will not do.
    const lead = DATA.summary.filter((s) => s.peakOpsPerSecond > 0)
      .slice().sort((a, b) => b.peakOpsPerSecond - a.peakOpsPerSecond);
    if (lead.length) {
      const best = lead[0], worst = lead[lead.length - 1];
      const ratio = worst.peakOpsPerSecond > 0 ? best.peakOpsPerSecond / worst.peakOpsPerSecond : null;
      const edges = lead.filter((s) => s.edge);
      $("cap-headline").textContent =
        "Each server was asked to do more and more at once until it started dropping "
        + "requests or taking too long to answer. The bars show how much it was getting "
        + "through just before that happened."
        + (lead.length > 1 && ratio && ratio >= 1.5
            ? " " + DATA.labels[best.implementation] + " gets through about "
              + (ratio >= 10 ? Math.round(ratio) : ratio.toFixed(1))
              + " times as much work as " + DATA.labels[worst.implementation] + "."
            : "")
        + (edges.length
            ? " Past its own limit each one keeps accepting work but stops finishing it, "
              + "which is what the charts below measure."
            : "");
    } else {
      $("cap-headline").textContent = "";
    }
    const withOps = POINTS.filter((p) => p.opsPerSecond > 0);
    const peak = withOps.length ? withOps.reduce((a, b) => (b.opsPerSecond > a.opsPerSecond ? b : a)) : null;
    $("cap-scale").textContent = peak
      ? "Highest completed throughput recorded: " + DATA.labels[peak.implementation] + " at "
        + commas(peak.opsPerSecond) + " ops/s (" + levelText(peak) + "). Hollow markers are load "
        + "levels that already beat that implementation. Several load levels share a "
        + "concurrency — 1c x 4o and 4c x 1o both offer four — so implementations are nudged "
        + "a few percent apart horizontally to keep coincident marks visible; hover for the "
        + "true figure. Latency comes from a 16-subbucket log histogram, so a p99 is the bucket's upper "
        + "edge rather than an exact measurement — bucket edges inside one octave are about 7% apart, "
        + "and levels in the same bucket stack. The table lists every level."
      : "";
    const tested = DATA.implementations.length;
    const cappedCount = DATA.summary.filter((s) => !s.edge).length;
    $("cap-frontier").textContent = tested + " implementation(s) traced. "
      + (cappedCount ? cappedCount + " reached the ramp cap without a limit being found; " : "")
      + "A red cell is labelled with the criterion it tripped, and a trailing + means it "
      + "tripped more than one; hover any cell for its throughput and the full list. Blank "
      + "cells past a row's red one were never run — the ramp stops a row at its edge.";
    const ranked = DATA.summary.filter((s) => s.sustained).slice().sort((a, b) => b.sustained.concurrency - a.sustained.concurrency);
    $("cap-summary").textContent = ranked.length
      ? "Went furthest: " + DATA.labels[ranked[0].implementation] + ", sustaining "
        + levelText(ranked[0].sustained) + " (" + commas(ranked[0].sustained.concurrency) + " concurrent requests)"
        + (ranked.length > 1 ? " — " + (ranked[0].sustained.concurrency / ranked[ranked.length - 1].sustained.concurrency).toFixed(1)
            + "x further than " + DATA.labels[ranked[ranked.length - 1].implementation] + "." : ".")
      : "";
  }

  // --- wiring ----------------------------------------------------------------
  function draw() {
    const f0 = headlineFigure(), f1 = scaleFigure(), f3 = summaryFigure();
    Plotly.react("plot-headline", f0.data, f0.layout, CONFIG);
    Plotly.react("plot-scale", f1.data, f1.layout, CONFIG);
    Plotly.react("plot-summary", f3.data, f3.layout, CONFIG);

    const grid = $("plot-frontier");
    if (!grid.children.length) {
      DATA.implementations.forEach((impl) => {
        // The headline "why" for this implementation, spelled out rather than
        // coded: the cells answer it per load level, this answers it for the
        // frontier as a whole, which is the question the card is asking.
        const found = DATA.summary.find((s) => s.implementation === impl);
        const edge = found && found.edge;
        // Naming the baseline beside the verdict, because every p99 verdict in
        // this column is a multiple of it and it is measured, not configured.
        const baseline = found && found.baselineP99Ms != null
          ? " · lightest-load p99 " + found.baselineP99Ms.toFixed(2) + " ms" : "";
        // The capacity number, which the edge above is not: where the work
        // stops growing rather than where the server stops coping.
        const knee = found && found.knee
          ? " · peak " + compact(found.peakOpsPerSecond) + " ops/s, reached by "
            + levelText(found.knee) + " (" + found.knee.concurrency + " concurrent)"
          : "";
        const cell = document.createElement("div");
        cell.className = "cell";
        const noisy = found && found.nonMonotone
          ? " ⚠ a lighter level failed while a heavier one held — this run did not"
            + " separate the limit from its own noise; raise samples or step_seconds"
          : "";
        cell.innerHTML = "<h3>" + esc(DATA.labels[impl]) + "</h3>"
          + '<p class="edge">' + (edge
              ? "gave up at " + esc(levelText(edge)) + " — " + esc(reasonText(edge.reasons))
              : "ramp cap reached, no limit found") + esc(knee) + esc(baseline) + esc(noisy) + "</p>"
          + '<div class="plot" id="plot-frontier-' + impl + '"></div>';
        grid.appendChild(cell);
      });
    }
    DATA.implementations.forEach((impl) => {
      const f = frontierFigure(impl);
      Plotly.react("plot-frontier-" + impl, f.data, f.layout, CONFIG);
    });

    renderLegends();
    renderCaptions();
    renderTables();
  }

  document.querySelectorAll(".metricbtn").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".metricbtn").forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
      metric = button.dataset.metric === "latency" ? "latency" : "ops";
      draw();
    });
  });

  document.querySelectorAll("[data-table]").forEach((button) => {
    button.addEventListener("click", () => {
      const on = button.getAttribute("aria-pressed") === "true";
      button.setAttribute("aria-pressed", String(!on));
      $("table-" + button.dataset.table).classList.toggle("hidden", on);
    });
  });

  $("themetoggle").addEventListener("click", () => {
    const next = themeName() === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    $("themetoggle").textContent = next === "dark" ? "Light theme" : "Dark theme";
    draw();
  });
  $("themetoggle").textContent = themeName() === "dark" ? "Light theme" : "Dark theme";

  draw();
  window.addEventListener("resize", () => {
    document.querySelectorAll(".plot").forEach((el) => { if (el.id) Plotly.Plots.resize(el); });
  });
})();
"""
