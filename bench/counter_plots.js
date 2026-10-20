// SPDX-License-Identifier: AGPL-3.0-or-later
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU Affero General Public License as published
// by the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
// GNU Affero General Public License for more details.
//
// You should have received a copy of the GNU Affero General Public License
// along with this program. If not, see <https://www.gnu.org/licenses/>.
//
//    Copyright 2026 (c) o6 Automation GmbH (Author: Daniel Opitz)

/* Offline dashboard for saved counter observations; no measurements run here. */
(() => {
  const data = JSON.parse(document.getElementById('counter-data').textContent);
  const byId = id => document.getElementById(id);
  const colors = {'open62541': '#2563eb', 'o6-python': '#ea580c', asyncua: '#059669',
    'node-opcua': '#9333ea', 'ua-dotnet': '#db2777', milo: '#d55181', gopcua: '#4a3aa7', s2opc: '#c98500',
    'async-opcua': '#b45309'};
  const sdkOrder = Object.keys(colors);
  const label = c => `${c.implementation} / ${c.measurement}`;
  const sourceLabel = row => row.counter_source === 'read_callback' ? 'SDK read callback' : 'Application updates';
  const publishing = c => c.publishing_ms ?? 1000;
  const number = n => Number(n).toLocaleString(undefined, {maximumFractionDigits: 2});
  const signed = n => `${n > 0 ? '+' : ''}${number(n)}`;
  const config = {responsive: true, displaylogo: false, toImageButtonOptions: {format: 'svg'}};
  const cases = [...data.requested, ...data.capacities.map(c => c.case), ...data.observations.map(r => r.case)];
  let visible = [];

  function choices(id, values, preferred) {
    const select = byId(id);
    const previous = preferred ?? select.value;
    select.replaceChildren(...values.map(value => new Option(`${value} ms`, value)));
    if (values.some(value => String(value) === String(previous))) select.value = previous;
  }

  function layout(x, y, extra = {}) {
    return {font: {family: 'system-ui, sans-serif', color: '#243247'},
      margin: {l: 85, r: 30, t: 25, b: 100}, height: 460,
      xaxis: {title: {text: x}, gridcolor: '#e2e8f0'},
      yaxis: {title: {text: y}, gridcolor: '#e2e8f0', zerolinecolor: '#64748b'},
      legend: {orientation: 'h', y: -0.25}, hovermode: 'closest', ...extra};
  }

  function plot(id, traces, settings) {
    if (!traces.length) settings.annotations = [{text: 'No saved measurements for this selection',
      x: 0.5, y: 0.5, xref: 'paper', yref: 'paper', showarrow: false}];
    return Plotly.react(id, traces, settings, config);
  }

  function selected(c) {
    return c.sampling_ms === Number(byId('sampling').value)
      && publishing(c) === Number(byId('publishing').value)
      && (byId('observer').value === 'all' || c.measurement === byId('observer').value);
  }

  function ordering(a, b) {
    return sdkOrder.indexOf(a.implementation) - sdkOrder.indexOf(b.implementation)
      || a.measurement.localeCompare(b.measurement) || (a.items ?? 0) - (b.items ?? 0);
  }

  function capacityPlot() {
    const rows = data.capacities.filter(c => selected(c.case)).sort((a, b) => ordering(a.case, b.case));
    const finished = rows.filter(c => !Object.hasOwn(c, 'error'));
    const traces = [];
    if (finished.length) {
      traces.push({type: 'bar', orientation: 'h', name: 'Largest passing load',
        x: finished.map(c => c.passing_items), y: finished.map(c => label(c.case)),
        marker: {color: finished.map(c => colors[c.case.implementation])},
        text: finished.map(c => (c.passing_items === 0 ? 'No passing load' : `${c.at_least ? '≥ ' : ''}${number(c.passing_items)}`)
          + (c.status === 'setup_limited' ? ' · setup limited' : '')),
        textposition: 'outside', cliponaxis: false,
        hovertemplate: '%{y}<br>Passing items: %{text}<extra></extra>'});
      const failed = finished.filter(c => c.failing_items != null);
      if (failed.length) traces.push({type: 'scatter', mode: 'markers', name: 'First failing load',
        x: failed.map(c => c.failing_items), y: failed.map(c => label(c.case)),
        marker: {symbol: 'diamond-open', size: 12, color: '#b91c1c', line: {width: 2}},
        hovertemplate: '%{y}<br>Failing items: %{x:,}<extra></extra>'});
      const setupFailed = finished.filter(c => c.status === 'setup_limited');
      if (setupFailed.length) traces.push({type: 'scatter', mode: 'markers', name: 'Setup timed out (unmeasured)',
        x: setupFailed.map(c => c.setup_timeout_items), y: setupFailed.map(c => label(c.case)),
        marker: {symbol: 'x', size: 12, color: '#b45309'},
        hovertemplate: '%{y}<br>Setup timed out at %{x:,} items; no progress measurement<extra></extra>'});
    }
    const maximum = Math.max(1, ...finished.flatMap(c => [c.passing_items, c.failing_items ?? 0, c.setup_timeout_items ?? 0]));
    plot('capacity', traces, layout('Monitored items', '', {
      height: Math.max(360, finished.length * 55 + 150), margin: {l: 180, r: 40, t: 20, b: 90},
      xaxis: {title: {text: 'Monitored items'}, range: [0, maximum * 1.18]},
      yaxis: {autorange: 'reversed', automargin: true}}));
    const notes = byId('capacity-notes');
    notes.replaceChildren();
    const selectedCases = new Map(cases.filter(selected).map(c => [label(c), c]));
    for (const [key] of selectedCases) {
      const result = rows.find(c => label(c.case) === key);
      if (result?.status === 'setup_limited') {
        const p = document.createElement('p');
        p.textContent = `${key}: ${result.note}`;
        notes.append(p);
      }
      if (!result || Object.hasOwn(result, 'error')) {
        const p = document.createElement('p');
        p.textContent = `${key}: incomplete search${result?.error ? ` — ${result.error}` : '; capacity not established yet'}.`;
        notes.append(p);
      }
    }
  }

  function loadPlot() {
    const groups = new Map();
    for (const row of visible) {
      const key = label(row.case);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(row);
    }
    const traces = [];
    for (const [key, rows] of groups) {
      const color = colors[rows[0].case.implementation];
      const base = {type: 'scatter', mode: 'lines+markers', x: rows.map(r => r.case.items), legendgroup: key,
        customdata: rows.map(r => [r.expected, r.passed ? 'pass' : 'below target', signed(100 * r.best_delta / r.expected)])};
      traces.push({...base, name: `${key} · worst`, y: rows.map(r => 100 * r.worst_delta / r.expected),
        line: {color, dash: rows[0].case.measurement === 'server' ? 'dash' : 'solid'},
        marker: {color, symbol: rows.map(r => r.passed ? 'circle' : 'x'), size: 8},
        hovertemplate: '%{x:,} items<br>Worst deviation: %{y:+.2f}%<br>Expected: %{customdata[0]} increments/item'
          + '<br>Best deviation: %{customdata[2]}%<br>%{customdata[1]}<extra>%{fullData.name}</extra>'});
      traces.push({...base, name: `${key} · mean`, y: rows.map(r => 100 * r.mean_delta / r.expected),
        line: {color, dash: 'dot', width: 1}, marker: {color, size: 4},
        hovertemplate: '%{x:,} items<br>Mean deviation: %{y:+.2f}%<extra>%{fullData.name}</extra>'});
      traces.push({type: 'scatter', mode: 'lines', x: rows.map(r => r.case.items), y: rows.map(r => 100 * r.threshold_delta / r.expected),
        name: '99% worst-item threshold', legendgroup: 'threshold', showlegend: traces.length === 2,
        line: {color: '#64748b', dash: 'dash', width: 1},
        hovertemplate: 'Passing threshold: %{y:+.2f}%<extra></extra>'});
    }
    plot('load', traces, layout('Monitored items (log scale)', 'Deviation from expected count (%)', {
      height: 540, xaxis: {title: {text: 'Monitored items (log scale)'}, type: 'log'},
      margin: {l: 85, r: 25, t: 20, b: 160}}));
  }

  function intervalPlot() {
    const row = visible[Number(byId('candidate').value)];
    const traces = row?.interval_delta.length ? [{type: 'scatter', mode: 'lines+markers', name: label(row.case),
      x: row.interval_delta.map((_, i) => (i + 1) * publishing(row.case) / 1000), y: row.interval_delta,
      line: {color: colors[row.case.implementation]},
      hovertemplate: 'Interval ending %{x:g} s<br>Mean error: %{y:+.2f} increments/item<extra></extra>'}] : [];
    plot('intervals', traces, layout('Time since retained window start (s)', 'Mean interval error (increments/item)',
      {showlegend: false, height: 400, margin: {l: 85, r: 25, t: 20, b: 65}}));
    byId('candidate-confidence').textContent = row
      ? `${sourceLabel(row)}. Expected per interval: ${number(row.expected_publish)} increments/item. ${row.confidence_text}`
      : 'No candidate observation for this selection.';
  }

  function render() {
    visible = data.observations.filter(r => selected(r.case)).sort((a, b) => ordering(a.case, b.case));
    byId('selection-status').textContent = `${visible.length} candidate observations · sampling ${byId('sampling').value || '—'} ms`
      + ` · publishing ${byId('publishing').value || '—'} ms`;
    capacityPlot();
    loadPlot();
    const previous = byId('candidate').selectedOptions[0]?.textContent;
    byId('candidate').replaceChildren(...visible.map((r, i) =>
      new Option(`${label(r.case)} · ${number(r.case.items)} items · ${r.passed ? 'pass' : 'below target'}`, i)));
    const preserved = Array.from(byId('candidate').options).find(o => o.textContent === previous);
    if (preserved) byId('candidate').value = preserved.value;
    else if (visible.length) {
      const firstGroup = visible.filter(r => label(r.case) === label(visible[0].case));
      const passing = firstGroup.filter(r => r.passed);
      const chosen = passing.length ? passing[passing.length - 1] : firstGroup[0];
      byId('candidate').value = visible.indexOf(chosen);
    }
    intervalPlot();
    byId('confidence-rows').replaceChildren(...visible.map(row => {
      const tr = document.createElement('tr');
      for (const value of [label(row.case), number(row.case.items), number(row.expected), signed(row.worst_delta),
        row.passed ? 'Pass' : 'Below target', row.confidence_text]) {
        const td = document.createElement('td');
        td.textContent = value;
        tr.append(td);
      }
      return tr;
    }));
  }

  function publishingChoices() {
    choices('publishing', [...new Set(cases.filter(c => c.sampling_ms === Number(byId('sampling').value))
      .map(publishing))].sort((a, b) => a - b));
  }
  choices('sampling', [...new Set(cases.map(c => c.sampling_ms))].sort((a, b) => a - b));
  publishingChoices();
  const observers = [...new Set(cases.map(c => c.measurement))];
  if (observers.length === 1) byId('observer').value = observers[0];
  byId('sampling').addEventListener('change', () => {publishingChoices(); render();});
  byId('publishing').addEventListener('change', render);
  byId('observer').addEventListener('change', render);
  byId('candidate').addEventListener('change', intervalPlot);
  byId('download').addEventListener('click', () => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'}));
    const a = document.createElement('a');
    a.href = url;
    a.download = 'subscription-counter-plots.json';
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  render();
})();
