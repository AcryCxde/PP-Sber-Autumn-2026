import { useEffect, useState, useSyncExternalStore } from "react";
import "./journey.css";
import {
  JourneyRecorder,
  compare,
  csv,
  exportStudy,
  summary,
  type Mode,
  type Phase,
  type Ratings,
  type Session,
} from "./journey.ts";

const PHASES = {
  baseline: "Привычный способ",
  prototype: "Через CoreTeams",
  interface: "Только интерфейс",
};
const MODES: Record<Mode, string> = {
  active: "Работаю",
  wait: "Жду",
  pause: "Пауза",
  unobserved: "Не наблюдалось",
};
const RATING_LABELS = {
  usefulness: "Полезность",
  applicability: "Применимость",
  clarity: "Понятность",
  verifiability: "Проверяемость источников",
};
const seconds = (n: number | null | undefined) =>
  n === null || n === undefined ? "Не измерено" : `${n.toFixed(1)} с`;
function download(content: string, extension: "json" | "csv") {
  const url = URL.createObjectURL(
    new Blob([content], {
      type:
        extension === "json" ? "application/json" : "text/csv;charset=utf-8",
    }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = `coreteams-study-${new Date().toISOString().slice(0, 10)}.${extension}`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export function JourneyPanel({
  recorder,
  configured,
  projectId,
  hasRun,
  sample,
  savedArtifact,
}: {
  recorder: JourneyRecorder;
  configured: boolean;
  projectId?: string;
  hasRun: boolean;
  sample: boolean;
  savedArtifact?: { artifactId: string; runId: string };
}) {
  const state = useSyncExternalStore(recorder.subscribe, recorder.getSnapshot);
  const active = state.sessions.find((s) => s.open);
  const [open, setOpen] = useState(false),
    [participant, setParticipant] = useState(""),
    [task, setTask] = useState("");
  const [phase, setPhase] = useState<Phase>("baseline"),
    [consent, setConsent] = useState(false);
  const [ratings, setRatings] = useState<Ratings>({}),
    [outcome, setOutcome] = useState<"completed" | "incomplete" | "abandoned">(
      "incomplete",
    );
  const [baselineId, setBaselineId] = useState(""),
    [prototypeId, setPrototypeId] = useState(""),
    [comparable, setComparable] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    setRatings({});
    setOutcome("incomplete");
  }, [active?.id]);
  const pair = compare(
    state.sessions.find((s) => s.id === baselineId),
    state.sessions.find((s) => s.id === prototypeId),
    comparable,
  );
  const disabled = Boolean(state.issue);
  const prototypeBlocked =
    phase === "prototype" && (!configured || hasRun || sample);
  function start() {
    setError("");
    if (prototypeBlocked) return;
    const ok = recorder.start({
      participantCode: participant,
      taskCode: task,
      phase,
      consent,
      buildVersion: import.meta.env.VITE_BUILD_VERSION || "0.2.0",
      environment:
        import.meta.env.VITE_RESEARCH_ENV === "fixture"
          ? "fixture"
          : ["localhost", "127.0.0.1"].includes(location.hostname)
            ? "local"
            : "deployment",
    });
    if (!ok) {
      setError(
        "Не удалось начать. Нужны коды вида U001 / T001 и отдельное согласие участника.",
      );
      return;
    }
    if (phase !== "baseline" && projectId && !sample)
      recorder.record("project_reopened", { projectId });
    setConsent(false);
  }
  function finish() {
    if (
      outcome === "completed" &&
      active &&
      !active.events.some(
        (e) =>
          e.name ===
          (active.phase === "baseline"
            ? "baseline_result_saved"
            : "task_result_selected"),
      ) &&
      active.phase !== "interface"
    ) {
      setError(
        "Для завершения с результатом нужна отметка сохранения. Иначе выберите «Не завершена».",
      );
      return;
    }
    if (recorder.finish(outcome, ratings)) setError("");
  }
  function selection(
    list: Session[],
    value: string,
    set: (id: string) => void,
    label: string,
  ) {
    return (
      <label>
        {label}
        <select
          aria-label={label}
          value={value}
          onChange={(e) => {
            set(e.target.value);
            setComparable(false);
          }}
        >
          <option value="">Выберите сессию</option>
          {list.map((s) => (
            <option value={s.id} key={s.id}>
              {s.participantCode} / {s.taskCode} · {s.startedAt} ·{" "}
              {s.environment}
            </option>
          ))}
        </select>
      </label>
    );
  }
  return (
    <section
      className="journey-section"
      aria-label="Исследование пользовательского пути"
    >
      <button
        className="secondary journey-toggle"
        aria-expanded={open}
        aria-controls="journey-panel"
        onClick={() => setOpen(!open)}
      >
        Замер пути{active ? ` · ${MODES[active.mode]}` : ""}
        {state.issue ? " · сбор остановлен" : ""}
      </button>
      {open ? (
        <div id="journey-panel" className="journey-panel">
          <h2>Замер пользовательского пути</h2>
          <p>
            Только добровольное исследование. Данные сохраняются в этом
            браузере, не отправляются в аналитику или AI-команде. Коды — не имя,
            почта или телефон. Тексты задач, ответов и файлы в журнал не
            попадают. Экспорт храните в согласованном приватном месте и удалите
            по согласованному сроку; очистка браузера удалит локальную копию.
          </p>
          {state.issue ? (
            <p role="alert" className="note">
              {state.issue}
            </p>
          ) : null}
          {error ? <p role="alert">{error}</p> : null}
          {!active ? (
            <div className="journey-grid">
              <label>
                Код участника
                <input
                  aria-label="Код участника"
                  value={participant}
                  onChange={(e) => setParticipant(e.target.value.toUpperCase())}
                  placeholder="U001"
                  pattern="[A-Z][0-9]{2,6}"
                  maxLength={7}
                />
              </label>
              <label>
                Код задачи
                <input
                  aria-label="Код задачи"
                  value={task}
                  onChange={(e) => setTask(e.target.value.toUpperCase())}
                  placeholder="T001"
                  pattern="[A-Z][0-9]{2,6}"
                  maxLength={7}
                />
              </label>
              <label>
                Способ решения
                <select
                  aria-label="Способ решения"
                  value={phase}
                  onChange={(e) => setPhase(e.target.value as Phase)}
                >
                  {Object.entries(PHASES).map(([id, name]) => (
                    <option key={id} value={id}>
                      {name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="journey-check">
                <input
                  type="checkbox"
                  checked={consent}
                  onChange={(e) => setConsent(e.target.checked)}
                />
                Участник согласился на этот замер и локальное хранение
                метаданных; цель, добровольность, срок хранения и право
                остановиться объяснены
              </label>
              <p className="muted">
                Нажмите «Начать замер» непосредственно перед первым действием.
                Для сравнения используйте одну реальную задачу и одинаковые
                коды. Прогон CoreTeams начинайте с нового проекта или проекта
                без запуска. В учебном примере измеряется только интерфейс.
              </p>
              {prototypeBlocked ? (
                <p className="note">
                  Прогон CoreTeams пока нельзя начать: настройте сервис и
                  выберите проект без запуска (не учебный пример). Проверку
                  интерфейса и baseline можно провести отдельно.
                </p>
              ) : null}
              <button
                className="primary"
                disabled={
                  disabled ||
                  !consent ||
                  prototypeBlocked ||
                  !/^[A-Z][0-9]{2,6}$/.test(participant) ||
                  !/^[A-Z][0-9]{2,6}$/.test(task)
                }
                onClick={start}
              >
                Начать замер
              </button>
            </div>
          ) : (
            <div>
              <h3>
                {PHASES[active.phase]} · {active.participantCode} /{" "}
                {active.taskCode}
              </h3>
              <p role="status">
                Состояние замера: {MODES[active.mode]}. Режим выбирает
                наблюдатель. Запрос и работа сервиса переключают активный режим
                в ожидание; пауза не снимается автоматически. После перезагрузки
                продолжите вручную — пропуск отмечается как ненаблюдаемый.
              </p>
              <div className="actions">
                {(["active", "wait", "pause"] as Mode[]).map((mode) => (
                  <button
                    className="secondary"
                    aria-pressed={active.mode === mode}
                    disabled={disabled}
                    key={mode}
                    onClick={() => recorder.mode(mode)}
                  >
                    {MODES[mode]}
                  </button>
                ))}
              </div>
              <dl className="journey-totals">
                {Object.entries(MODES).map(([mode, label]) => (
                  <div key={mode}>
                    <dt>{label}</dt>
                    <dd>
                      {seconds(summary(active)[`${mode}_s` as "active_s"])}
                    </dd>
                  </div>
                ))}
              </dl>
              <p className="muted">
                Показаны интервалы до последней контрольной точки (каждые 5
                секунд). При завершении текущий интервал сохраняется целиком.
                Неактивность мыши не считается паузой автоматически.
              </p>
              <div className="actions">
                <button
                  className="secondary"
                  disabled={disabled}
                  onClick={() =>
                    recorder.record("help_observed", {}, undefined, true)
                  }
                >
                  Отметить помощь
                </button>
                <button
                  className="secondary"
                  disabled={disabled}
                  onClick={() =>
                    recorder.record("error_observed", {}, undefined, true)
                  }
                >
                  Отметить затруднение
                </button>
                <button
                  className="secondary"
                  disabled={disabled}
                  onClick={() =>
                    recorder.record("rework_observed", {}, undefined, true)
                  }
                >
                  Отметить ручную доработку
                </button>
                {active.phase === "baseline" ? (
                  <button
                    className="secondary"
                    disabled={disabled}
                    onClick={() =>
                      recorder.record(
                        "baseline_result_saved",
                        {},
                        "baseline-save",
                        true,
                      )
                    }
                  >
                    Результат baseline сохранён
                  </button>
                ) : null}
                {active.phase === "prototype" ? (
                  <button
                    className="secondary"
                    disabled={
                      disabled ||
                      sample ||
                      !savedArtifact ||
                      !active.events.some(
                        (e) =>
                          e.name === "artifact_saved" &&
                          e.artifactId === savedArtifact?.artifactId,
                      )
                    }
                    onClick={() =>
                      savedArtifact &&
                      recorder.record(
                        "task_result_selected",
                        { projectId, ...savedArtifact },
                        "task-result",
                      )
                    }
                  >
                    Этот артефакт — результат задачи
                  </button>
                ) : null}
              </div>
              <p>
                Отмечено: помощь {summary(active).help_requests_n}; затруднения{" "}
                {summary(active).errors_n}; доработки {summary(active).rework_n}
                . Baseline-сохранение — отметка наблюдателя, а не серверное
                подтверждение.
              </p>
              {active.phase === "prototype" ? (
                <p className="muted">
                  Откройте сохранённую версию справа и отметьте именно результат
                  задачи, а не промежуточный документ. Время берётся из первого
                  наблюдаемого подтверждения сохранения выбранной версии. Выбор
                  фиксируется один раз на сессию.
                </p>
              ) : null}
              <div className="journey-grid">
                {Object.entries(RATING_LABELS).map(([key, label]) => (
                  <label key={key}>
                    {label}
                    <select
                      aria-label={label}
                      value={ratings[key as keyof Ratings] || ""}
                      onChange={(e) =>
                        setRatings({
                          ...ratings,
                          [key]: e.target.value
                            ? Number(e.target.value)
                            : undefined,
                        })
                      }
                    >
                      <option value="">Не измерено</option>
                      {[1, 2, 3, 4, 5].map((n) => (
                        <option key={n} value={n}>
                          {n}
                          {n === 1
                            ? " — совсем нет"
                            : n === 5
                              ? " — полностью"
                              : ""}
                        </option>
                      ))}
                    </select>
                  </label>
                ))}
              </div>
              <label>
                Итог задачи
                <select
                  aria-label="Итог задачи"
                  value={outcome}
                  onChange={(e) => setOutcome(e.target.value as typeof outcome)}
                >
                  <option value="incomplete">Не завершена</option>
                  <option value="completed">Завершена с результатом</option>
                  <option value="abandoned">Участник прекратил попытку</option>
                </select>
              </label>
              <button className="primary" disabled={disabled} onClick={finish}>
                Завершить замер
              </button>
              <button
                className="text-btn"
                disabled={disabled}
                onClick={() => {
                  if (
                    confirm(
                      "Остановить сбор и удалить текущую исследовательскую сессию? Проект и файлы останутся.",
                    )
                  )
                    recorder.remove(active.id);
                }}
              >
                Отозвать согласие и удалить замер
              </button>
            </div>
          )}
          <details>
            <summary>
              Сохранённые сессии и сравнение ({state.sessions.length})
            </summary>
            {state.sessions.length ? (
              <>
                <div className="journey-table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Участник / задача</th>
                        <th>Способ</th>
                        <th>Итог</th>
                        <th>До сохранения</th>
                        <th>Пропуск наблюдения</th>
                        <th>Управление</th>
                      </tr>
                    </thead>
                    <tbody>
                      {state.sessions.map((s) => (
                        <tr key={s.id}>
                          <td>
                            {s.participantCode} / {s.taskCode}
                          </td>
                          <td>
                            {PHASES[s.phase]}
                            {s.environment === "fixture"
                              ? " · тестовая заглушка"
                              : ""}
                          </td>
                          <td>{s.open ? "Идёт" : s.outcome}</td>
                          <td>{seconds(summary(s).elapsed_to_saved_s)}</td>
                          <td>{seconds(summary(s).unobserved_s)}</td>
                          <td>
                            <button
                              className="text-btn"
                              disabled={disabled}
                              onClick={() => {
                                if (
                                  confirm(
                                    "Удалить эту исследовательскую сессию из браузера? Это необратимо; выгрузите нужную копию заранее.",
                                  )
                                )
                                  recorder.remove(s.id);
                              }}
                            >
                              Удалить сессию
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {selection(
                  state.sessions.filter((s) => s.phase === "baseline"),
                  baselineId,
                  setBaselineId,
                  "Baseline для сравнения",
                )}
                {selection(
                  state.sessions.filter((s) => s.phase === "prototype"),
                  prototypeId,
                  setPrototypeId,
                  "Прогон CoreTeams для сравнения",
                )}
                <label className="journey-check">
                  <input
                    type="checkbox"
                    checked={comparable}
                    onChange={(e) => setComparable(e.target.checked)}
                  />
                  Подтверждаю одинаковую задачу, критерии результата и
                  сопоставимые инструменты/условия. Учитываю, что повторное
                  решение даёт эффект обучения.
                </label>
                {pair.blocked.length ? (
                  <ul>
                    {pair.blocked.map((b) => (
                      <li key={b}>{b}</li>
                    ))}
                  </ul>
                ) : (
                  <pre className="journey-delta">
                    {JSON.stringify(pair.delta, null, 2)}
                  </pre>
                )}
                <p className="muted">
                  Разница = CoreTeams − baseline: отрицательное время означает
                  меньшую длительность в этой паре. Это описание наблюдения, не
                  доказательство причинной экономии времени. Незавершённые
                  попытки сохраняются, а не исключаются из исследования.
                </p>
                <div className="actions">
                  <button
                    className="secondary"
                    disabled={disabled}
                    onClick={() => {
                      recorder.checkpoint();
                      download(
                        JSON.stringify(
                          exportStudy(recorder.getSnapshot().sessions, pair),
                          null,
                          2,
                        ),
                        "json",
                      );
                    }}
                  >
                    Выгрузить JSON замеров
                  </button>
                  <button
                    className="secondary"
                    disabled={disabled}
                    onClick={() => {
                      recorder.checkpoint();
                      download(csv(recorder.getSnapshot().sessions), "csv");
                    }}
                  >
                    Выгрузить CSV замеров
                  </button>
                </div>
              </>
            ) : (
              <p>
                Сессий пока нет. Ничего не собирается до согласия и явного
                старта.
              </p>
            )}
          </details>
        </div>
      ) : null}
    </section>
  );
}
