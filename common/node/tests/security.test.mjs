// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright 2026 o6 Automation GmbH

import test from 'node:test';
import assert from 'node:assert/strict';
import { createPrivateKey, generateKeyPairSync } from 'node:crypto';
import { privateKeyPem } from '../security.mjs';

test('shared keys preserve identity across PKCS#1 and PKCS#8 DER conversion', () => {
  const { privateKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
  const expected = privateKey.export({ format: 'jwk' });
  for (const type of ['pkcs1', 'pkcs8']) {
    const der = privateKey.export({ format: 'der', type });
    assert.deepEqual(createPrivateKey(privateKeyPem(der)).export({ format: 'jwk' }), expected);
  }
  assert.throws(() => privateKeyPem(Buffer.from('not a key')), /neither PKCS/);
});
