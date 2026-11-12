import { useCallback, useState } from 'react';
import { adminApi, type RunDetails } from './adminApi';
import { useAdminPolling } from './adminHooks';
import { LoadState, RefreshStatus } from './AdminShared';
import { estimateLabel, formatTime, safeProjectHref, stageLabel, statusLabel, userLabel } from './adminView';
import { copyRunId } from './adminClipboard';

export function RunDetailContent({ data }: { data: RunDetails }) {
  const { run, harness_estimate: estimate } = data; const href = safeProjectHref(run.project_href);
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'unavailable'>('idle');
  async function copyId() {
    setCopyState(await copyRunId(run.id, navigator.clipboard) ? 'copied' : 'unavailable');
  }
  return <><h3>Запуск {run.id}</h3><dl className="admin-details"><dt>Контекст</dt><dd>{run.task_label}</dd><dt>Пользователь</dt><dd>{userLabel(run.user)}</dd><dt>Состояние</dt><dd>{statusLabel(run.status)}</dd><dt>Этап</dt><dd>{stageLabel(run.stage)}</dd><dt>Начало</dt><dd>{formatTime(run.started_at)}</dd><dt>Исход</dt><dd>{formatTime(run.completed_at)}</dd><dt>Код</dt><dd>{run.error_code ?? 'Нет кода'}</dd></dl>
    {run.status === 'outcome_unknown' && <p className="admin-notice">Исход неизвестен; перед повтором требуется проверить сохранённые результаты. Автоматического повторного запуска нет.</p>}
    <p>Для разбора передайте Run ID ответственному. Длительность сама по себе не доказывает зависание.</p>
    <button className="btn secondary" type="button" onClick={() => { void copyId(); }}>{copyState === 'copied' ? 'Run ID скопирован' : 'Скопировать Run ID'}</button>
    {copyState === 'unavailable' && <div role="status"><p>Не удалось скопировать Run ID. Выделите и скопируйте его вручную.</p><label>Run ID для ручного копирования <input readOnly value={run.id} onFocus={(event) => event.currentTarget.select()} /></label></div>}
    {run.user && <a className="btn secondary" href={`#admin/users?email=${encodeURIComponent(run.user.email)}`}>Проверить AI-доступ пользователя</a>}{href && <a className="btn secondary" href={href}>Открыть проект</a>}
    <section className="admin-section"><h3>Оценка расхода по харнессу, USD</h3><p>{estimateLabel(estimate)}</p><p className="helper">Это оценка харнесса, не фактическое списание, не остаток подписки и не проверенный API-эквивалент upstream. Итоги сессии нельзя складывать как расходы отдельных запусков.</p><p>Наблюдение: {formatTime(estimate.observed_at)} · Источник: {estimate.source.kind} / {estimate.source.field}</p>{estimate.state === 'unknown' && <p>Недостаточно подтверждённых данных об области расчёта. Неизвестность не означает нулевой расход.</p>}</section></>;
}
export function AdminRunDetails({ id, onClose }: { id: string; onClose: () => void }) {
  const load = useCallback((signal: AbortSignal) => adminApi.run(id, signal), [id]);
  const state = useAdminPolling(load);
  return <aside className="admin-detail-panel" aria-label="Детали запуска"><button type="button" className="text-button" onClick={onClose}>Закрыть детали</button><RefreshStatus state={state} refresh={state.refresh} /><LoadState state={state}>{(data) => <RunDetailContent data={data} />}</LoadState></aside>;
}
