// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright 2026 o6 Automation GmbH

// Live fault fixture: one invalid-session response, then normal service handling.
import { writeFileSync } from 'node:fs';
import { OPCUAServer, ReadResponse, StatusCodes } from '../sdk.mjs';
// Use decoded requests so the fault hook also sees optimized single-value Reads.
OPCUAServer.prototype.leanRequestHandler = () => () => false;
const original = OPCUAServer.prototype._on_ReadRequest;
let requests = 0;
OPCUAServer.prototype._on_ReadRequest = function(message, channel) {
  if (message.request.nodesToRead.some(item => item.nodeId.namespace === 1 && item.nodeId.value >= 1001 && item.nodeId.value <= 1100)) {
    requests++;
    if (requests === 1) {
      channel.send_response('MSG', new ReadResponse({ responseHeader: { serviceResult: StatusCodes.BadSessionIdInvalid } }), message);
      return;
    }
  }
  return original.call(this, message, channel);
};
process.on('exit', () => writeFileSync(process.env.BENCH_FAULT_REPORT, JSON.stringify({ requests })));
