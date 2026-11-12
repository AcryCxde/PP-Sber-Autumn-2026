import { describe, expect, it } from 'vitest';
import { createAdminPolicyStore } from './adminPolicyStore';
import { beginPolicy, policyReducer } from './adminUsersState';

const user = { id: 'target', email: 'target@example.test', created_at: '2026-11-12T00:00:00Z', last_seen_at: null, ai_access: { frozen: false, version: 2 } };
function attempted() {
  const draft = policyReducer(beginPolicy(user, 'original-key'), { type: 'reason', value: 'private reason' });
  return policyReducer(draft, { type: 'submit' });
}
describe('auth-scoped policy operation across navigation', () => {
  it('restores the identical uncertain operation after leaving Users', () => {
    const store = createAdminPolicyStore();
    store.set(attempted());
    store.leave();
    const restored = store.get();
    expect(restored).toMatchObject({ key: 'original-key', reason: 'private reason', attempted: true, status: 'editing', user });
    expect(policyReducer(restored, { type: 'reason', value: 'changed' })).toBe(restored);
    expect(policyReducer(restored, { type: 'submit' })?.key).toBe('original-key');
  });
  it('preserves a timed-out payload and key across navigation', () => {
    const store = createAdminPolicyStore();
    store.set(policyReducer(attempted(), { type: 'error', message: 'timeout' }));
    const before = store.get();
    store.leave();
    expect(store.get()).toBe(before);
  });
  it('clears completed and unattempted drafts, but never shares another auth scope', () => {
    const first = createAdminPolicyStore(); const second = createAdminPolicyStore();
    first.set(attempted());
    expect(second.get()).toBeNull();
    first.clear();
    expect(first.get()).toBeNull();
    first.set(beginPolicy(user, 'not-submitted'));
    first.leave();
    expect(first.get()).toBeNull();
  });
  it('notifies mounted consumers on resolution and revocation', () => {
    const store = createAdminPolicyStore(); let notifications = 0;
    const stop = store.subscribe(() => { notifications += 1; });
    store.set(attempted()); store.clear();
    expect(notifications).toBe(2);
    stop(); store.set(attempted());
    expect(notifications).toBe(2);
  });
});
