import type { AdminRun, Estimate, Period, RunStatus } from './adminApi';
const STATUS: Record<RunStatus, string> = { pending: 'Ожидает запуска', running: 'Выполняется', completed: 'Технически завершён', failed: 'Ошибка', outcome_unknown: 'Исход неизвестен' };
export const statusLabel = (status: RunStatus) => STATUS[status];
export const stageLabel = (stage: AdminRun['stage']) => stage?.title ?? 'Этап недоступен';
export const userLabel = (user: AdminRun['user']) => user?.email ?? 'Пользователь не определён';
export function formatTime(value: string | null): string { return value === null ? 'Нет данных' : new Date(value).toLocaleString('ru-RU', { timeZoneName: 'short' }); }
export function elapsed(start: string | null, asOf: string): string {
  if (!start) return 'Ещё не начат';
  const minutes = Math.max(0, Math.floor((Date.parse(asOf) - Date.parse(start)) / 60000));
  return minutes < 60 ? `${minutes} мин` : `${Math.floor(minutes / 60)} ч ${minutes % 60} мин`;
}
export function estimateLabel(estimate: Pick<Estimate, 'state' | 'scope' | 'value_usd'>): string {
  if (estimate.state !== 'known' || estimate.value_usd === null) return 'Нет данных';
  return `${estimate.scope === 'session_total' ? 'Вся сессия, не отдельный запуск' : 'Доказанный прирост запуска'}: ${estimate.value_usd} USD`;
}
export function fixedPeriod(days: number, now = new Date()): Period { return { start: new Date(now.getTime() - days * 86400000).toISOString(), end: now.toISOString() }; }
export function parseExplicitPeriod(start: string, end: string): Period {
  const offset = /(Z|[+-]\d{2}:\d{2})$/;
  if (!offset.test(start) || !offset.test(end) || !Number.isFinite(Date.parse(start)) || !Number.isFinite(Date.parse(end)) || Date.parse(start) >= Date.parse(end)) throw new Error('Укажите начало и конец с явным часовым смещением, например 2026-11-12T12:00:00+03:00. Конец должен быть позже начала.');
  return { start: new Date(start).toISOString(), end: new Date(end).toISOString() };
}
export function safeProjectHref(value: string | null): string | null { return value && /^#project\/[A-Za-z0-9-]+\/(workspace|welcome|context|question|team-plan|plan)(\/\d+)?$/.test(value) ? value : null; }
