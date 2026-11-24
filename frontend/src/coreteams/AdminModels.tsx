import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { AdminApiError, adminApi, type ModelHealth, type ModelRouting, type RoutingAudit, type RoutingSettings } from './adminApi';
import { useAdminFailure, useAdminPolling } from './adminHooks';
import { LoadState, Pagination } from './AdminShared';
import { formatClock, formatTime } from './adminView';

const healthLabels: Record<ModelHealth['state'], string> = {
  available: 'Отвечает', unavailable: 'Недоступна', incompatible: 'Несовместима', stale: 'Проверка устарела',
};
const pausedTitle = 'Приостановить новые запуски агентов';

async function loadRouting(signal: AbortSignal) {
  try {
    const routing = await adminApi.modelRouting(signal);
    return { as_of: routing.as_of, routing };
  } catch (error) {
    if (error instanceof AdminApiError && error.code === 'model_routing_disabled')
      return { as_of: new Date().toISOString(), routing: null };
    throw error;
  }
}

function errorText(error: unknown) {
  if (error instanceof AdminApiError) {
    if (error.code === 'duplicate_model') return 'Основная и резервная модели должны указывать на разные маршруты.';
    if (error.code === 'model_not_ready') return 'Модель пока недоступна. Проверьте другую модель.';
    if (error.code === 'model_not_in_catalog') return 'Модель исчезла из каталога Brotherhood.';
    if (error.code === 'model_catalog_unavailable') return 'Каталог Brotherhood недоступен.';
    if (error.status === 409) return 'Настройки изменились. Проверьте актуальное состояние и повторите действие.';
    if (error.code === 'invalid_admin_origin') return error.message;
  }
  return 'Не удалось обновить настройки или проверить модель.';
}

function ModelPicker({ label, selected, opposite, models, disabled, onSelect }: {
  label: string; selected: string; opposite: string; models: ModelHealth[]; disabled: boolean;
  onSelect: (model: ModelHealth) => void;
}) {
  const labelId = useId();
  const valueId = useId();
  const picker = useRef<HTMLDetailsElement>(null);
  const summary = useRef<HTMLElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState('');
  const choices = models.filter((model) => model.id.toLowerCase().includes(query.trim().toLowerCase()));
  const otherModel = models.find((model) => model.id === opposite);
  function close() {
    if (picker.current) picker.current.open = false;
    summary.current?.focus();
  }
  return <div className="admin-model-picker">
    <span id={labelId}>{label}</span>
    <details name="admin-model-options" ref={picker} onToggle={(event) => {
      if (event.currentTarget.open) { setQuery(''); input.current?.focus(); }
    }} onKeyDown={(event) => {
      if (event.key === 'Escape' && event.currentTarget.open) { event.preventDefault(); close(); }
    }}>
      <summary ref={summary} aria-labelledby={`${labelId} ${valueId}`}><code id={valueId}>{selected}</code></summary>
      <div className="admin-model-options">
        <input ref={input} type="search" aria-label={`Поиск · ${label.toLowerCase()}`} placeholder="Поиск модели"
          value={query} onChange={(event) => setQuery(event.target.value)} />
        <ul aria-label={label}>
          {choices.slice(0, 8).map((model) => {
            const sameRoute = model.id !== opposite && model.reported_model !== null && model.reported_model === otherModel?.reported_model;
            const hint = model.id === selected ? 'Выбрана' : sameRoute ? 'Тот же маршрут' :
              model.state !== 'available' ? 'Проверить и выбрать' :
              model.id === opposite ? 'Поменять местами' : null;
            return <li key={model.id}><button type="button" disabled={disabled || model.id === selected || sameRoute}
              onClick={() => { close(); onSelect(model); }}>
              <code>{model.id}</code>{hint && <small>{hint}</small>}
            </button></li>;
          })}
        </ul>
        {!choices.length && <p className="helper">Модели не найдены.</p>}
        {choices.length > 8 && <p className="helper">Введите более точное имя модели.</p>}
      </div>
    </details>
  </div>;
}

function Catalog({ models, busy, onCheck }: { models: ModelHealth[]; busy: boolean; onCheck: (id: string) => void }) {
  const [query, setQuery] = useState('');
  const [result, setResult] = useState<{ query: string; items: string[]; total: number; error?: string }>();
  const handleFailure = useAdminFailure();
  const search = query.trim();
  useEffect(() => {
    if (!search) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void adminApi.modelCatalog(search, controller.signal).then((data) => {
        if (!controller.signal.aborted) setResult({ query: search, ...data });
      }).catch((error: unknown) => {
        if (!controller.signal.aborted && !handleFailure(error))
          setResult({ query: search, items: [], total: 0, error: errorText(error) });
      });
    }, 200);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [search, handleFailure]);
  const shown = search && result?.query === search ? result : undefined;
  const items = shown?.items.filter((id) => !models.some((model) => id === model.id)) ?? [];
  return <details className="admin-model-catalog">
    <summary>Найти в каталоге</summary>
    <input type="search" maxLength={128} aria-label="Имя модели в каталоге" placeholder="Например, glm-5.3-flash"
      value={query} onChange={(event) => setQuery(event.target.value)} />
    {search && !shown && <p className="helper" role="status">Поиск…</p>}
    {shown?.error && <p role="alert">{shown.error}</p>}
    {shown && !shown.error && !items.length && <p className="helper">Новых моделей по этому запросу нет.</p>}
    <ul className="admin-model-list" aria-label="Результаты поиска">{items.map((id) =>
      <li key={id}><code>{id}</code><button className="btn secondary" type="button" disabled={busy} onClick={() => onCheck(id)}>Проверить</button></li>)}</ul>
    {!!shown && shown.total > 8 && <p className="helper">Показаны первые 8 из {shown.total} моделей.</p>}
  </details>;
}

function PauseConfirmation({ onCancel, onConfirm }: { onCancel: () => void; onConfirm: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => element?.close();
  }, []);
  return <dialog ref={dialog} className="admin-policy-dialog admin-model-dialog"
    aria-labelledby="model-pause-title" aria-describedby="model-pause-detail"
    onCancel={(event) => { event.preventDefault(); onCancel(); }}>
    <h2 id="model-pause-title">{pausedTitle}?</h2>
    <p id="model-pause-detail">Пользователи CoreTeams временно не смогут запускать агентов. Текущие запуски продолжатся. Проекты, история и документы останутся доступны.</p>
    <div className="admin-actions">
      <button className="btn secondary" type="button" autoFocus onClick={onCancel}>Отмена</button>
      <button className="btn primary" type="button" onClick={onConfirm}>Приостановить запуски</button>
    </div>
  </dialog>;
}

export function AdminModels() {
  const state = useAdminPolling(loadRouting);
  const handleFailure = useAdminFailure();
  const [latest, setLatest] = useState<ModelRouting>();
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [error, setError] = useState('');
  const operation = useRef<AbortController | null>(null);
  const data = state.data?.routing;
  const routing = latest && data && Date.parse(latest.as_of) > Date.parse(data.as_of) ? latest : data;
  useEffect(() => () => operation.current?.abort(), []);

  async function update(action: (signal: AbortSignal) => Promise<ModelRouting>) {
    if (operation.current) return;
    const controller = new AbortController();
    operation.current = controller;
    setBusy(true);
    setError('');
    try {
      const next = await action(controller.signal);
      if (!controller.signal.aborted) setLatest(next);
    } catch (error) {
      if (!controller.signal.aborted && !handleFailure(error)) setError(errorText(error));
    } finally {
      if (operation.current === controller) operation.current = null;
      if (!controller.signal.aborted) {
        setBusy(false);
        state.refresh();
      }
    }
  }

  function changePaused(paused: boolean) {
    if (!routing) return;
    setConfirm(false);
    void update((signal) => adminApi.changeModelRouting({
      primary_model: routing.primary_model, reserve_model: routing.reserve_model, paused, expected_version: routing.version,
    }, signal));
  }

  function choose(role: 'primary_model' | 'reserve_model', model: ModelHealth) {
    if (!routing) return;
    const other = role === 'primary_model' ? 'reserve_model' : 'primary_model';
    const result: RoutingSettings = { primary_model: routing.primary_model, reserve_model: routing.reserve_model, paused: routing.paused };
    if (model.id === result[other]) result[other] = result[role];
    result[role] = model.id;
    void update(async (signal) => {
      if (model.state !== 'available') {
        const verified = await adminApi.checkModels(model.id, signal);
        signal.throwIfAborted();
        setLatest(verified);
        if (verified.models.find((item) => item.id === model.id)?.state !== 'available')
          throw new AdminApiError(422, 'model_not_ready');
      }
      return adminApi.changeModelRouting({ ...result, expected_version: routing.version }, signal);
    });
  }

  return <section className="admin-section admin-models">
    <div className="admin-model-header"><h1>AI-модели</h1><a href="#admin/models/history">История</a></div>
    <LoadState state={state}>{() => routing ? <>
      <div className="admin-model-status" data-state={!routing.active_model ? 'unavailable' :
        routing.active_model === routing.reserve_model ? 'reserve' : 'available'} role="status">
        <strong>{routing.paused ? 'Новые запуски приостановлены' : !routing.active_model ? 'Запуски агентов недоступны' :
          routing.active_model === routing.reserve_model ? 'Работаем на резерве' : 'Запуски агентов доступны'}</strong>
        <p>{routing.active_model ? <>Модель для новых запусков · <code>{routing.active_model}</code></> :
          routing.paused ? 'Приостановлены администратором для всех пользователей.' :
          routing.reason === 'checking' ? 'Ожидаем первой проверки моделей.' :
          routing.reason === 'gateway_unavailable' ? 'Шлюз Brotherhood недоступен.' : 'Основная и резервная модели недоступны.'}</p>
      </div>
      <div className="admin-model-header">
        <span className="helper">{routing.last_checked_at ? `Проверено в ${formatClock(routing.last_checked_at)}` : 'Проверяем маршруты'} · каждые {routing.check_interval_seconds / 60} мин</span>
        <div className="admin-actions">
          <button type="button" className="btn secondary" disabled={busy} onClick={() => void update((signal) => adminApi.checkModels(undefined, signal))}>Проверить сейчас</button>
          <button type="button" className="btn secondary" disabled={busy || state.stale}
            onClick={() => routing.paused ? changePaused(false) : setConfirm(true)}>{routing.paused ? 'Снять паузу' : pausedTitle}</button>
        </div>
      </div>
      {error && <p role="alert" className="admin-notice">{error}</p>}
      {state.refreshError && <p role="status" className="helper">Не удалось обновить состояние.</p>}
      <div className="admin-model-fields">
        <ModelPicker label="Основная модель" selected={routing.primary_model} opposite={routing.reserve_model} models={routing.models}
          disabled={busy || state.stale} onSelect={(model) => choose('primary_model', model)} />
        <ModelPicker label="Резервная модель" selected={routing.reserve_model} opposite={routing.primary_model} models={routing.models}
          disabled={busy || state.stale} onSelect={(model) => choose('reserve_model', model)} />
      </div>
      <section className="admin-model-checked">
        <h2>Проверенные модели</h2>
        <ul className="admin-model-list">{[...routing.models].sort((a, b) => modelOrder(a.id, routing) - modelOrder(b.id, routing))
          .map((model) => <li key={model.id}><code>{model.id}</code>
            <span className="admin-model-health" data-state={model.state}>{healthLabels[model.state]}</span></li>)}</ul>
        <Catalog models={routing.models} busy={busy} onCheck={(id) => void update((signal) => adminApi.checkModels(id, signal))} />
      </section>
      {confirm && <PauseConfirmation onCancel={() => setConfirm(false)} onConfirm={() => changePaused(true)} />}
    </> : <p>Управление моделями пока не включено.</p>}</LoadState>
    {state.status === 'error' && <button className="btn secondary" type="button" onClick={state.refresh}>Обновить</button>}
  </section>;
}

function modelOrder(id: string, routing: RoutingSettings) {
  return id === routing.primary_model ? 0 : id === routing.reserve_model ? 1 : 2;
}

function auditText(item: RoutingAudit) {
  if (item.action === 'model_checked') return `Добавлена в проверенные · ${item.model}`;
  if (item.action === 'automatic') {
    if (!item.after?.active_model)
      return item.after?.reason === 'gateway_unavailable' ? 'Шлюз недоступен · запуски закрыты' : 'Рабочих моделей нет · запуски закрыты';
    return `Автоматически выбрана модель ${item.after.active_model}`;
  }
  if (!item.after) return 'Изменены настройки';
  const changes: string[] = [];
  if (item.before?.primary_model !== item.after.primary_model) changes.push(`Основная модель · ${item.after.primary_model}`);
  if (item.before?.reserve_model !== item.after.reserve_model) changes.push(`Резервная модель · ${item.after.reserve_model}`);
  if (item.before?.paused !== item.after.paused)
    changes.push(item.after.paused ? 'Приостановлены новые запуски агентов' : 'Ручная пауза запусков снята');
  return changes.join('. ') || 'Изменены настройки';
}

export function AdminModelHistory() {
  const [page, setPage] = useState(1);
  const load = useCallback((signal: AbortSignal) => adminApi.modelHistory(page, signal), [page]);
  const state = useAdminPolling(load);
  return <section className="admin-section admin-models">
    <div className="admin-model-header"><h1>История изменений</h1><a href="#admin/models">К моделям</a></div>
    <LoadState state={state}>{(data) => data.items.length ? <>
      <table className="admin-table admin-model-history"><thead><tr>
        <th scope="col">Дата и время</th><th scope="col">Кто</th><th scope="col">Изменение</th>
      </tr></thead><tbody>{data.items.map((entry) => <tr key={entry.id}>
        <td data-label="Дата и время">{formatTime(entry.created_at)}</td>
        <td data-label="Кто">{entry.actor ?? 'Система'}</td><td data-label="Изменение">{auditText(entry)}</td>
      </tr>)}</tbody></table>
      {data.total > data.page_size && <Pagination page={data.page} size={data.page_size} total={data.total} onChange={setPage} />}
    </> : <p>Изменений пока нет.</p>}</LoadState>
    {state.status === 'error' && <button className="btn secondary" type="button" onClick={state.refresh}>Обновить</button>}
  </section>;
}
