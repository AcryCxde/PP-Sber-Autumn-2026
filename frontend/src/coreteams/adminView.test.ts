import { describe, expect, it } from 'vitest';
import { formatCompactTime, formatClock, fixedPeriod, parseExplicitPeriod, statusLabel, estimateLabel, stageLabel, userLabel } from './adminView';
import { parseRoute, routeHash } from './router';

describe('admin routes and honest view models', () => {
  it('roundtrips both pages, preserves aliases and regular routes', () => {
    expect(parseRoute('#admin')).toEqual({ page: 'admin', section: 'overview' });
    expect(routeHash(parseRoute('#admin/users'))).toBe('#admin/users');
    expect(routeHash(parseRoute('#admin/overview'))).toBe('#admin/overview');
    expect(routeHash(parseRoute('#project/p1/workspace'))).toBe('#project/p1/workspace');
  });
  it('distinguishes execution and outcome without claiming acceptance or a stalled task', () => {
    expect(statusLabel('pending')).toBe('Ожидает запуска');
    expect(statusLabel('running')).toBe('Выполняется');
    expect(statusLabel('completed')).toBe('Технически завершён');
    expect(statusLabel('failed')).not.toBe(statusLabel('outcome_unknown'));
    expect(stageLabel(null)).toBe('Этап недоступен');
    expect(userLabel(null)).toBe('Пользователь не определён');
  });
  it('never turns an unknown estimate into zero or a session total into run cost', () => {
    expect(estimateLabel({ state: 'unknown', scope: 'unknown', value_usd: null })).toBe('Нет данных');
    expect(estimateLabel({ state: 'known', scope: 'run_delta', value_usd: '0' })).toBe('Оценка этого запуска: 0 USD');
    expect(estimateLabel({ state: 'known', scope: 'session_total', value_usd: '0.062745' })).toBe('Оценка всей сессии, не отдельного запуска: 0.062745 USD');
    expect(estimateLabel({ state: 'known', scope: 'run_delta', value_usd: null })).toBe('Нет данных');
    expect(estimateLabel({ state: 'known', scope: 'run_delta', value_usd: '123456789.12345678901234567890' })).toBe('Оценка этого запуска: 123456789.12345678901234567890 USD');
  });
  it('formats compact local table timestamps without losing calendar context', () => {
    const now = new Date(2026, 10, 14, 12, 0);
    expect(formatClock(new Date(2026, 10, 14, 11, 5).toISOString())).toBe('11:05');
    expect(formatCompactTime(new Date(2026, 10, 14, 11, 5).toISOString(), now)).toBe('Сегодня, 11:05');
    expect(formatCompactTime(new Date(2026, 10, 7, 11, 5).toISOString(), now)).toBe('7 ноя, 11:05');
    expect(formatCompactTime(new Date(2025, 10, 7, 11, 5).toISOString(), now)).toBe('7 ноя 2025, 11:05');
    expect(formatCompactTime(null, now)).toBe('Нет данных');
  });
  it('freezes period bounds and requires explicit timezone through DST', () => {
    const period = fixedPeriod(7, new Date('2026-11-12T00:00:00Z'));
    expect(period).toEqual({ start: '2026-11-05T00:00:00.000Z', end: '2026-11-12T00:00:00.000Z' });
    expect(() => parseExplicitPeriod('2026-11-01T01:30', '2026-11-01T02:30')).toThrow();
    expect(parseExplicitPeriod('2026-11-01T01:30:00-04:00', '2026-11-01T01:30:00-05:00')).toEqual({ start: '2026-11-01T05:30:00.000Z', end: '2026-11-01T06:30:00.000Z' });
  });
});
