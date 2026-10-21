// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright 2026 o6 Automation GmbH

import test from 'node:test';
import assert from 'node:assert/strict';
import { Workload, pipeline } from '../../../throughput/node/workload.mjs';
import { ReadRequest, StatusCodes, TimestampsToReturn, DataType, VariantArrayType } from '../sdk.mjs';
import { options } from '../options.mjs';

test('window refills before a slow request finishes and drains failures', async () => {
  let active = 0;
  let peak = 0;
  const completed = [];
  await pipeline(8, 3, async index => {
    peak = Math.max(peak, ++active);
    await new Promise(resolve => setTimeout(resolve, index === 0 ? 60 : 1));
    completed.push(index);
    active--;
  });
  assert.equal(peak, 3);
  assert.ok(completed.indexOf(3) < completed.indexOf(0));
  let issued = 0;
  await assert.rejects(pipeline(100, 4, async index => {
    issued++;
    if (index === 0) throw new Error('service failed');
    await new Promise(resolve => setTimeout(resolve, 10));
  }), /service failed/);
  assert.equal(issued, 4);
});

test('one explicit transaction per batch with independent request objects', async () => {
  const requests = [];
  const session = { performMessageTransaction(request, callback) {
    requests.push(request);
    setImmediate(() => callback(null, { responseHeader: { serviceResult: StatusCodes.Good },
      results: request.nodesToRead.map(() => ({ statusCode: StatusCodes.Good,
        value: { dataType: DataType.Int32, arrayType: VariantArrayType.Scalar, value: 17 } })),
    }));
  } };
  const workload = new Workload(session, options(['--batch-size', '105', '--worker', 'async', '--max-outstanding', '4']));
  await workload.run(7);
  assert.equal(requests.length, 7);
  for (const request of requests) {
    assert.ok(request instanceof ReadRequest);
    assert.equal(request.nodesToRead.length, 105);
    assert.equal(request.timestampsToReturn, TimestampsToReturn.Source);
    assert.equal(request.requestHeader.timeoutHint, 60000);
  }
  assert.notEqual(requests[0].requestHeader, requests[1].requestHeader);
  assert.notEqual(requests[0].nodesToRead, requests[1].nodesToRead);
  assert.equal(workload.checksum, 7n * 122n);
});

test('checksum preserves all 64 bits and wraps like native uint64', async () => {
  const session = { performMessageTransaction(request, callback) {
    callback(null, { responseHeader: { serviceResult: StatusCodes.Good }, results: [StatusCodes.Good] });
  } };
  const workload = new Workload(session, options(['--operation', 'write']));
  workload.checksum = 2n ** 64n - 1n;
  await workload.run(1);
  assert.equal(workload.checksum, 1048n);
  const asyncRead = new Workload(session, options(['--worker', 'async'])).request().request;
  assert.equal(asyncRead.timestampsToReturn, TimestampsToReturn.Neither);
});
