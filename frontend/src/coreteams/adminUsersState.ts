import { AdminApiError, isRejectedAdminOperation, type AdminUser, type CurrentPolicy } from './adminApi';
export function isUncertainPolicyError(error: unknown): boolean {
  return !isRejectedAdminOperation(error) && !(error instanceof AdminApiError && ((error.status === 422 && ['invalid_policy_request', 'invalid_reason', 'invalid_idempotency_key'].includes(error.code)) || (error.status === 503 && error.code === 'policy_management_unavailable') || (error.status === 409 && error.code === 'policy_state_conflict')));
}
export type PolicyDraft = { user: AdminUser; frozen: boolean; reason: string; key: string; status: 'editing' | 'submitting' | 'conflict' | 'satisfied'; error: string | null; attempted: boolean };
export function beginPolicy(user: AdminUser, key: string): PolicyDraft { return { user, frozen: !user.ai_access.frozen, reason: '', key, status: 'editing', error: null, attempted: false }; }
export type PolicyAction = { type: 'reason'; value: string; key?: string } | { type: 'submit' } | { type: 'error'; message: string; uncertain?: boolean } | { type: 'conflict'; current: CurrentPolicy } | { type: 'reconfirm'; key: string } | { type: 'clear' };
export function policyReducer(state: PolicyDraft | null, action: PolicyAction): PolicyDraft | null {
  if (action.type === 'clear') return null;
  if (!state) return null;
  switch (action.type) {
    case 'reason': return state.attempted ? state : { ...state, reason: action.value, error: null };
    case 'submit': return { ...state, status: 'submitting', error: null, attempted: true };
    case 'error': return { ...state, status: 'editing', attempted: action.uncertain !== false, error: action.message };
    case 'conflict': return { ...state, user: { ...state.user, ai_access: action.current }, status: action.current.frozen === state.frozen ? 'satisfied' : 'conflict', attempted: false, error: action.current.frozen === state.frozen ? 'Выбранное состояние уже установлено. Новая операция не требуется.' : 'Состояние изменено другим администратором. Проверьте его и подтвердите действие заново.' };
    case 'reconfirm': return state.status !== 'conflict' ? state : { ...state, status: 'editing', key: action.key, attempted: false, error: null };
  }
}
export function canConfirm(state: PolicyDraft | null): boolean { return !!state && state.status === 'editing' && state.reason.trim().length <= 1000 && (!state.frozen || state.reason.trim().length > 0); }
export const initialUsersState = { q: '', page: 1 };
export function usersReducer(state: typeof initialUsersState, action: { type: 'query'; q: string } | { type: 'page'; page: number }) { return action.type === 'query' ? { q: action.q, page: 1 } : { ...state, page: action.page }; }
