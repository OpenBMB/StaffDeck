import assert from 'node:assert/strict';
import test from 'node:test';

import { createRateLimiter, createSessionToken, verifySessionToken } from '../lib/security.mjs';

test('signed website sessions verify and reject tampering', () => {
  const secret = 'a-secure-test-secret-that-is-long-enough';
  const { session, token } = createSessionToken(secret, 60, 1_000);
  assert.equal(verifySessionToken(token, secret, 2_000)?.sid, session.sid);
  assert.equal(verifySessionToken(`${token}x`, secret, 2_000), null);
  assert.equal(verifySessionToken(token, secret, 62_000), null);
});
test('rate limiter resets after its window', () => {
  const consume = createRateLimiter({ limit: 2, windowMs: 1_000 });
  assert.equal(consume('visitor', 0).allowed, true);
  assert.equal(consume('visitor', 10).allowed, true);
  assert.equal(consume('visitor', 20).allowed, false);
  assert.equal(consume('visitor', 1_100).allowed, true);
});
