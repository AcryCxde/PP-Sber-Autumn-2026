import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { adminApi, type RunDetails } from './adminApi';
import { useAdminPolling } from './adminHooks';
import { LoadState, RefreshStatus } from './AdminShared';
import { estimateLabel, formatTime, safeProjectHref, stageLabel, statusLabel, userLabel } from './adminView';
import { copyRunId } from './adminClipboard';

export function RunDetailContent({ data, refreshStatus }: { data: RunDetails; refreshStatus?: ReactNode }) {
  const { run, harness_estimate: estimate } = data; const href = safeProjectHref(run.project_href);
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'unavailable'>('idle');
  async function copyId() {
    setCopyState(await copyRunId(run.id, navigator.clipboard) ? 'copied' : 'unavailable');
  }
  return <>
    <header className="admin-detail-heading"><p className="admin-detail-eyebrow">ДЕТАЛИ ЗАПУСКА</p><h2>{run.task_label}</h2><span className={`admin-run-status status-${run.status}`}>{statusLabel(run.status)}</span></header>
    {run.status === 'outcome_unknown' && <p className="admin-notice">Результат неизвестен. Перед повтором проверьте сохранённые результаты.</p>}
    <dl className="admin-detail-grid"><div><dt>Пользователь</dt><dd>{userLabel(run.user)}</dd></div><div><dt>Текущий этап</dt><dd>{stageLabel(run.stage)}</dd></div><div><dt>Начало</dt><dd>{formatTime(run.started_at)}</dd></div>{run.completed_at && <div><dt>Завершение</dt><dd>{formatTime(run.completed_at)}</dd></div>}</dl>
    <div className="admin-detail-cost"><span>Оценка стоимости харнесса</span><strong>{estimateLabel(estimate)}</strong><small>{estimate.state === 'known' && estimate.value_usd !== null ? 'Оценка, не фактическое списание' : 'Недостаточно подтверждённых данных для оценки. Это не означает нулевой расход.'}</small></div>
    {refreshStatus}
    <div className="admin-detail-actions">{run.user && <a href={`#admin/users?email=${encodeURIComponent(run.user.email)}`}>AI-доступ пользователя</a>}{href && <a href={href}>Открыть проект</a>}</div>
    <details className="admin-detail-technical"><summary>Техническая информация</summary><dl className="admin-details"><dt>ID запуска</dt><dd>{run.id}</dd>{run.error_code && <><dt>Код ошибки</dt><dd>{run.error_code}</dd></>}<dt>Источник оценки</dt><dd>{estimate.source.kind} / {estimate.source.field}</dd><dt>Данные получены</dt><dd>{formatTime(estimate.observed_at)}</dd></dl><button type="button" onClick={() => { void copyId(); }}>{copyState === 'copied' ? 'ID скопирован' : 'Скопировать ID запуска'}</button>{copyState === 'unavailable' && <div role="status"><p>Не удалось скопировать ID. Скопируйте его вручную.</p><label>ID запуска <input readOnly value={run.id} onFocus={(event) => event.currentTarget.select()} /></label></div>}</details>
  </>;
}
export function AdminRunDetails({ id, onClose }: { id: string; onClose: () => void }) {
  const load = useCallback((signal: AbortSignal) => adminApi.run(id, signal), [id]);
  const state = useAdminPolling(load);
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    element?.showModal();
    return () => { element?.close(); document.body.style.overflow = previousOverflow; };
  }, []);
  return <dialog ref={dialog} className="admin-detail-panel" aria-label="Детали запуска" onCancel={(event) => { event.preventDefault(); onClose(); }}><button type="button" className="admin-detail-close" onClick={onClose} autoFocus aria-label="Закрыть детали">×</button>{!state.data && <RefreshStatus state={state} refresh={state.refresh} />}<LoadState state={state}>{(data) => <RunDetailContent data={data} refreshStatus={<RefreshStatus state={state} refresh={state.refresh} />} />}</LoadState></dialog>;
}
