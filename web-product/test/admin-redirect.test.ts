import assert from 'node:assert/strict';
import test from 'node:test';
import { adminRedirect } from '../src/adminRedirect.ts';

test('legacy admin redirects only to the fixed main route', () => {
  assert.equal(adminRedirect('#admin'), '/#admin/overview');
  for (const hash of ['', '#home', '#admin/users', '#admin?return=https://example.test']) {
    assert.equal(adminRedirect(hash), null);
  }
});
