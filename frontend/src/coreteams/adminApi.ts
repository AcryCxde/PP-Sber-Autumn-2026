import { apiFetch } from '../auth';

export type Period = { start: string; end: string };
export type RunStatus = 'pending' | 'running' | 'completed' | 'failed' | 'outcome_unknown';
export type AdminRun = {
  id: string; conversation_id: string; project_id: string | null; task_label: string;
  user: { id: string; email: string } | null; status: RunStatus;
  stage: { index: number; id: string | null; title: string; source: string } | null;
  started_at: string | null; completed_at: string | null; project_href: string | null; error_code: string | null;
};
export type Page<T> = { as_of: string; items: T[]; total: number; page: number; page_size: number };
export type Overview = {
  as_of: string; period: Period; completeness: 'complete';
  totals: { runs: number; failed: number; outcome_unknown: number; running: number };
  without_started_at: number; active: Omit<Page<AdminRun>, 'as_of'>;
  attention: Omit<Page<AdminRun>, 'as_of'> & { period: Period; no_start: boolean };
};
export type RunsPage = Page<AdminRun> & { period: Period; completeness: 'complete' };
export type AdminUser = { id: string; email: string; created_at: string; last_seen_at: string | null; ai_access: { frozen: boolean; version: number } };
export type Audit = { id: string; actor_id: string; target_id: string; created_at: string; action: 'freeze' | 'unfreeze'; reason: string | null; before_version: number; after_version: number };
export type Estimate = {
  state: 'known' | 'unknown'; value_usd: string | null; scope: 'session_total' | 'run_delta' | 'unknown';
  observed_at: string | null; unavailable_reason: string | null;
  source: { kind: string; field: string; event_id: string | null; journal_seq: number | null; runner_version: string | null };
};
export type RunDetails = { as_of: string; run: AdminRun; harness_estimate: Estimate };
export type Mutation = { frozen: boolean; reason: string | null; expected_version: number };
export type MutationResult = { user: AdminUser; audit_id: string; changed_at: string };
export type CurrentPolicy = { frozen: boolean; version: number };

export class AdminApiError extends Error {
  status: number;
  code: string;
  current?: CurrentPolicy;
  constructor(status: number, code: string, current?: CurrentPolicy) {
    super(status === 409 ? 'Доступ изменён другим администратором. Проверьте состояние и подтвердите заново.' :
      status === 503 ? 'Управление AI-доступом пока недоступно.' :
      status === 422 ? 'Проверьте введённые данные.' :
      status === 403 ? 'Недостаточно прав для этого действия.' : 'Не удалось выполнить запрос администратора.');
    this.name = 'AdminApiError';
    this.status = status;
    this.code = code;
    this.current = current;
  }
}
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Некорректный ответ администратора.');
  return value as Record<string, unknown>;
}
function text(value: unknown): string { if (typeof value !== 'string') throw new Error('Некорректный текст ответа.'); return value; }
function count(value: unknown): number { if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) throw new Error('Некорректный счётчик.'); return value; }
function stamp(value: unknown): string { const result = text(value); if (!Number.isFinite(Date.parse(result))) throw new Error('Некорректное время.'); return result; }
function nullableText(value: unknown): string | null { return value === null ? null : text(value); }
function nullableStamp(value: unknown): string | null { return value === null ? null : stamp(value); }
function period(value: unknown): Period { const v = object(value); const result = { start: stamp(v.start), end: stamp(v.end) }; if (Date.parse(result.start) >= Date.parse(result.end)) throw new Error('Некорректный период.'); return result; }
function boolean(value: unknown): boolean { if (typeof value !== 'boolean') throw new Error('Некорректный флаг.'); return value; }
function run(value: unknown): AdminRun {
  const v = object(value); const status = text(v.status);
  if (!['pending', 'running', 'completed', 'failed', 'outcome_unknown'].includes(status)) throw new Error('Неизвестный статус запуска.');
  const owner = v.user === null ? null : object(v.user); const stage = v.stage === null ? null : object(v.stage);
  return { id: text(v.id), conversation_id: text(v.conversation_id), project_id: nullableText(v.project_id), task_label: text(v.task_label),
    user: owner ? { id: text(owner.id), email: text(owner.email) } : null, status: status as RunStatus,
    stage: stage ? { index: count(stage.index), id: nullableText(stage.id), title: text(stage.title), source: text(stage.source) } : null,
    started_at: nullableStamp(v.started_at), completed_at: nullableStamp(v.completed_at), project_href: nullableText(v.project_href), error_code: nullableText(v.error_code) };
}
function page<T>(value: unknown, item: (v: unknown) => T): Omit<Page<T>, 'as_of'> {
  const v = object(value); if (!Array.isArray(v.items)) throw new Error('Некорректный список.');
  const result = { items: v.items.map(item), total: count(v.total), page: count(v.page), page_size: count(v.page_size) };
  if (!result.page || !result.page_size) throw new Error('Некорректная страница.'); return result;
}
function user(value: unknown): AdminUser {
  const v = object(value); const access = object(v.ai_access);
  return { id: text(v.id), email: text(v.email), created_at: stamp(v.created_at), last_seen_at: nullableStamp(v.last_seen_at), ai_access: { frozen: boolean(access.frozen), version: count(access.version) } };
}
export function parseOverview(value: unknown): Overview {
  const v = object(value); const totals = object(v.totals); const attention = object(v.attention);
  if (v.completeness !== 'complete') throw new Error('Неполные данные обзора.');
  return { as_of: stamp(v.as_of), period: period(v.period), completeness: 'complete', totals: { runs: count(totals.runs), failed: count(totals.failed), outcome_unknown: count(totals.outcome_unknown), running: count(totals.running) },
    without_started_at: count(v.without_started_at), active: page(v.active, run), attention: { ...page(attention, run), period: period(attention.period), no_start: boolean(attention.no_start) } };
}
function details(value: unknown): RunDetails {
  const v = object(value); const e = object(v.harness_estimate); const source = object(e.source);
  if (e.state !== 'known' && e.state !== 'unknown') throw new Error('Некорректная оценка.');
  if (!['unknown', 'session_total', 'run_delta'].includes(text(e.scope))) throw new Error('Неизвестная область оценки.');
  const amount = nullableText(e.value_usd);
  if (e.state === 'known' && (amount === null || !/^\d+(\.\d+)?([eE][+-]?\d+)?$/.test(amount) || e.scope === 'unknown')) throw new Error('Недостоверная оценка.');
  return { as_of: stamp(v.as_of), run: run(v.run), harness_estimate: { state: e.state, value_usd: amount, scope: e.scope as Estimate['scope'], observed_at: nullableStamp(e.observed_at), unavailable_reason: nullableText(e.unavailable_reason), source: { kind: text(source.kind), field: text(source.field), event_id: nullableText(source.event_id), journal_seq: source.journal_seq === null ? null : count(source.journal_seq), runner_version: nullableText(source.runner_version) } } };
}
async function request<T>(path: string, parse: (v: unknown) => T, signal?: AbortSignal, init?: RequestInit): Promise<T> {
  const response = await apiFetch(`/api/admin/v1${path}`, { ...init, signal, cache: 'no-store' });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const v = body && typeof body === 'object' ? body as Record<string, unknown> : {};
    const raw = v.current && typeof v.current === 'object' ? v.current as Record<string, unknown> : null;
    const current = raw && typeof raw.frozen === 'boolean' && Number.isSafeInteger(raw.version) ? { frozen: raw.frozen, version: raw.version as number } : undefined;
    throw new AdminApiError(response.status, typeof v.code === 'string' ? v.code : 'request_failed', current);
  }
  return parse(body);
}
async function policyRequest<T>(load: (signal: AbortSignal) => Promise<T>, signal?: AbortSignal): Promise<T> {
  const attempt = new AbortController();
  const cancel = () => attempt.abort(signal?.reason);
  signal?.addEventListener('abort', cancel, { once: true });
  if (signal?.aborted) cancel();
  const deadline = setTimeout(() => attempt.abort(new Error('AI-access mutation deadline exceeded')), 30000);
  let rejectAborted!: (reason: unknown) => void;
  const aborted = new Promise<never>((_, reject) => { rejectAborted = reject; });
  const onAbort = () => rejectAborted(attempt.signal.reason);
  attempt.signal.addEventListener('abort', onAbort, { once: true });
  try {
    attempt.signal.throwIfAborted();
    // Bound fetch and body parsing even when either ignores cancellation; the outcome remains uncertain.
    return await Promise.race([load(attempt.signal), aborted]);
  } finally {
    clearTimeout(deadline);
    signal?.removeEventListener('abort', cancel);
    attempt.signal.removeEventListener('abort', onAbort);
  }
}
function query(values: object): string { const params = new URLSearchParams(); for (const [key, value] of Object.entries(values)) if (value !== undefined) params.set(key, String(value)); return `?${params}`; }
export const adminApi = {
  overview: (values: Period & { attention_start: string; attention_end: string; active_page: number; attention_page: number; attention_no_start: boolean }, signal?: AbortSignal) => request(`/overview${query(values)}`, parseOverview, signal),
  runs: (values: Period & { status: string; page: number }, signal?: AbortSignal) => request(`/runs${query(values)}`, (value): RunsPage => { const v = object(value); if (v.completeness !== 'complete') throw new Error('Неполная выборка.'); return { ...page(v, run), as_of: stamp(v.as_of), period: period(v.period), completeness: 'complete' }; }, signal),
  run: (id: string, signal?: AbortSignal) => request(`/runs/${encodeURIComponent(id)}`, details, signal),
  users: (values: { q?: string; page: number }, signal?: AbortSignal) => request(`/users${query(values)}`, (v): Page<AdminUser> => ({ ...page(v, user), as_of: stamp(object(v).as_of) }), signal),
  audit: (id: string, pageNumber: number, signal?: AbortSignal) => request(`/users/${encodeURIComponent(id)}/audit${query({ page: pageNumber })}`, (value): Page<Audit> => ({ ...page(value, (entry) => { const v = object(entry); if (v.action !== 'freeze' && v.action !== 'unfreeze') throw new Error('Неизвестное действие.'); return { id: text(v.id), actor_id: text(v.actor_id), target_id: text(v.target_id), created_at: stamp(v.created_at), action: v.action, reason: nullableText(v.reason), before_version: count(v.before_version), after_version: count(v.after_version) }; }), as_of: stamp(object(value).as_of) }), signal),
  changeAiAccess: (id: string, mutation: Mutation, key: string, signal?: AbortSignal) => policyRequest((attemptSignal) => request(`/users/${encodeURIComponent(id)}/ai-access`, (value): MutationResult => { const v = object(value); return { user: user(v.user), audit_id: text(v.audit_id), changed_at: stamp(v.changed_at) }; }, attemptSignal, { method: 'PATCH', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key, 'X-CoreTeams-Admin': '1' }, body: JSON.stringify(mutation) }), signal),
};
