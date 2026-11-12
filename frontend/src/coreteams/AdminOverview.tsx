import { useCallback, useRef, useState } from 'react';
import { adminApi, type AdminRun, type Overview, type Period } from './adminApi';
import { useAdminPolling } from './adminHooks';
import { elapsed, fixedPeriod, formatTime, stageLabel, statusLabel, userLabel } from './adminView';
import { LoadState, Pagination, PeriodControl, RefreshStatus } from './AdminShared';
import { AdminRunDetails } from './AdminRunDetails';

function RunTable({ items, asOf, onDetails, empty }: { items: AdminRun[]; asOf: string; onDetails: (id: string) => void; empty: string }) {
  if (!items.length) return <p>{empty}</p>;
  return <div className="admin-table-wrap"><table className="admin-table"><thead><tr><th>Задача / запуск</th><th>Пользователь</th><th>Состояние / этап</th><th>Время</th><th>Разбор</th></tr></thead><tbody>{items.map((run) => <tr key={run.id}><td data-label="Задача / запуск">{run.task_label}<small>{run.id}</small></td><td data-label="Пользователь">{userLabel(run.user)}</td><td data-label="Состояние / этап">{statusLabel(run.status)}<small>{stageLabel(run.stage)}</small>{run.error_code && <small>Код: {run.error_code}</small>}</td><td data-label="Время">{run.status === 'running' || run.status === 'pending' ? <>{formatTime(run.started_at)}<small>{elapsed(run.started_at, asOf)}</small></> : <>{run.completed_at ? formatTime(run.completed_at) : 'Время исхода недоступно'}<small>Начало: {formatTime(run.started_at)}</small></>}</td><td data-label="Разбор"><button type="button" className="text-button" onClick={() => onDetails(run.id)}>Детали</button></td></tr>)}</tbody></table></div>;
}
export function OverviewContent({ data, onDetails, onRuns, onActivePage, onAttentionPage }: { data: Overview; onDetails: (id: string) => void; onRuns: (status: string) => void; onActivePage: (page: number) => void; onAttentionPage: (page: number) => void }) {
  const { active, attention, totals } = data;
  return <><section className="admin-section"><h2>Сейчас выполняется</h2><p className="helper">Что работает сейчас и кого затрагивает задержка. Ожидающие запуска показаны отдельно своим статусом; период итогов этот список не ограничивает.</p><RunTable items={active.items} asOf={data.as_of} onDetails={onDetails} empty="Нет выполняющихся или ожидающих запусков." /><Pagination page={active.page} total={active.total} size={active.page_size} onChange={onActivePage} /></section>
    <section className="admin-section"><h2>Требует внимания</h2><p className="helper">Ошибки и неизвестные исходы — для разбора конкретных запусков, не очередь подтверждённых инцидентов.</p><p>{attention.no_start ? 'Выборка без времени начала, вне периода' : `По времени начала: [${attention.period.start}, ${attention.period.end})`}</p><RunTable items={attention.items} asOf={data.as_of} onDetails={onDetails} empty="Проблем по выбранному фильтру не найдено." /><Pagination page={attention.page} total={attention.total} size={attention.page_size} onChange={onAttentionPage} /></section>
    <section className="admin-section"><h2>Итоги за период</h2><p>Масштаб проблем среди запусков с началом в [{data.period.start}, {data.period.end}). Состояния на момент снимка; техническое завершение не означает принятие результата.</p><dl className="admin-totals">{([['all', 'Всего запусков', totals.runs], ['failed', 'С ошибкой', totals.failed], ['outcome_unknown', 'С неизвестным исходом', totals.outcome_unknown], ['running', 'Ещё выполняется', totals.running]] as const).map(([status, label, value]) => <div key={status}><dt>{label}</dt><dd><button type="button" className="text-button" onClick={() => onRuns(status)}>{value}{status !== 'all' ? ` из ${totals.runs}` : ''}</button></dd></div>)}</dl><p>Без времени начала: {data.without_started_at}, вне когорты. Ошибки, неизвестные и выполняющиеся не обязаны в сумме давать общее число.</p></section></>;
}
function RunSelection({ period, status, onDetails, onClose }: { period: Period; status: string; onDetails: (id: string) => void; onClose: () => void }) {
  const [page, setPage] = useState(1);
  const load = useCallback((signal: AbortSignal) => adminApi.runs({ ...period, status, page }, signal), [period, status, page]);
  const state = useAdminPolling(load);
  return <section className="admin-section" aria-label="Запуски выбранной когорты"><h2>Выбранные запуски</h2><p>Та же когорта [{period.start}, {period.end}) · фильтр: {status === 'all' ? 'Все' : statusLabel(status as 'failed' | 'running' | 'outcome_unknown')}</p><button className="text-button" type="button" onClick={onClose}>Закрыть выборку</button><RefreshStatus state={state} refresh={state.refresh} /><LoadState state={state}>{(data) => <><RunTable items={data.items} asOf={data.as_of} onDetails={onDetails} empty="Запусков в этой выборке нет." /><Pagination page={data.page} total={data.total} size={data.page_size} onChange={setPage} /></>}</LoadState></section>;
}
export function AdminOverview() {
  const [period, setPeriod] = useState(() => fixedPeriod(7)); const [attention, setAttention] = useState(() => fixedPeriod(7));
  const [activePage, setActivePage] = useState(1); const [attentionPage, setAttentionPage] = useState(1); const [noStart, setNoStart] = useState(false);
  const [selected, setSelected] = useState<string | null>(null); const [selection, setSelection] = useState<{ period: Period; status: string } | null>(null);
  const opener = useRef<HTMLElement | null>(null);
  const load = useCallback((signal: AbortSignal) => adminApi.overview({ ...period, attention_start: attention.start, attention_end: attention.end, active_page: activePage, attention_page: attentionPage, attention_no_start: noStart }, signal), [period, attention, activePage, attentionPage, noStart]);
  const state = useAdminPolling(load);
  const openDetails = (id: string) => { opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null; setSelected(id); };
  const closeDetails = () => { setSelected(null); opener.current?.focus(); };
  return <div className="admin-page"><h1>Обзор</h1><p className="helper">Оперативные данные: найти выполняющуюся задачу, разобрать сбой и оценить масштаб проблемы.</p><RefreshStatus state={state} refresh={state.refresh} />
    <div className="admin-filters"><PeriodControl label="Когорта итогов" value={period} onChange={(value) => { setPeriod(value); setSelection(null); }} /><PeriodControl label="Период проблем" value={attention} onChange={(value) => { setAttention(value); setAttentionPage(1); }} /><label className="admin-check"><input type="checkbox" checked={noStart} onChange={(event) => { setNoStart(event.target.checked); setAttentionPage(1); }} />Проблемы без времени начала, вне периода</label></div>
    <LoadState state={state}>{(data) => <OverviewContent data={data} onDetails={openDetails} onRuns={(status) => setSelection({ period: data.period, status })} onActivePage={setActivePage} onAttentionPage={setAttentionPage} />}</LoadState>
    {selection && <RunSelection key={`${selection.status}-${selection.period.start}-${selection.period.end}`} {...selection} onDetails={openDetails} onClose={() => setSelection(null)} />}
    {selected && <AdminRunDetails key={selected} id={selected} onClose={closeDetails} />}
  </div>;
}
