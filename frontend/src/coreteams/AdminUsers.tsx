import { useCallback, useEffect, useReducer, useRef, useState } from 'react';
import { AdminApiError, adminApi, type AdminUser } from './adminApi';
import { useAdminFailure, useAdminPolling } from './adminHooks';
import { LoadState, Pagination, RefreshStatus } from './AdminShared';
import { formatTime } from './adminView';
import { beginPolicy, canConfirm, initialUsersState, isUncertainPolicyError, policyReducer, usersReducer, type PolicyDraft } from './adminUsersState';

function initialSearch() { if (typeof window === 'undefined') return ''; return new URLSearchParams(window.location.hash.split('?')[1] ?? '').get('email') ?? ''; }
function AuditPanel({ user, onClose }: { user: AdminUser; onClose: () => void }) {
  const [page, setPage] = useState(1); const load = useCallback((signal: AbortSignal) => adminApi.audit(user.id, page, signal), [user.id, page]); const state = useAdminPolling(load);
  return <section className="admin-section" aria-label="История AI-доступа"><h2>История AI-доступа: {user.email}</h2><p>Внутренние записи, доступны только администраторам.</p><button className="text-button" type="button" onClick={onClose}>Закрыть историю</button><RefreshStatus state={state} refresh={state.refresh} /><LoadState state={state}>{(data) => <>{data.items.length ? <ul className="admin-audit">{data.items.map((entry) => <li key={entry.id}><strong>{entry.action === 'freeze' ? 'Заморозка' : 'Снятие заморозки'}</strong> · {formatTime(entry.created_at)}<p>Администратор: {entry.actor_id}</p><p>Внутренняя заметка: {entry.reason ?? 'Не указана'}</p></li>)}</ul> : <p>Изменений AI-доступа не найдено.</p>}<Pagination page={data.page} total={data.total} size={data.page_size} onChange={setPage} /></>}</LoadState></section>;
}
function PolicyDialog({ draft, dispatch, onSubmit, onClose }: { draft: PolicyDraft; dispatch: (action: Parameters<typeof policyReducer>[1]) => void; onSubmit: () => void; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current; const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    element?.showModal();
    return () => { element?.close(); opener?.focus(); };
  }, []);
  return <dialog ref={dialog} className="admin-policy-dialog" aria-labelledby="admin-policy-title" onCancel={(event) => { event.preventDefault(); onClose(); }}><form onSubmit={(event) => { event.preventDefault(); if (canConfirm(draft)) onSubmit(); }}>
    <h2 id="admin-policy-title">{draft.frozen ? 'Заморозить AI-доступ' : 'Снять заморозку'}</h2><p>{draft.user.email}</p><p>Сейчас: {draft.user.ai_access.frozen ? 'Заморожен' : 'Разрешён'} · версия {draft.user.ai_access.version}</p>
    {draft.frozen ? <p>Новые AI-запуски будут запрещены бессрочно, до ручного снятия администратором. Уже допущенная работа и её внутренние вызовы завершатся штатно. Вход и просмотр проектов сохранятся.</p> : <p>Новые AI-запуски снова будут разрешены. Аккаунт и роль пользователя не меняются.</p>}
    <label htmlFor="admin-policy-reason">{draft.frozen ? 'Внутренняя причина (обязательно)' : 'Внутренняя заметка (необязательно)'}</label><textarea id="admin-policy-reason" autoFocus value={draft.reason} maxLength={1000} disabled={draft.status === 'submitting' || draft.attempted} onChange={(event) => dispatch({ type: 'reason', value: event.target.value })} /><p className="helper">Не видна пользователю. Не указывайте секреты и лишние персональные сведения. Пользователь увидит нейтральное сообщение о приостановке AI-доступа.</p>
    {draft.error && <p role="alert">{draft.error}</p>}{draft.attempted && draft.status !== 'submitting' && <p>Ответ на предыдущую попытку не подтверждён. Причина и ключ операции сохранены: повторите подтверждение, чтобы получить её результат, прежде чем менять данные.</p>}{draft.status === 'conflict' && <button className="btn secondary" type="button" onClick={() => dispatch({ type: 'reconfirm', key: crypto.randomUUID() })}>Состояние проверено, подготовить новое подтверждение</button>}
    <div className="admin-actions"><button className="btn secondary" type="button" disabled={draft.status === 'submitting' || draft.attempted} onClick={onClose}>Отмена</button><button className="btn" type="submit" disabled={!canConfirm(draft)}>{draft.status === 'submitting' ? 'Сохранение…' : draft.frozen ? 'Подтвердить заморозку' : 'Подтвердить снятие'}</button></div>
  </form></dialog>;
}
export function AdminUsers({ currentUserId }: { currentUserId: string }) {
  const [search, setSearch] = useState(initialSearch); const [filter, updateFilter] = useReducer(usersReducer, { ...initialUsersState, q: initialSearch() });
  const [draft, setDraft] = useState<PolicyDraft | null>(null);
  const [audit, setAudit] = useState<AdminUser | null>(null); const [notice, setNotice] = useState('');
  const opener = useRef<HTMLElement | null>(null);
  const failAuth = useAdminFailure(); const busy = useRef(false); const active = useRef(true); const controller = useRef<AbortController | null>(null);
  const actualDraft = draft;
  const load = useCallback((signal: AbortSignal) => adminApi.users(filter, signal), [filter]); const state = useAdminPolling(load);
  useEffect(() => { const timer = setTimeout(() => updateFilter({ type: 'query', q: search.trim() }), 300); return () => clearTimeout(timer); }, [search]);
  useEffect(() => { active.current = true; return () => { active.current = false; controller.current?.abort(); }; }, []);
  function close() { if (actualDraft?.attempted) return; controller.current?.abort(); setDraft(null); requestAnimationFrame(() => opener.current?.focus()); }
  function resolved() { controller.current?.abort(); setDraft(null); }
  function change(action: Parameters<typeof policyReducer>[1]) { setDraft((previous) => policyReducer(previous, action)); }
  function open(user: AdminUser) { if (actualDraft?.attempted) return; opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null; setDraft(beginPolicy(user, crypto.randomUUID())); setNotice(''); }
  async function submit() {
    if (!actualDraft || busy.current || !canConfirm(actualDraft)) return;
    const submitted = actualDraft; busy.current = true; controller.current = new AbortController();
    setDraft(policyReducer(submitted, { type: 'submit' }));
    try {
      await adminApi.changeAiAccess(submitted.user.id, { frozen: submitted.frozen, reason: submitted.reason.trim() || null, expected_version: submitted.user.ai_access.version }, submitted.key, controller.current.signal);
      if (!active.current || controller.current.signal.aborted) return;
      resolved(); setNotice('Изменение AI-доступа сохранено.'); state.refresh();
    } catch (error) {
      if (!active.current || controller.current.signal.aborted) return;
      if (failAuth(error)) return;
      if (error instanceof AdminApiError && error.status === 409 && error.current) { setDraft(policyReducer(submitted, { type: 'conflict', current: error.current })); state.refresh(); }
      else setDraft(policyReducer(submitted, { type: 'error', uncertain: isUncertainPolicyError(error), message: error instanceof AdminApiError ? error.message : 'Не удалось сохранить. Повторите подтверждение с теми же данными.' }));
    } finally { busy.current = false; }
  }
  return <div className="admin-page"><h1>Пользователи</h1><p className="helper">Управление только AI-доступом: заморозка не блокирует вход и не удаляет проекты.</p><label className="admin-search">Поиск по email<input type="search" value={search} maxLength={254} onChange={(event) => setSearch(event.target.value)} /></label><RefreshStatus state={state} refresh={state.refresh} />{notice && <p role="status">{notice}</p>}<LoadState state={state}>{(data) => <>{data.items.length ? <div className="admin-table-wrap"><table className="admin-table"><thead><tr><th>Email</th><th>Регистрация</th><th>Последний визит</th><th>AI-доступ</th><th>Действия</th></tr></thead><tbody>{data.items.map((user) => <tr key={user.id}><td data-label="Email">{user.email}</td><td data-label="Регистрация">{formatTime(user.created_at)}</td><td data-label="Последний визит">{formatTime(user.last_seen_at)}</td><td data-label="AI-доступ">{user.ai_access.frozen ? 'Заморожен' : 'Разрешён'}</td><td data-label="Действия"><button className="text-button" type="button" disabled={user.id === currentUserId && !user.ai_access.frozen} title={user.id === currentUserId && !user.ai_access.frozen ? 'Самозаморозка недоступна' : undefined} onClick={() => open(user)}>{user.ai_access.frozen ? 'Снять заморозку' : 'Заморозить AI-доступ'}</button><button className="text-button" type="button" onClick={() => setAudit(user)}>История</button></td></tr>)}</tbody></table></div> : <p>Пользователи не найдены.</p>}<Pagination page={data.page} total={data.total} size={data.page_size} onChange={(page) => updateFilter({ type: 'page', page })} /></>}</LoadState>{audit && <AuditPanel key={audit.id} user={audit} onClose={() => setAudit(null)} />}{actualDraft && <PolicyDialog draft={actualDraft} dispatch={change} onClose={close} onSubmit={() => { void submit(); }} />}</div>;
}
