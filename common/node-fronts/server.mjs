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

// node-opcua with front threads (FrontThreadEngine), on the stock worker's pinned node-opcua install
// (common/node). This thread is the engine: it owns the compact store and applies every write. The
// fronts are ways into that one server on the same port (SO_REUSEPORT; Linux spreads connections over
// them by hash) and answer Reads from the store in place. One front per CPU of this process's affinity
// but one, which is left to the engine: the suites pin the server before it starts, so this is its
// share of the machine. O6_NODE_FRONTS overrides the count.
import { availableParallelism } from 'node:os';
import { DataType, Variant, VariantArrayType, FrontThreadEngine } from '../node/sdk.mjs';
import { options } from '../node/options.mjs';
import { serverProfile } from '../node/profile.mjs';
import { FIRST_NODE_ID, NODE_COUNT, ARRAY_FIRST_NODE_ID, SERVER_READY, arrayPayload, output } from '../node/contract.mjs';

const cpus = availableParallelism();
const fronts = Number(process.env.O6_NODE_FRONTS) || Math.max(1, cpus - 1);
const o = options();
// The limits are the one server's, applied to all fronts together: set on the engine, not per front.
const engine = await FrontThreadEngine.create({
  applicationUri: 'urn:o6:benchmark:server',
  expectedNodes: 8192 + NODE_COUNT + o.arraySizes.length,
  serverCapabilities: serverProfile(o).serverCapabilities,
});
// Namespace 1 is the server's own (from the applicationUri), where the clients look for the nodes.
const ns = engine.registerNamespace('urn:o6:benchmark:server');
if (ns !== 1) throw new Error(`Benchmark namespace must be 1, got ${ns}`);
const space = engine.addressSpace;
const objects = space.findNode('ns=0;i=85');
function variable(id, value, array) {
  space.addVariable({
    organizedBy: objects, nodeId: `ns=1;i=${id}`, browseName: `Benchmark${id}`, dataType: 'Int32',
    valueRank: array ? 1 : -1, arrayDimensions: array ? [value.length] : undefined,
    // numeric: the compact store does not parse the 'CurrentRead | CurrentWrite' form (3 = both)
    accessLevel: 3, userAccessLevel: 3, minimumSamplingInterval: 1,
    // an explicit array Variant: given a bare typed array, the compact store keeps a scalar
    value: new Variant({ dataType: DataType.Int32, arrayType: array ? VariantArrayType.Array : VariantArrayType.Scalar, value }),
  });
}
for (let i = 0; i < NODE_COUNT; i++) variable(FIRST_NODE_ID + i, i, false);
o.arraySizes.forEach((size, i) => variable(ARRAY_FIRST_NODE_ID + i, arrayPayload(size), true));

let stopping = false;
async function stop() {
  if (stopping) return;
  stopping = true;
  const timeout = setTimeout(() => process.exit(1), 8000);
  timeout.unref();
  try { await engine.shutdown(); } finally { clearTimeout(timeout); }
}
process.on('SIGINT', () => stop().catch(error => { console.error(error); process.exitCode = 1; }));
process.on('SIGTERM', () => stop().catch(error => { console.error(error); process.exitCode = 1; }));
try {
  // Each front builds its own OPCUAServerOptions from the parsed options (functions cannot cross threads).
  await engine.start({ fronts, serverModule: new URL('./front_options.mjs', import.meta.url), serverModuleData: o });
  console.error(JSON.stringify({ node_opcua_fronts: { cpus, fronts: engine.frontCount, endpointUrls: engine.endpointUrls } }));
  await output(`${SERVER_READY}\n`);
} catch (error) {
  console.error(error);
  await stop();
  process.exitCode = 1;
}
