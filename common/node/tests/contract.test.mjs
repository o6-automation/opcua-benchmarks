// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright 2026 o6 Automation GmbH

import test from 'node:test';
import assert from 'node:assert/strict';
import { PassThrough } from 'node:stream';
import { NodeCursor, arrayNodeId, arrayPayload, releaseBarrier } from '../contract.mjs';
import { options } from '../options.mjs';

test('LCG wraps unsigned 32-bit state and array order fixes NodeIds', () => {
  const cursor = new NodeCursor(1);
  assert.deepEqual(Array.from({ length: 5 }, () => cursor.next()), [48, 67, 38, 65, 32]);
  assert.equal(arrayNodeId([1000, 10], 10), 2002);
  assert.throws(() => arrayNodeId([10], 1000));
  assert.deepEqual([...arrayPayload(1002).slice(998)], [998, 999, 0, 1]);
});

test('release remembers early newline and rejects EOF', async () => {
  const stream = new PassThrough();
  const release = releaseBarrier(stream);
  stream.write('\n');
  await release;
  const eof = new PassThrough();
  const rejected = releaseBarrier(eof);
  eof.end();
  await assert.rejects(rejected, /EOF/);
});

test('options reject ambiguous or unrepresentable workloads', () => {
  for (const args of [['--samples', '2'], ['--iterations', '9007199254740993'], ['--array-size', '10', '--batch-size', '2'], ['--security', 'Sign']]) {
    assert.throws(() => options(args));
  }
});
