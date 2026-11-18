import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { AdminOverview, OverviewContent } from './AdminOverview';
import { RunDetailContent } from './AdminRunDetails';
import { AdminUsers } from './AdminUsers';
import type { Overview, RunDetails } from './adminApi';
import { AdminAvailability, Pagination, PeriodControl, RefreshStatus } from './AdminShared';
import { formatTime } from './adminView';
import { initialPollState } from './adminPolling';

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
    expect(html.indexOf('Активные запуски')).toBeLessThan(html.indexOf('Требует внимания'));
    expect(html.indexOf('Требует внимания')).toBeLessThan(html.indexOf('Итоги за период'));
    expect(html).not.toContain('<svg'); expect(html).not.toContain('не включительно');
    expect(html).toContain('class="admin-kpi-value">5</span>');
    expect(html).toContain('class="admin-kpi-label">Всего запусков</span>');
    expect(html).not.toContain(' из 5');
  });
  it('keeps task, user, status and time in tables without technical identifiers', () => {
    const run = { ...detail.run, id: 'run-id-technical-only', error_code: 'error-technical-only', task_label: 'Проверка плана', user: { id: 'u1', email: 'reader@example.test' }, status: 'failed' as const };
    const data: Overview = { ...overview, active: { ...overview.active, total: 2, items: [{ ...run, id: 'active-technical-only', status: 'running', started_at: overview.period.start }, { ...run, id: 'pending-technical-only', status: 'pending' }] }, attention: { ...overview.attention, total: 2, items: [run, { ...run, id: 'completed-technical-only', status: 'completed', completed_at: overview.as_of }] } };
    const html = renderToStaticMarkup(<OverviewContent data={data} onDetails={() => {}} onRuns={() => {}} onActivePage={() => {}} onAttentionPage={() => {}} />);
    expect(html).toContain(`title="${formatTime(overview.period.start)}"`);
    expect(html).toContain('5 ноя,');
    expect(html).not.toContain('run-id-technical-only');
    for (const text of ['Проверка плана', 'reader@example.test', 'Ошибка', 'Выполняется', 'Технически завершён', 'Ещё не начат', 'Время завершения недоступно', 'Детали', '<th>Задача</th>', 'data-label="Задача"', 'Пользователь', 'Состояние / этап', 'Время']) expect(html).toContain(text);
    for (const text of ['active-technical-only', 'pending-technical-only', 'completed-technical-only', 'error-technical-only']) expect(html).not.toContain(text);
  });
  it('preserves identifiers and error inside closed technical detail', () => {
    const html = renderToStaticMarkup(<RunDetailContent data={{ ...detail, run: { ...detail.run, id: 'run-id-technical-only', error_code: 'error-technical-only' } }} />);
    const technical = html.match(/<details\b[^>]*>[\s\S]*?<\/details>/)?.[0];
    expect(technical).toBeDefined();
    expect(technical).not.toMatch(/<details\b[^>]*\bopen(?:[\s=>])/);
    for (const text of ['run-id-technical-only', 'error-technical-only']) expect(technical!.indexOf(text)).toBeGreaterThan(technical!.indexOf('<summary>Техническая информация</summary>'));
  });
  it('renders safe detail with honest cost and no retry/cancel controls', () => {
    const html = renderToStaticMarkup(<RunDetailContent data={detail} />);
    expect(html).toContain('Нет данных'); expect(html).toContain('Оценка стоимости');
    expect(html).toContain('проверьте сохранённые результаты'); expect(html).not.toContain('Повторить запуск'); expect(html).not.toContain('$0');
  });
  it('keeps filters in stable scoped headers even before data loads', () => {
    const html = renderToStaticMarkup(<AdminOverview />);
    expect(html).toContain('class="admin-overview-header"');
    expect(html.indexOf('Активные запуски')).toBeLessThan(html.indexOf('За 24 часа'));
    expect(html).toContain('Без даты начала');
    expect(html).toContain('title="Запуски без времени начала, независимо от периода"');
    expect(html).not.toContain('Дополнительный фильтр');
    expect(html).not.toContain('Активные запуски не ограничены периодом');
    expect(html.match(/<fieldset/g)).toHaveLength(2);
  });
  it('places optional controls in their section headers with or without data', () => {
    for (const data of [overview, null]) {
      const html = renderToStaticMarkup(<OverviewContent data={data} onDetails={() => {}} onRuns={() => {}} onActivePage={() => {}} onAttentionPage={() => {}} attentionControls={<button>Attention filter</button>} totalsControls={<button>Totals filter</button>} loadingContent={<p>Loading once</p>} />);
      expect(html).toContain('<h2>Требует внимания</h2><div class="admin-section-controls"><button>Attention filter</button>');
      expect(html).toContain('<div class="admin-section-controls"><button>Totals filter</button>');
      expect(html.match(/Attention filter/g)).toHaveLength(1);
      expect(html.match(/Totals filter/g)).toHaveLength(1);
      expect(html.match(/Loading once/g)?.length ?? 0).toBe(data ? 0 : 1);
    }
  });
  it.each([
    { status: 'unauthorized' as const, refreshError: false, text: 'Требуется вход' },
    { status: 'forbidden' as const, refreshError: false, text: 'Недостаточно прав' },
    { status: 'error' as const, refreshError: true, text: 'Ошибка обновления' },
  ])('keeps compact $status failures visible to assistive technology', ({ status, refreshError, text }) => {
    const html = renderToStaticMarkup(<RefreshStatus compact state={{ ...initialPollState(), status, refreshError }} refresh={() => {}} />);
    expect(html).toContain('role="alert"');
    expect(html).toContain(text);
    expect(html).not.toContain('Обновление…');
  });
  it('clears presentation snapshot when access is revoked', () => {
    const html = renderToStaticMarkup(<OverviewContent data={overview} clearPrevious onDetails={() => {}} onRuns={() => {}} onActivePage={() => {}} onAttentionPage={() => {}} />);
    expect(html).not.toContain('class="admin-kpi"');
    expect(html).not.toContain('Всего:');
  });
  it('marks loading bodies busy while keeping scoped controls outside them', () => {
    const html = renderToStaticMarkup(<OverviewContent data={null} onDetails={() => {}} onRuns={() => {}} onActivePage={() => {}} onAttentionPage={() => {}} attentionControls={<button>Current filter</button>} loadingContent={<p role="status">Обновление…</p>} />);
    expect(html.match(/class="admin-section-body" aria-busy="true"/g)).toHaveLength(3);
    expect(html).toContain('<div class="admin-section-controls"><button>Current filter</button></div>');
    expect(html).toContain('Обновление…');
  });
  it('offers exactly three presets with seven days selected and no custom controls', () => {
    const html = renderToStaticMarkup(<PeriodControl label="Итоги за период" value={overview.period} onChange={() => {}} />);
    expect(html.match(/<option\b/g)).toHaveLength(3);
    expect(html).toContain('<option value="7" selected="">За 7 дней</option>');
    expect(html).not.toContain('Свой интервал');
    expect(html).not.toContain('<input');
    for (const text of ['За 24 часа', 'За 7 дней', 'За 30 дней']) expect(html).toContain(text);
    for (const text of ['Часовой пояс', 'UTC', 'GMT', 'не включительно', 'Применить']) expect(html).not.toContain(text);
  });
  it.each([0, 2, 25])('shows only result count for a single page with %i results', (total) => {
    const html = renderToStaticMarkup(<Pagination page={1} total={total} size={25} onChange={() => {}} />);
    expect(html).toContain(`Всего: ${total}`);
    expect(html).not.toContain('<button');
    expect(html).not.toContain('Страница');
  });
  it.each([{ page: 2, total: 0 }, { page: 2, total: 2 }, { page: 1, total: 26 }])('keeps navigation for multiple or out-of-range pages: %o', ({ page, total }) => {
    const html = renderToStaticMarkup(<Pagination page={page} total={total} size={25} onChange={() => {}} />);
    expect(html).toContain('Назад');
    expect(html).toContain('Далее');
    expect(html).toContain(`Страница ${page}`);
    if (page > 1) expect(html).toContain('type="button">Назад</button>');
  });
  it.each([
    { scope: 'run_delta' as const, amount: '0', label: 'Оценка этого запуска: 0 USD' },
    { scope: 'session_total' as const, amount: '0.062745', label: 'Оценка всей сессии, не отдельного запуска: 0.062745 USD' },
  ])('renders honest known $scope estimate and technical provenance', ({ scope, amount, label }) => {
    const observedAt = overview.as_of;
    const data: RunDetails = { ...detail, run: { ...detail.run, project_href: '#project/safe-project/workspace' }, harness_estimate: { ...detail.harness_estimate, state: 'known', scope, value_usd: amount, observed_at: observedAt } };
    const html = renderToStaticMarkup(<RunDetailContent data={data} />);
    expect(html).toContain(label);
    expect(html).toContain('не фактическое списание');
    expect(html).toContain('href="#project/safe-project/workspace"');
    expect(html).not.toContain('Повторить запуск');
    expect(html).not.toContain('Отменить запуск');
    const technical = html.match(/<details\b[^>]*>[\s\S]*?<\/details>/)?.[0] ?? '';
    for (const text of [detail.harness_estimate.source.kind, detail.harness_estimate.source.field, formatTime(observedAt)]) expect(technical).toContain(text);
  });
  it('does not fabricate zero for unknown cost', () => {
    const html = renderToStaticMarkup(<RunDetailContent data={detail} />);
    expect(html).toContain('Нет данных');
    expect(html).not.toContain('0 USD');
  });
  it('Users initial render keeps only one page heading and no modal/private reason', () => {
    const html = renderToStaticMarkup(<AdminUsers currentUserId="admin" />);
    expect(html).toContain('Пользователи'); expect(html).not.toContain('<header'); expect(html).not.toContain('<dialog');
  });
});
