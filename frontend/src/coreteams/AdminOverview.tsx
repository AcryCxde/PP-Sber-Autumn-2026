import { useCallback, useRef, useState, type ReactNode } from 'react';
import { adminApi, type AdminRun, type Overview, type Period } from './adminApi';
import { useAdminPolling } from './adminHooks';
import { elapsed, fixedPeriod, formatCompactTime, formatTime, stageLabel, statusLabel, userLabel } from './adminView';
import { LoadState, Pagination, PeriodControl, RefreshStatus } from './AdminShared';
import { AdminRunDetails } from './AdminRunDetails';

function RunTime({ value, asOf }: { value: string | null; asOf: string }) {
  return value ? <time dateTime={value} title={formatTime(value)}>{formatCompactTime(value, new Date(asOf))}</time> : <>Нет данных</>;
}
function RunTable({ items, asOf, onDetails, empty }: { items: AdminRun[]; asOf: string; onDetails: (id: string) => void; empty: string }) {
  if (!items.length) return <p>{empty}</p>;
  return <div className="admin-table-wrap"><table className="admin-table admin-run-table"><thead><tr><th>Задача</th><th>Пользователь</th><th>Состояние / этап</th><th>Время</th><th>Подробнее</th></tr></thead><tbody>{items.map((run) => <tr key={run.id}><td data-label="Задача">{run.task_label}</td><td data-label="Пользователь">{userLabel(run.user)}</td><td data-label="Состояние / этап">{statusLabel(run.status)}<small>{stageLabel(run.stage)}</small></td><td data-label="Время">{run.status === 'running' || run.status === 'pending' ? <><RunTime value={run.started_at} asOf={asOf} /><small>{elapsed(run.started_at, asOf)}</small></> : <>{run.completed_at ? <RunTime value={run.completed_at} asOf={asOf} /> : 'Время завершения недоступно'}<small>Начало: <RunTime value={run.started_at} asOf={asOf} /></small></>}</td><td data-label="Подробнее"><button type="button" className="text-button" onClick={() => onDetails(run.id)}>Детали</button></td></tr>)}</tbody></table></div>;
}
export function OverviewContent({ data: currentData, clearPrevious = false, onDetails, onRuns, onActivePage, onAttentionPage, attentionControls, totalsControls, loadingContent }: { data: Overview | null; clearPrevious?: boolean; onDetails: (id: string) => void; onRuns: (status: string) => void; onActivePage: (page: number) => void; onAttentionPage: (page: number) => void; attentionControls?: ReactNode; totalsControls?: ReactNode; loadingContent?: ReactNode }) {
  const [previous, setPrevious] = useState(currentData);
  if (clearPrevious && previous) setPrevious(null);
  else if (!clearPrevious && currentData && currentData !== previous) setPrevious(currentData);
  const data = clearPrevious ? null : currentData ?? previous;
  const busy = currentData === null;
  const outdated = busy && data !== null;
  const body = (children: ReactNode) => <div className="admin-section-body" aria-busy={busy}><div inert={outdated || undefined} aria-hidden={outdated || undefined} style={outdated ? { opacity: 0.8 } : undefined}>{children}</div></div>;
  return <>
    <section className="admin-section"><div className="admin-section-header"><h2 title="Выполняющиеся и ожидающие запуски, независимо от периода">Активные запуски</h2></div>{busy && loadingContent}{body(data ? <><RunTable items={data.active.items} asOf={data.as_of} onDetails={onDetails} empty="Нет выполняющихся или ожидающих запусков." /><Pagination page={data.active.page} total={data.active.total} size={data.active.page_size} onChange={onActivePage} /></> : null)}</section>
    <section className="admin-section"><div className="admin-section-header"><h2>Требует внимания</h2>{attentionControls && <div className="admin-section-controls">{attentionControls}</div>}</div>{body(data && <><RunTable items={data.attention.items} asOf={data.as_of} onDetails={onDetails} empty="Проблем по выбранному фильтру не найдено." /><Pagination page={data.attention.page} total={data.attention.total} size={data.attention.page_size} onChange={onAttentionPage} /></>)}</section>
    <section className="admin-section"><div className="admin-section-header"><h2 title="Текущие состояния запусков, начавшихся в выбранном периоде">Итоги за период</h2>{totalsControls && <div className="admin-section-controls">{totalsControls}</div>}</div>{body(data && <div className="admin-totals">{([['all', 'Всего запусков', data.totals.runs], ['failed', 'С ошибкой', data.totals.failed], ['outcome_unknown', 'С неизвестным исходом', data.totals.outcome_unknown], ['running', 'Ещё выполняется', data.totals.running]] as const).map(([status, label, value]) => <div key={status}><button type="button" className="admin-kpi" onClick={() => onRuns(status)}><span className="admin-kpi-value">{value}</span><span className="admin-kpi-label">{label}</span></button></div>)}</div>)}</section>
  </>;
}
function RunSelection({ period, status, onDetails, onClose }: { period: Period; status: string; onDetails: (id: string) => void; onClose: () => void }) {
  const [page, setPage] = useState(1);
  const load = useCallback((signal: AbortSignal) => adminApi.runs({ ...period, status, page }, signal), [period, status, page]);
  const state = useAdminPolling(load);
  return <section className="admin-section" aria-label="Запуски за выбранный период"><h2>Выбранные запуски</h2><p>Статус: {status === 'all' ? 'Все' : statusLabel(status as 'failed' | 'running' | 'outcome_unknown')}</p><button className="text-button" type="button" onClick={onClose}>Закрыть выборку</button><RefreshStatus state={state} refresh={state.refresh} /><LoadState state={state}>{(data) => <><RunTable items={data.items} asOf={data.as_of} onDetails={onDetails} empty="Запусков в этой выборке нет." /><Pagination page={data.page} total={data.total} size={data.page_size} onChange={setPage} /></>}</LoadState></section>;
}
export function AdminOverview() {
  const [period, setPeriod] = useState(() => fixedPeriod(7)); const [attention, setAttention] = useState(() => fixedPeriod(7));
  const [activePage, setActivePage] = useState(1); const [attentionPage, setAttentionPage] = useState(1); const [noStart, setNoStart] = useState(false);
  const [selected, setSelected] = useState<string | null>(null); const [selection, setSelection] = useState<{ period: Period; status: string } | null>(null);
  const opener = useRef<HTMLElement | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const load = useCallback((signal: AbortSignal) => adminApi.overview({ ...period, attention_start: attention.start, attention_end: attention.end, active_page: activePage, attention_page: attentionPage, attention_no_start: noStart }, signal), [period, attention, activePage, attentionPage, noStart]);
  const state = useAdminPolling(load);
  const [displayAsOf, setDisplayAsOf] = useState<string | null>(null);
  const accessRevoked = state.status === 'unauthorized' || state.status === 'forbidden';
  if (accessRevoked && displayAsOf) setDisplayAsOf(null);
  else if (!accessRevoked && state.asOf && state.asOf !== displayAsOf) setDisplayAsOf(state.asOf);
  const openDetails = (id: string) => { opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null; setSelected(id); };
  const closeDetails = () => {
    setSelected(null);
    requestAnimationFrame(() => {
      const target = opener.current;
      if (target?.isConnected) target.focus();
      else heading.current?.focus();
    });
  };
  return <div className="admin-page"><div className="admin-overview-header"><h1 ref={heading} tabIndex={-1}>Обзор</h1><RefreshStatus state={{ ...state, asOf: accessRevoked ? null : state.asOf ?? displayAsOf }} refresh={state.refresh} compact /></div>
    <OverviewContent data={state.data} clearPrevious={state.status === 'unauthorized' || state.status === 'forbidden'}  onDetails={openDetails} onRuns={(status) => { if (state.data) setSelection({ period: state.data.period, status }); }} onActivePage={setActivePage} onAttentionPage={setAttentionPage}
      attentionControls={<><PeriodControl label="Период внимания" value={attention} onChange={(value) => { setAttention(value); setAttentionPage(1); }} /><label className="admin-check" title="Запуски без времени начала, независимо от периода"><input type="checkbox" checked={noStart} onChange={(event) => { setNoStart(event.target.checked); setAttentionPage(1); }} />Без даты начала</label></>}
      totalsControls={<PeriodControl label="Период итогов" value={period} onChange={(value) => { setPeriod(value); setSelection(null); }} />} />
    {selection && <RunSelection key={`${selection.status}-${selection.period.start}-${selection.period.end}`} {...selection} onDetails={openDetails} onClose={() => setSelection(null)} />}
    {selected && <AdminRunDetails key={selected} id={selected} onClose={closeDetails} />}
  </div>;
}
