import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { AdminOverview, OverviewContent } from './AdminOverview';
import { RunDetailContent } from './AdminRunDetails';
import { AdminUsers } from './AdminUsers';
import type { Overview, RunDetails } from './adminApi';
import { AdminAvailability } from './AdminShared';

export const overview: Overview = { as_of: '2026-11-12T00:00:00Z', period: { start: '2026-11-05T00:00:00Z', end: '2026-11-12T00:00:00Z' }, completeness: 'complete', totals: { runs: 5, failed: 1, outcome_unknown: 1, running: 1 }, without_started_at: 2, active: { items: [], total: 0, page: 1, page_size: 25 }, attention: { items: [], total: 0, page: 1, page_size: 25, period: { start: '2026-11-05T00:00:00Z', end: '2026-11-12T00:00:00Z' }, no_start: false } };
export const detail: RunDetails = { as_of: overview.as_of, run: { id: 'r1', conversation_id: 'c1', project_id: null, task_label: 'Диалог', user: null, status: 'outcome_unknown', stage: null, started_at: null, completed_at: null, project_href: null, error_code: 'unclassified' }, harness_estimate: { state: 'unknown', scope: 'unknown', value_usd: null, observed_at: null, unavailable_reason: 'scope_unconfirmed', source: { kind: 'harness_result', field: 'total_cost_usd', event_id: null, journal_seq: null, runner_version: null } } };
describe('admin SSR (not browser accessibility evidence)', () => {
  it('disabled route renders a read-only unavailable view without mounting private children', () => {
    const html = renderToStaticMarkup(<AdminAvailability enabled={false}><AdminUsers currentUserId="admin" /></AdminAvailability>);
    expect(html).toContain('Администрирование временно недоступно');
    expect(html).not.toContain('Поиск по email');
    expect(html).not.toContain('Заморозить');
  });
  it('does not create a second header and initially renders loading without fabricated totals', () => {
    const html = renderToStaticMarkup(<AdminOverview />);
    expect(html).toContain('Обзор'); expect(html).not.toContain('<header'); expect(html).not.toContain('0 запусков');
  });
  it('orders operational blocks, labels the cohort and never renders a chart', () => {
    const html = renderToStaticMarkup(<OverviewContent data={overview} onDetails={() => {}} onRuns={() => {}} onActivePage={() => {}} onAttentionPage={() => {}} />);
    expect(html.indexOf('Сейчас выполняется')).toBeLessThan(html.indexOf('Требует внимания'));
    expect(html.indexOf('Требует внимания')).toBeLessThan(html.indexOf('Итоги за период'));
    expect(html).toContain('вне когорты'); expect(html).not.toContain('<svg');
  });
  it('renders safe detail with honest cost and no retry/cancel controls', () => {
    const html = renderToStaticMarkup(<RunDetailContent data={detail} />);
    expect(html).toContain('Нет данных'); expect(html).toContain('Оценка расхода по харнессу, USD');
    expect(html).toContain('проверить сохранённые результаты'); expect(html).not.toContain('Повторить запуск'); expect(html).not.toContain('$0');
  });
  it('Users initial render keeps only one page heading and no modal/private reason', () => {
    const html = renderToStaticMarkup(<AdminUsers currentUserId="admin" />);
    expect(html).toContain('Пользователи'); expect(html).not.toContain('<header'); expect(html).not.toContain('<dialog');
  });
});
