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

"""Evidence for a capacity estimate, including explicit unconfirmed outcomes."""

from contextlib import closing
import html
import json
import statistics

import plotly.graph_objects as go

from common.bench_db import BenchDB
from server_capacity.run import SUITE
from server_capacity.options import DEFAULT_IMPLEMENTATIONS, IMPLEMENTATIONS
from server_capacity.search import METHOD

REPORT_MARKER = "<!-- server-capacity-report:v8 -->"


def load_summaries(points, *, confirmation_only=False):
    """Keep load-specific evidence; new searches estimate spread from fresh repeats only."""
    groups = {}
    for case, obs in points:
        key = (case["implementation"], case["clients"], case["outstanding"])
        groups.setdefault(key, []).append((case, obs))
    summaries = []
    for (sdk, clients, outstanding), group in groups.items():
        healthy = [(case, obs) for case, obs in group if obs["healthy"] and "probe_error" not in obs]
        if not healthy:
            continue
        repeats = [
            (case, obs) for case, obs in group if not confirmation_only or case.get("phase", "").startswith("confirmation")
        ]
        rates = [obs["requests_per_second"] for case, obs in repeats if obs["healthy"] and "probe_error" not in obs]
        variance = statistics.variance(rates) if len(rates) > 1 else None
        summaries.append(
            dict(
                sdk=sdk,
                clients=clients,
                outstanding=outstanding,
                mean=statistics.mean(rates) if rates else None,
                maximum=max(obs["requests_per_second"] for case, obs in healthy),
                count=len(rates),
                failed_count=sum(not obs["healthy"] for case, obs in repeats),
                variance=variance,
                stddev=variance**0.5 if variance is not None else None,
            )
        )
    return sorted(summaries, key=lambda s: (s["sdk"], -s["maximum"], -(s["mean"] or 0), s["clients"], s["outstanding"]))


def throughput_summary(points, *, selections=None):
    """Separate equally weighted discovery loads from selected confirmation repeats."""
    summary = {}
    for sdk in IMPLEMENTATIONS:
        discovery = {}
        repeats = []
        failed_discovery = failed_confirmation = 0
        confirmed_loads = set()
        candidates = (
            None
            if selections is None
            else {(c["clients"], c["outstanding"]) for c in selections.get(sdk, {}).get("candidates", [])}
        )
        for case, obs in points:
            if case["implementation"] != sdk:
                continue
            load = (case["clients"], case["outstanding"])
            phase = case.get("phase", "legacy")
            healthy = obs["healthy"] and "probe_error" not in obs
            if phase in ("discovery", "legacy"):
                if healthy:
                    discovery.setdefault(load, []).append(obs["requests_per_second"])
                else:
                    failed_discovery += 1
            elif phase.startswith("confirmation") and (candidates is None or load in candidates):
                if healthy:
                    repeats.append(obs["requests_per_second"])
                    confirmed_loads.add(load)
                else:
                    failed_confirmation += 1
        discovery_rates = [statistics.mean(rates) for rates in discovery.values()]
        variance = statistics.variance(repeats) if len(repeats) > 1 else None
        summary[sdk] = dict(
            discovery_mean=statistics.mean(discovery_rates) if discovery_rates else None,
            discovery_loads=len(discovery_rates),
            discovery_failed=failed_discovery,
            confirmation_mean=statistics.mean(repeats) if repeats else None,
            confirmation_count=len(repeats),
            confirmation_loads=len(confirmed_loads),
            confirmation_failed=failed_confirmation,
            variance=variance,
            stddev=variance**0.5 if variance is not None else None,
        )
    return summary


def capacity_figure(points, *, selections=None):
    """Compare discovery-space averages with repeated measurements of selected loads."""
    summary = throughput_summary(points, selections=selections)
    # The opt-in SDKs only get a row when they were measured.
    shown = [sdk for sdk in IMPLEMENTATIONS if sdk in DEFAULT_IMPLEMENTATIONS or any(c["implementation"] == sdk for c, _ in points)]
    exploration = [summary[sdk]["discovery_mean"] for sdk in shown]
    confirmations = [summary[sdk]["confirmation_mean"] for sdk in shown]
    deviations = [summary[sdk]["stddev"] for sdk in shown]
    discovery_details = [[summary[sdk]["discovery_loads"], summary[sdk]["discovery_failed"]] for sdk in shown]
    confirmation_details = [
        [
            summary[sdk]["confirmation_count"],
            summary[sdk]["confirmation_loads"],
            f"{summary[sdk]['stddev']:,.1f}" if summary[sdk]["stddev"] is not None else "unknown (fewer than two repeats)",
            f"{summary[sdk]['variance']:,.1f}" if summary[sdk]["variance"] is not None else "unknown (fewer than two repeats)",
            summary[sdk]["confirmation_failed"],
        ]
        for sdk in shown
    ]
    labels = lambda values: [f"{value:,.0f}" if value is not None else "" for value in values]
    figure = go.Figure(
        [
            go.Bar(
                name="Search-space mean*",
                orientation="h",
                y=list(shown),
                x=exploration,
                marker_color="#94a3b8",
                customdata=discovery_details,
                text=labels(exploration),
                textposition="inside",
                insidetextanchor="middle",
                hovertemplate=(
                    "%{y}<br>Search-space mean: %{x:,.1f} requests/s"
                    "<br>Successful load configurations: %{customdata[0]}<br>Excluded probes: %{customdata[1]}"
                    "<br>*Some loads may leave the server unsaturated.<extra></extra>"
                ),
            ),
            go.Bar(
                name="Confirmation mean ±1 SD",
                orientation="h",
                y=list(shown),
                x=confirmations,
                error_x=dict(type="data", symmetric=True, array=deviations, thickness=2, width=6, color="#172554"),
                marker_color="#2878b5",
                customdata=confirmation_details,
                text=labels(confirmations),
                textposition="inside",
                insidetextanchor="middle",
                hovertemplate=(
                    "%{y}<br>Confirmation mean: %{x:,.1f} requests/s<br>Successful repeats: %{customdata[0]}"
                    "<br>Selected load configurations: %{customdata[1]}<br>Sample SD: %{customdata[2]} requests/s"
                    "<br>Sample variance: %{customdata[3]} (requests/s)²<br>Excluded repeats: %{customdata[4]}<extra></extra>"
                ),
            ),
        ]
    )
    for sdk in shown:
        if summary[sdk]["confirmation_mean"] is None:
            figure.add_annotation(
                x=0,
                y=sdk,
                xanchor="left",
                showarrow=False,
                text="No confirmation measurements" if summary[sdk]["discovery_mean"] is not None else "No successful probes",
            )
    figure.update_layout(
        title="Observed throughput by SDK",
        barmode="group",
        bargap=0.25,
        bargroupgap=0.08,
        xaxis_title="Throughput (requests/s)",
        yaxis_title="Server SDK",
        template="plotly_white",
        yaxis=dict(type="category", categoryorder="array", categoryarray=list(shown), autorange="reversed"),
        xaxis=dict(rangemode="tozero", tickformat=","),
        legend=dict(orientation="h", y=1.10),
        margin=dict(r=50, b=105),
        height=600,
    )
    figure.add_annotation(
        x=0,
        y=-0.19,
        xref="paper",
        yref="paper",
        xanchor="left",
        showarrow=False,
        align="left",
        text="*Tested successful loads only; some may leave the server unsaturated.<br>Search-space mean depends on the explored configurations.",
    )
    return figure


def build_page(database, cdn=False):
    with closing(BenchDB(database, SUITE)) as store:
        rows = list(store.results.values())
        failures = store.failures
        metadata = store.stored_metadata
        name = store.name
    escape = lambda value: html.escape(str(value))
    legacy = metadata.get("method") == "legacy_grid"
    summaries = []
    for implementation, result in metadata.get("capacities", {}).items():
        rate = result.get("capacity_requests_per_second")
        summaries.append(
            f"<tr><td>{escape(implementation)}</td><td>{escape(result['status'])}</td>"
            f"<td>{f'{rate:,.0f}' if rate is not None else 'unconfirmed'}</td>"
            f"<td>{escape(result['reason'])}</td></tr>"
        )
    points = []
    for row in rows:
        case = row["config_key"]
        observations = row.get("runs", []) if legacy else [row["observation"]]
        for observation in observations:
            points.append((case, observation))
    ranked = metadata.get("method") == METHOD
    selections = metadata.get("candidate_selections", {}) if ranked else None
    capacity_chart = capacity_figure(points, selections=selections)
    loads = []
    for load in load_summaries(points, confirmation_only=ranked):
        spread = f"{load['stddev']:,.0f}" if load["stddev"] is not None else "unknown (fewer than two probes)"
        values = [
            load["sdk"],
            f"{load['clients']}c × {load['outstanding']}o",
            f"{load['maximum']:,.0f}",
            f"{load['mean']:,.0f}" if load["mean"] is not None else "awaiting confirmation",
            spread,
            load["count"],
            load["failed_count"],
        ]
        loads.append("<tr>" + "".join(f"<td>{escape(value)}</td>" for value in values) + "</tr>")
    figure = go.Figure()
    for sdk in sorted({case["implementation"] for case, _ in points}):
        for phase in ("discovery", "confirmation", "legacy"):
            selected = [
                (case, obs)
                for case, obs in points
                if case["implementation"] == sdk and ("legacy" if legacy else case["phase"].split("-")[0]) == phase
            ]
            if not selected:
                continue
            figure.add_trace(
                go.Scatter(
                    name=f"{sdk} {phase}",
                    mode="markers",
                    x=[c["clients"] * c["outstanding"] for c, _ in selected],
                    y=[o["requests_per_second"] for _, o in selected],
                    text=[f"{c['clients']} clients × {c['outstanding']} outstanding" for c, _ in selected],
                    marker=dict(symbol=["circle" if o["healthy"] else "x" for _, o in selected]),
                )
            )
    figure.update_layout(
        xaxis_title="Total outstanding requests", xaxis_type="log", yaxis_title="Successful requests/s", template="plotly_white"
    )
    table = []
    for case, obs in points:
        values = [
            case["implementation"],
            case["clients"],
            case["outstanding"],
            case.get("phase", "legacy"),
            f"{obs['requests_per_second']:,.0f}",
            f"{obs['error_rate']:.2%}" if obs.get("error_rate") is not None else "unmeasured",
            obs["p50_ms"],
            obs["p99_ms"],
            f"{obs['client_cpu_max_fraction']:.1%}" if "client_cpu_max_fraction" in obs else "unrecorded",
            f"{obs['backpressure_wait_min_fraction']:.1%}" if "backpressure_wait_min_fraction" in obs else "unrecorded",
            (
                ", ".join(f"{code}: {count}" for code, count in obs.get("error_statuses", {}).items() if count)
                if "error_statuses" in obs
                else "unrecorded"
            ),
            obs.get("probe_error", ""),
        ]
        table.append("<tr>" + "".join(f"<td>{escape(value)}</td>" for value in values) + "</tr>")
    errors = "".join(f"<li>{escape(f['configuration'])}: {escape(f.get('error', 'failure'))}</li>" for f in failures)
    state = (
        "Historical grid: no saturation evidence"
        if legacy
        else "Search finished" if metadata.get("complete") else "Search incomplete"
    )
    evidence = escape(json.dumps(metadata.get("capacities", {}), indent=2))
    method_description = (
        "Explore client counts and pipeline depths on a bounded geometric grid; select the top 10% of successful "
        "discovery configurations (at least one), then repeat each selected configuration n times in rotated order. "
        "The selected list is saved before confirmation. Confirmation means and sample variances use only fresh confirmation "
        "measurements, never the discovery measurement that selected a candidate. Confirmed means the selected "
        "load completed its repeats successfully; it does not prove an absolute server maximum."
        if ranked
        else "Historical search: adaptive plateau checks. Its stored conclusions are preserved below. "
        "Discovery and confirmation phases remain separate; no missing repeats are fabricated."
    )
    return f"""<!doctype html><html lang="en"><meta charset="utf-8"><title>Server capacity</title>{REPORT_MARKER}<body>
<h1>Server capacity — {escape(name)}</h1><p>{state}</p>
{capacity_chart.to_html(full_html=False, include_plotlyjs='cdn' if cdn else True)}
<p><strong>*Search-space mean:</strong> each successfully tested load configuration has equal weight,
using discovery measurements only. Some loads may offer insufficient work to saturate the server,
leaving capacity unused. This average depends on the configurations explored; it is neither maximum
capacity nor typical production performance.</p>
<p><strong>Confirmation mean:</strong> combines fresh successful repeats across all selected top-decile
load configurations. Discovery measurements do not contribute. Whiskers show ±1 sample standard
 deviation across these repeats, reflecting both differences between selected loads and variation
between repeats; they are not a confidence interval or a maximum. Hover shows sample counts,
excluded failures and sample variance. Slow successful repeats remain included.
Fewer than two successful repeats means variability is unknown, not zero.</p>
<p>Failed and invalid probes are excluded from both means and retained in the evidence below.
Named runs are kept separate.</p>
<details><summary>All measured load configurations (requests/s)</summary>
<table><tr><th>SDK</th><th>Load</th><th>Maximum</th><th>Mean</th><th>Sample SD</th><th>Successful probes</th><th>Failed probes</th></tr>{''.join(loads)}</table></details>
<details><summary>Stored search conclusions</summary>
<table><tr><th>SDK</th><th>Status</th><th>Stored estimate (requests/s)</th><th>Evidence / limitation</th></tr>{''.join(summaries)}</table>
<p>Stored estimates are retained for audit; the bars above show means of the stated measurement groups.</p></details>
<p>Measurement method: {escape(metadata.get('method', 'unrecorded'))}. {method_description}</p>
{figure.to_html(full_html=False, include_plotlyjs=False)}
<table><tr><th>SDK</th><th>Clients</th><th>Outstanding/client</th><th>Phase</th><th>Requests/s</th>
<th>Error rate</th><th>p50 ms</th><th>p99 ms</th><th>Max client CPU</th><th>Min full-window wait</th><th>Error statuses</th><th>Probe failure</th></tr>{''.join(table)}</table>
<details><summary>Confirmation evidence</summary><pre>{evidence}</pre></details>
<h2>Failed measurements</h2><ul>{errors}</ul></body></html>"""
