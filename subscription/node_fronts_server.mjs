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
//    Copyright 2026 (c) Sterfive (Author: Etienne Rossignon)

// The subscription counter server for the node-opcua-fronts flavours: FrontThreadEngine on the stock
// worker's node-opcua. This thread is the engine and owns the compact store; the monitored items live in
// a session worker, which samples the store in place, and the fronts carry the client's connection.
// A worker cannot call a read callback of this thread without a round trip per sample, so the counters
// are application updates, as for asyncua: this thread writes every item once per sampling period
// during the armed window, and a server observation counts those writes (counter_source
// "application_update"). Same arguments and control lines as node_server.mjs.
import { availableParallelism } from 'node:os';
import { createInterface } from 'node:readline';
import { FrontThreadEngine, DataType, Variant, VariantArrayType } from '../common/node/sdk.mjs';

const [portText, itemsText, samplingText, measurement] = process.argv.slice(2);
const port = Number(portText), items = Number(itemsText), sampling = Number(samplingText);
const emit = value => process.stdout.write(JSON.stringify(value) + '\n');
console.log = console.info = console.warn = console.error;
const now = () => process.hrtime.bigint(); // Linux CLOCK_MONOTONIC, shared with the coordinator.
const sleep = ns => new Promise(resolve => setTimeout(resolve, Math.max(0, Math.ceil(Number(ns) / 1e6))));
let engine;
try {
  if (!['server', 'client'].includes(measurement) || !Number.isInteger(items) || items < 1 || items > 1048576
    || !Number.isInteger(sampling) || sampling < 1) throw new Error('Invalid worker arguments');
  const cpus = availableParallelism();
  const fronts = Number(process.env.O6_NODE_FRONTS) || Math.max(1, cpus - 1);
  engine = await FrontThreadEngine.create({
    applicationUri: 'urn:o6:benchmark:server',
    expectedNodes: 8192 + items,
    serverCapabilities: { maxSessions: 1, maxSubscriptions: 1, minSupportedSampleRate: 1,
      maxMonitoredItems: items, maxMonitoredItemsPerSubscription: items, maxMonitoredItemsQueueSize: 1,
      operationLimits: { maxMonitoredItemsPerCall: 256 } },
  });
  const ns = engine.registerNamespace('urn:o6:benchmark:server');
  if (ns !== 1) throw new Error('Expected namespace 1');
  const space = engine.addressSpace;
  const objects = space.findNode('ns=0;i=85');
  const counter = value => new Variant({ dataType: DataType.UInt64, arrayType: VariantArrayType.Scalar,
    value: [Math.floor(value / 0x100000000), value >>> 0] });
  const nodes = [];
  for (let i = 0; i < items; i++) {
    nodes.push(space.addVariable({ organizedBy: objects, nodeId: `ns=1;i=${i + 1}`, browseName: `Counter${i}`,
      dataType: 'UInt64', valueRank: -1, minimumSamplingInterval: 1, accessLevel: 1, userAccessLevel: 1, value: counter(0) }));
  }
  await engine.start({ fronts, serverModule: new URL('./node_fronts_options.mjs', import.meta.url), serverModuleData: { port } });
  emit({ event: 'ready', sdk: 'node-opcua', runtime: process.version, clock: 'CLOCK_MONOTONIC',
    counter_source: 'application_update', fronts: engine.frontCount, cpus });

  // Writes every item at the sampling period from start to end, skipping missed grid slots, and counts
  // the writes admitted in the measured window, as subscription/python_server.py produce() does.
  async function produce(start, end, measuredStart, measuredEnd, intervalNs) {
    const values = Array(items).fill(0);
    const measured = measurement === 'server' ? Array(items).fill(0) : null;
    const intervalCounts = measured ? Array(Number((measuredEnd - measuredStart) / intervalNs)).fill(0) : null;
    const period = BigInt(sampling) * 1_000_000n;
    let due = start;
    while (now() < end) {
      const t = now();
      if (t < due) { await sleep(due - t); continue; }
      for (let i = 0; i < items; i++) {
        const admission = now();
        if (admission >= end) break;
        values[i]++;
        nodes[i].setValueFromSource(counter(values[i]));
        if (measured && admission >= measuredStart && admission < measuredEnd) {
          measured[i]++;
          intervalCounts[Number((admission - measuredStart) / intervalNs)]++;
        }
      }
      due = start + ((now() - start) / period + 1n) * period;
      // Yield so the engine pushes this turn's changes and serves its fronts and workers.
      await sleep(0n);
    }
    emit(measured ? { event: 'result', counts: measured, interval_counts: intervalCounts } : { event: 'done' });
  }

  let producer;
  const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
  for await (const line of input) {
    if (line === 'quit') break;
    const parts = line.split(' ');
    if (parts.length !== 6 || parts[0] !== 'arm' || producer) throw new Error('Invalid control');
    const [start, end, measuredStart, measuredEnd, intervalNs] = parts.slice(1).map(BigInt);
    if (start <= now() || measuredStart < start || measuredEnd <= measuredStart || end < measuredEnd)
      throw new Error('Missed arming deadline');
    if (intervalNs <= 0n || (measuredEnd - measuredStart) % intervalNs !== 0n) throw new Error('Invalid publishing interval');
    emit({ event: 'armed', start_ns: String(start), end_ns: String(end),
      measured_start_ns: String(measuredStart), measured_end_ns: String(measuredEnd), interval_ns: String(intervalNs) });
    producer = produce(start, end, measuredStart, measuredEnd, intervalNs).catch(error => {
      emit({ event: 'error', message: String(error) });
      process.exitCode = 2;
    });
  }
  await producer;
} catch (error) {
  emit({ event: 'error', message: String(error) });
  process.exitCode = 2;
} finally {
  process.stdin.destroy();
  // The coordinator gives a worker a few seconds to exit after "quit" and records a kill as an
  // error. Stopping the front and session threads can take longer, and the measurement is over:
  // allow the engine 2 s, then leave.
  if (engine) await Promise.race([engine.shutdown().catch(() => {}), sleep(2_000_000_000n)]);
  process.exit(process.exitCode ?? 0);
}
