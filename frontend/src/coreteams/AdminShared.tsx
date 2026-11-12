import type { ReactNode } from 'react';
import type { PollState } from './adminPolling';
import { formatTime, parseExplicitPeriod } from './adminView';
import type { Period } from './adminApi';
import { useState } from 'react';

export function AdminAvailability({ enabled, children }: { enabled: boolean; children: ReactNode }) {
  return enabled ? children : <div className="page"><h1>Администрирование временно недоступно</h1><p>Управление отключено. Действующие ограничения AI-доступа сохраняются.</p></div>;
}

export function RefreshStatus<T>({ state, refresh }: { state: PollState<T>; refresh: () => void }) {
  return <div className="admin-refresh"><span role="status">{state.asOf ? `Обновлено: ${formatTime(state.asOf)}` : 'Данные ещё не получены'}{state.stale ? ' · Данные устарели' : ''}{state.refreshError ? ' · Не удалось обновить' : ''}</span><button type="button" className="btn secondary" onClick={refresh} disabled={state.refreshing}>{state.refreshing ? 'Обновление…' : 'Обновить'}</button></div>;
}
export function LoadState<T>({ state, children }: { state: PollState<T>; children: (data: T) => ReactNode }) {
  if (state.data) return children(state.data);
  if (state.status === 'unauthorized') return <p role="alert">Требуется вход.</p>;
  if (state.status === 'forbidden') return <p role="alert">Недостаточно прав.</p>;
  if (state.status === 'error') return <p role="alert">Не удалось загрузить данные. Используйте «Обновить».</p>;
  return <p role="status" className="helper">Загрузка…</p>;
}
export function Pagination({ page, total, size, onChange }: { page: number; total: number; size: number; onChange: (page: number) => void }) {
  return <div className="admin-pagination"><button className="btn secondary" type="button" disabled={page <= 1} onClick={() => onChange(page - 1)}>Назад</button><span>Страница {page} · всего {total}</span><button className="btn secondary" type="button" disabled={page * size >= total} onClick={() => onChange(page + 1)}>Далее</button></div>;
}
export function PeriodControl({ label, value, onChange }: { label: string; value: Period; onChange: (value: Period) => void }) {
  const [days, setDays] = useState('7'); const [custom, setCustom] = useState(false);
  const [start, setStart] = useState(value.start); const [end, setEnd] = useState(value.end); const [error, setError] = useState('');
  function apply() {
    try { const next = custom ? parseExplicitPeriod(start, end) : { start: new Date(Date.now() - Number(days) * 86400000).toISOString(), end: new Date().toISOString() }; onChange(next); setError(''); }
    catch (failure) { setError(failure instanceof Error ? failure.message : 'Некорректный период.'); }
  }
  return <fieldset className="admin-period"><legend>{label}</legend><label>Период<select value={custom ? 'custom' : days} onChange={(event) => { setCustom(event.target.value === 'custom'); if (event.target.value !== 'custom') setDays(event.target.value); }}><option value="1">24 часа</option><option value="7">7 суток</option><option value="30">30 суток</option><option value="custom">Свой интервал</option></select></label>{custom && <><label>Начало с часовым смещением<input value={start} onChange={(event) => setStart(event.target.value)} placeholder="2026-11-12T12:00:00+03:00" /></label><label>Конец с часовым смещением<input value={end} onChange={(event) => setEnd(event.target.value)} /></label></>}<button className="btn secondary" type="button" onClick={apply}>Обновить период</button><small>UTC: [{value.start}, {value.end}) · конец исключён. Время записей — {Intl.DateTimeFormat().resolvedOptions().timeZone}.</small>{error && <p role="alert">{error}</p>}</fieldset>;
}
