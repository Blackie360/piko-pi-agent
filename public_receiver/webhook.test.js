import test from 'node:test';
import assert from 'node:assert/strict';
import { POST, GET } from './api/webhook.js';

test('provider requests fail closed without reading any header, credential or payload', async () => {
  const unreadable = new Proxy({}, { get() { throw Error('Request must not be read'); } });
  for (let attempt = 0; attempt < 2; attempt++) {
    const response = POST(unreadable);
    assert.equal(response.status, 401);
    assert.equal(response.headers.get('cache-control'), 'no-store');
    assert.deepEqual(await response.json(), {
      accepted: false,
      error: 'provider_verification_not_configured',
      message: 'Piko is not accepting provider messages yet.'
    });
  }
});

test('readiness cannot claim that activation is complete', async () => {
  const response = GET();
  assert.equal(response.status, 503);
  const body = await response.json();
  assert.equal(body.active, false);
  assert.equal(body.status, 'blocked_pending_provider_verification');
});

