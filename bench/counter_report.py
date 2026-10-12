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

"""Interactive counter reports, kept outside the measurement source fingerprint."""

from contextlib import closing
from html import escape
import json
from pathlib import Path

from common.bench_db import BenchDB
from subscription.strategy import describe as describe_search
from plotly.offline import get_plotlyjs as plotly_script

REPORT_MARKER = '<meta name="counter-dashboard" content="6">'


def workload_note(sources):
    """Keep SDK exceptions and historical workload semantics above the plots."""
    history = ""
    if "application_update" in sources:
        history = (
            "<p>Saved application-update observations use periodic local writes. "
            "Older observations without counter-source metadata also use that workload; "
            "they have not been converted into callback measurements.</p>"
        )
    return (
        '<aside class="workload-note"><p><strong>Counter generation and SDK exceptions</strong></p>'
        "<p>New open62541, o6-python, node-opcua, UA-.NETStandard and Milo runs increment each item's UInt64 "
        "counter in its SDK read callback. The SDK's subscription sampling drives these reads; "
        "there is no application loop updating all items. Counters advance only during the armed run. "
        "Server observations count callback increments in the retained window; client observations "
        "use delivered counter differences. SDK timer drift and scheduling overhead can reduce callback "
        "counts even at small loads; counts are not rescaled to remove these deficits.</p>"
        "<p><strong>Application updates:</strong> asyncua, gopcua and S2OPC notify on changes rather than "
        "periodically sampling read callbacks. Their application loops update every item at the configured "
        "sampling period and skip missed slots. asyncua and gopcua report the publishing interval as their "
        "revised sampling interval; S2OPC reports zero (change-triggered sampling). Their results include "
        "application-update work and measure a different counter generation path.</p>"
        "<p><strong>node-opcua-fronts exception:</strong> with FrontThreadEngine the monitored items live in a "
        "session worker that samples the shared store; a read callback of the engine thread would cost a "
        "round trip per sample. The engine thread therefore writes every item at the configured sampling "
        "period, as for asyncua, and server observations count those writes.</p>"
        + history
        + "</aside>"
    )


def confidence_text(confidence, expected):
    """Explain the saved estimate without implying confidence in a capacity boundary."""
    status = confidence.get("status", "not recorded")
    n = confidence.get("intervals")
    block = confidence.get("block_intervals")
    text = f"{status.capitalize()}. "
    if confidence.get("lower") is not None and confidence.get("upper") is not None:
        lower, upper = confidence["lower"], confidence["upper"]
        text += (
            f"Approx. {confidence.get('level', 0.95):.0%} CI for mean progress: {lower:.2%}–{upper:.2%}; "
            f"mean count error {(lower - 1) * expected:+.2f} to {(upper - 1) * expected:+.2f} increments/item. "
        )
    if n is not None:
        text += f"{n} retained publishing intervals. "
    if block:
        text += f"Circular block bootstrap, {block}-interval blocks, {confidence.get('resamples', 2000)} resamples. "
    if status == "limited data":
        text += "Few blocks of observed time: the uncertainty estimate is weak. "
    elif status == "no observed variation":
        text += "A flat trace cannot estimate unseen disturbances; no confidence interval is claimed. "
    elif status == "insufficient data":
        text += "Too few intervals to estimate uncertainty. "
    elif status == "approximate":
        text += "The estimate assumes representative conditions and dependence captured by the chosen block length. "
    elif status == "not recorded":
        text += "This observation has no saved confidence estimate. "
    return text + "This does not quantify certainty in the worst item, capacity, or validity of the statistical method."


def dashboard_data(rows, capacities, requested):
    observations = []
    for row in rows:
        observation = row["observation"]
        case, summary = observation["case"], observation["summary"]
        expected = summary["expected_per_item"]
        publishing = observation["setup"]["publishing_ms"]
        confidence = summary.get("confidence", {})
        interval_counts = observation["recording"].get("interval_counts", [])
        expected_publish = summary.get("expected_per_publish", publishing / case["sampling_ms"])
        observations.append(
            dict(
                case={**case, "publishing_ms": publishing},
                counter_source=observation.get("server", {}).get("counter_source", "application_update"),
                server_sdk_clock=observation.get("server", {}).get("sdk_clock"),
                client_sdk_clock=observation["setup"].get("sdk_clock"),
                expected=expected,
                worst_delta=summary["observed_min"] - expected,
                mean_delta=(summary["progress_fraction_mean"] - 1) * expected,
                best_delta=summary["observed_max"] - expected,
                threshold_delta=-0.01 * expected,
                passed=summary["progress_fraction_min"] >= 0.99,
                confidence=confidence,
                confidence_text=confidence_text(confidence, expected),
                interval_delta=[count / case["items"] - expected_publish for count in interval_counts],
                expected_publish=expected_publish,
                retained_ms=summary.get("measured_duration_ms"),
                warmup_ms=summary.get("warmup_ms"),
            )
        )
    return dict(observations=observations, capacities=capacities, requested=requested)


def build_page(database):
    with closing(BenchDB(database, "subscription")) as store:
        metadata = store.stored_metadata
        payload = dashboard_data(store.results.values(), metadata.get("capacities", []), metadata.get("requested_cases", []))
        name, complete, failures = store.name, metadata.get("complete", False), store.failures
        uniform, _ = store.get_config("subscription")
    # Escape script terminators even when a stored name/error contains HTML.
    encoded = json.dumps(payload, allow_nan=False).replace("<", "\\u003c").replace("&", "\\u0026")
    script = Path(__file__).with_name("counter_plots.js").read_text()
    errors = "".join(f"<li>{escape(str(failure['configuration']))}: {escape(failure['message'])}</li>" for failure in failures)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">{REPORT_MARKER}<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Subscription counter · {escape(name)}</title>
<style>
body{{font:15px system-ui,sans-serif;color:#243247;background:#f3f6fa;margin:0}}
main{{max-width:1400px;margin:auto;padding:24px}}h1{{margin-bottom:8px}}h2{{margin-top:0}}
p{{line-height:1.55}}section,.controls{{background:white;border:1px solid #dce3ed;border-radius:10px;padding:22px;margin:18px 0}}
.workload-note{{background:#fff4d6;border:2px solid #b7791f;border-radius:8px;padding:0 20px;margin:18px 0}}
.controls{{display:flex;gap:24px;flex-wrap:wrap}}label{{display:flex;gap:8px;align-items:center}}
select{{font:inherit;padding:7px;border:1px solid #b8c4d5;border-radius:5px;max-width:100%}}
.plot{{min-height:400px}}.muted{{color:#53657c}}.scroll{{overflow-x:auto}}table{{border-collapse:collapse;width:100%}}
th,td{{text-align:left;padding:10px;border-bottom:1px solid #e2e8f0;vertical-align:top}}th{{white-space:nowrap}}
td:last-child{{min-width:300px}}.empty{{padding:24px;color:#53657c}}details{{margin-top:14px}}pre{{white-space:pre-wrap}}
@media(max-width:650px){{main{{padding:10px}}section{{padding:12px}}.controls{{gap:12px}}label{{flex-wrap:wrap}}}}
</style></head><body><main>
<h1>Subscription counter · {escape(name)}</h1>
<p class="muted">{'Complete' if complete else 'Partial run'} · {len(payload['observations'])} saved candidate observations.
Client and server are measured in separate runs. All plots use saved measurements.</p>
{workload_note({row['counter_source'] for row in payload['observations']})}
<div class="controls" aria-label="Plot filters">
<label>Sampling interval <select id="sampling" aria-label="Sampling interval"></select></label>
<label>Publishing interval <select id="publishing" aria-label="Publishing interval"></select></label>
<label>Measurement <select id="observer" aria-label="Measurement"><option value="all">Client + server</option>
<option value="client">Client</option><option value="server">Server</option></select></label>
</div>
<p id="selection-status" role="status"></p>
<section><h2>Observed capacity</h2>
<p>Largest passing load and first failing load in each completed search. Every item's count must reach 99% of expected.
These are single-run observations, not repeatedly confirmed capacities or statistical confidence bounds.
An open diamond marks measured failure; ≥ means the configured ceiling passed.
An orange × marks setup timeout: its bar shows the last passing workload, not an established capacity.
Server startup and monitored-item creation share a five-minute limit. Incomplete searches remain labeled.</p>
<div id="capacity" class="plot"></div><div id="capacity-notes"></div></section>
<section><h2>Count error under load</h2>
<p>100 × (observed count − expected count) / expected count, per monitored item over the retained window.
Zero is ideal; negative values indicate missing progress. Solid (client) or dashed (server) lines show the worst item; dotted lines show the mean.
The gray line at −1% is the fixed 99% pass threshold. Positive client errors can arise from publishing phase and boundary timing.</p>
<div id="load" class="plot"></div></section>
<section><h2>Progress over publishing intervals</h2>
<p>Mean count error per item in each retained publishing interval. This exposes bursts and stalls hidden by totals;
it is an average across items, not the worst-item pass test. Warm-up is excluded.</p>
<label>Candidate <select id="candidate" aria-label="Candidate"></select></label>
<div id="intervals" class="plot"></div><p id="candidate-confidence"></p></section>
<section><h2>How much confidence do these measurements support?</h2>
<p>The nominal 95% interval estimates <strong>mean progress across items over time</strong> using a circular block bootstrap.
It is not a 95% probability that the method is correct, that every item passes, or that a capacity is reproducible.
Resampling cannot establish that this trace represents future conditions or captures longer stalls.
Short traces and traces without variation are explicitly qualified below; the 99% worst-item rule stays unchanged.</p>
<p>Each row is one measured SDK / observer / sampling / publishing / item-count configuration. The filters above apply here too.</p>
<div class="scroll"><table><thead><tr><th>SDK / observer</th><th>Items</th><th>Expected/item</th>
<th>Worst count error</th><th>Outcome</th><th>Confidence of mean progress</th></tr></thead><tbody id="confidence-rows"></tbody></table></div>
</section>
<details><summary>Measurement definition and saved evidence</summary>
<p>Scalar UInt64 counters, queue size 1, one open62541 client. Counter sources are saved with each new observation.
Current measurements retain at least 1000 expected increments and
at least the configured minimum publishing intervals, after 10% extra warm-up rounded up to whole publishing intervals.
{describe_search(uniform)} Historical observations retain their recorded semantics.</p>
<p>Expected progress is retained duration divided by the configured sampling period.
New native open62541 workers explicitly use CLOCK_MONOTONIC for SDK timers and measurement;
older observations without sdk_clock metadata used the SDK's CLOCK_MONOTONIC_RAW default,
which can produce biased counts when the clocks advance at different rates.
Confidence estimates do not change pass/fail decisions.</p>
<button id="download" type="button">Download plotted data (JSON)</button></details>
{'<details><summary>Recorded failures</summary><ul>' + errors + '</ul></details>' if errors else ''}
<noscript><p>Enable JavaScript to view the interactive plots and confidence table.</p></noscript>
</main><script id="counter-data" type="application/json">{encoded}</script>
<script>{plotly_script()}</script><script>{script}</script></body></html>"""
