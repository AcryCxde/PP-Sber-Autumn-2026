import { describe, expect, it } from 'vitest';
import { beginPolicy, policyReducer, canConfirm, initialUsersState, isUncertainPolicyError, usersReducer } from './adminUsersState';
import { AdminApiError, type AdminUser } from './adminApi';
const user: AdminUser = { id: 'u', email: 'u@example.ru', created_at: '2026-11-12T00:00:00Z', last_seen_at: null, ai_access: { frozen: false, version: 0 } };
describe('policy form state', () => {
  it('requires a normalized reason and keeps an idempotency key for network retry', () => {
    let state = beginPolicy(user, 'key');
    expect(canConfirm(state)).toBe(false);
    state = policyReducer(state, { type: 'reason', value: ' \t\n' }); expect(canConfirm(state)).toBe(false);
    state = policyReducer(state, { type: 'reason', value: 'Причина' }); expect(canConfirm(state)).toBe(true);
    state = policyReducer(state, { type: 'submit' }); expect(canConfirm(state)).toBe(false);
    state = policyReducer(state, { type: 'error', message: 'Сеть' });
    expect(state?.key).toBe('key'); expect(state?.reason).toBe('Причина'); expect(canConfirm(state)).toBe(true);
  });
  it('conflict preserves draft, updates version and requires new explicit confirmation/key', () => {
    let state = policyReducer(beginPolicy(user, 'old'), { type: 'reason', value: 'Причина' });
    state = policyReducer(state, { type: 'conflict', current: { frozen: false, version: 2 } });
    expect(canConfirm(state)).toBe(false); expect(state?.reason).toBe('Причина');
    state = policyReducer(state, { type: 'reconfirm', key: 'new' });
    expect(state?.key).toBe('new'); expect(state?.user.ai_access.version).toBe(2); expect(canConfirm(state)).toBe(true);
  });
  it.each([false, true])('resolves a conflict when another admin already applied desired frozen=%s', (frozen) => {
    let state = beginPolicy({ ...user, ai_access: { frozen: !frozen, version: 0 } }, 'original');
    state = policyReducer(state, { type: 'conflict', current: { frozen, version: 1 } })!;
    expect(state.status).toBe('satisfied');
    expect(state.attempted).toBe(false);
    expect(canConfirm(state)).toBe(false);
    expect(policyReducer(state, { type: 'reconfirm', key: 'unnecessary' })).toBe(state);
  });
  it('a policy_state_conflict is a definitive no-op, not an uncertain commit', () => {
    expect(isUncertainPolicyError(new AdminApiError(409, 'policy_state_conflict'))).toBe(false);
  });
  it('a generic server error remains uncertain, verified rejection may unlock editing', () => {
    expect(isUncertainPolicyError(new AdminApiError(503, 'ai_policy_unavailable'))).toBe(true);
    expect(isUncertainPolicyError(new AdminApiError(503, 'unknown'))).toBe(true);
    expect(isUncertainPolicyError(new AdminApiError(503, 'policy_management_unavailable'))).toBe(false);
    expect(isUncertainPolicyError(new AdminApiError(422, 'invalid_policy_request'))).toBe(false);
    let state = policyReducer(beginPolicy(user, 'key'), { type: 'reason', value: 'Original' });
    state = policyReducer(state, { type: 'submit' });
    state = policyReducer(state, { type: 'error', message: '503' });
    expect(state?.attempted).toBe(true);
    state = policyReducer(state, { type: 'error', message: 'Rejected', uncertain: false });
    expect(state?.attempted).toBe(false);
  });
  it('auth loss and close clear private state, unfreeze note is optional', () => {
    expect(canConfirm(beginPolicy({ ...user, ai_access: { frozen: true, version: 1 } }, 'key'))).toBe(true);
    expect(policyReducer(beginPolicy(user, 'key'), { type: 'clear' })).toBeNull();
  });
  it('locks an uncertain submitted payload and key until retry resolves', () => {
    let state = policyReducer(beginPolicy(user, 'key'), { type: 'reason', value: 'x'.repeat(1001) });
    expect(canConfirm(state)).toBe(false);
    state = policyReducer(state, { type: 'reason', value: 'Valid' });
    state = policyReducer(state, { type: 'submit' });
    state = policyReducer(state, { type: 'error', message: 'Сеть' });
    state = policyReducer(state, { type: 'reason', value: 'Changed', key: 'changed' });
    expect(state?.key).toBe('key');
    expect(state?.reason).toBe('Valid');
    expect(state?.attempted).toBe(true);
  });
});
describe('user search', () => {
  it('commits debounced search at page one and preserves query on pagination', () => {
    const state = usersReducer({ ...initialUsersState, page: 3 }, { type: 'query', q: 'new' });
    expect(state).toMatchObject({ q: 'new', page: 1 });
    expect(usersReducer(state, { type: 'page', page: 2 })).toMatchObject({ q: 'new', page: 2 });
  });
});
