import { describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { AdminApiError } from './adminApi';
import { AdminAccessContext, useAdminFailure } from './adminHooks';
import { UnauthorizedError } from '../auth';
import { beginPolicy, isUncertainPolicyError, policyReducer } from './adminUsersState';
import { createAdminPolling } from './adminPolling';

function failureHandler(revoke: (status: 'unauthorized' | 'forbidden') => void) {
  let handle!: ReturnType<typeof useAdminFailure>;
  function Probe() { handle = useAdminFailure(); return null; }
  renderToStaticMarkup(<AdminAccessContext.Provider value={revoke}><Probe /></AdminAccessContext.Provider>);
  return handle;
}

describe('operation rejection versus revoked admin access', () => {
  it.each(['invalid_admin_origin', 'self_freeze_forbidden'])('keeps admin data for the rejected operation %s', (code) => {
    const revoke = vi.fn(); const error = new AdminApiError(403, code);
    expect(failureHandler(revoke)(error)).toBe(false);
    expect(revoke).not.toHaveBeenCalled();
    const user = { id: 'u', email: 'u@example.test', created_at: '2026-11-12T00:00:00Z', last_seen_at: null, ai_access: { frozen: false, version: 0 } };
    const submitted = policyReducer(beginPolicy(user, 'key'), { type: 'submit' });
    const rejected = policyReducer(submitted, { type: 'error', uncertain: isUncertainPolicyError(error), message: error.message });
    expect(rejected?.attempted).toBe(false);
    expect(rejected?.error).toBe(error.message);
  });
  it.each(['admin_required', 'request_failed', 'unknown_permission_failure'])('revokes on a permission denial %s', (code) => {
    const revoke = vi.fn();
    expect(failureHandler(revoke)(new AdminApiError(403, code))).toBe(true);
    expect(revoke).toHaveBeenCalledExactlyOnceWith('forbidden');
  });
  it('still revokes direct read polling on any forbidden response', async () => {
    const revoked = vi.fn();
    const polling = createAdminPolling<{ as_of: string }>(() => Promise.reject(new AdminApiError(403, 'invalid_admin_origin')), revoked);
    polling.start();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(polling.state().status).toBe('forbidden');
    expect(revoked).toHaveBeenCalledOnce();
    polling.dispose();
  });
  it('still revokes on an active unauthorized error', () => {
    const revoke = vi.fn();
    expect(failureHandler(revoke)(new UnauthorizedError())).toBe(true);
    expect(revoke).toHaveBeenCalledExactlyOnceWith('unauthorized');
  });
});
