import type { ReactNode } from 'react';
import type { PollState } from './adminPolling';
import { fixedPeriod, formatClock, formatTime } from './adminView';
import type { Period } from './adminApi';
import { useState } from 'react';

export function AdminAvailability({ enabled, children }: { enabled: boolean; children: ReactNode }) {
  return enabled ? children : <div className="page"><h1>Администрирование временно недоступно</h1><p>Управление отключено. Действующие ограничения AI-доступа сохраняются.</p></div>;
}

export function RefreshStatus<T>({ state, refresh, compact = false }: { state: PollState<T>; refresh: () => void; compact?: boolean }) {
  return <div className="admin-refresh"><span role={compact && (state.refreshError || state.status === 'unauthorized' || state.status === 'forbidden') ? 'alert' : 'status'} style={compact ? { whiteSpace: 'nowrap', minInlineSize: '19ch' } : undefined} title={compact && state.refreshError ? 'Не удалось применить фильтры. Предыдущие данные неактуальны и недоступны для действий. Используйте «Обновить».' : state.asOf ? formatTime(state.asOf) : undefined}>{compact && state.status === 'unauthorized' ? 'Требуется вход' : compact && state.status === 'forbidden' ? 'Недостаточно прав' : compact && state.refreshError ? 'Ошибка обновления' : compact && state.status === 'loading' ? 'Обновление…' : <>{state.asOf ? `Обновлено: ${formatClock(state.asOf)}` : 'Данные ещё не получены'}{state.stale ? compact ? ' · Устарело' : ' · Данные устарели' : ''}{state.refreshError ? ' · Не удалось обновить' : ''}</>}</span><button type="button" className="btn secondary" onClick={refresh} disabled={state.refreshing}>{compact ? 'Обновить' : state.refreshing ? 'Обновление…' : 'Обновить'}</button></div>;
}
export function LoadState<T>({ state, children }: { state: PollState<T>; children: (data: T) => ReactNode }) {
  if (state.data) return children(state.data);
  if (state.status === 'unauthorized') return <p role="alert">Требуется вход.</p>;
  if (state.status === 'forbidden') return <p role="alert">Недостаточно прав.</p>;
  if (state.status === 'error') return <p role="alert">Не удалось загрузить данные. Используйте «Обновить».</p>;
  return <p role="status" className="helper">Загрузка…</p>;
}
export function Pagination({ page, total, size, onChange }: { page: number; total: number; size: number; onChange: (page: number) => void }) {
  if (page === 1 && total <= size) return <div className="admin-pagination"><span>Всего: {total}</span></div>;
  return <div className="admin-pagination"><button className="btn secondary" type="button" disabled={page <= 1} onClick={() => onChange(page - 1)}>Назад</button><span>Страница {page} · всего {total}</span><button className="btn secondary" type="button" disabled={page * size >= total} onClick={() => onChange(page + 1)}>Далее</button></div>;
}
export function PeriodControl({ label, onChange }: { label: string; value: Period; onChange: (value: Period) => void }) {
  const [days, setDays] = useState('7');
  function selectPeriod(selected: string) {
    const next = fixedPeriod(Number(selected));
    setDays(selected); onChange(next);
  }
  return <fieldset className="admin-period" aria-label={label}><label><select aria-label={label} value={days} onChange={(event) => selectPeriod(event.target.value)}><option value="1">За 24 часа</option><option value="7">За 7 дней</option><option value="30">За 30 дней</option></select></label></fieldset>;
}
