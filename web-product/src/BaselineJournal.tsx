import { useState } from "react";
import {
  BASELINE_ACTIONS,
  BASELINE_TOOLS,
  BASELINE_BARRIERS,
  BASELINE_OUTCOMES,
  RESULT_KINDS,
  type BaselineStep,
  type ResultKind,
} from "./baseline.ts";
import type { JourneyRecorder, Session } from "./journey.ts";

export function BaselineJournal({
  recorder,
  session,
  disabled,
}: {
  recorder: JourneyRecorder;
  session: Session;
  disabled: boolean;
}) {
  const [action, setAction] = useState<BaselineStep["action"] | "">("");
  const [tool, setTool] = useState<BaselineStep["tool"] | "">("");
  const [barrier, setBarrier] =
    useState<NonNullable<BaselineStep["barrier"]>>("unknown");
  const [outcome, setOutcome] =
    useState<NonNullable<BaselineStep["outcome"]>>("unfinished");
  const [kind, setKind] = useState<ResultKind | "">("");
  const steps = session.baselineSteps || [],
    current = steps.find((s) => !s.endedAt);
  return (
    <section
      className="baseline-journal"
      aria-label="Журнал привычного способа"
    >
      <h3>Привычный способ — ваши шаги</h3>
      <p className="muted">
        На заполнение дневника ставьте «Паузу», чтобы не смешивать запись замера
        с работой над задачей. Общее время всё равно включает паузы; активное
        время и ожидание анализируются отдельно. Эти отметки — ваш отчёт, а не
        независимая проверка качества результата.
      </p>
      <p>
        Решайте задачу в своих инструментах, а здесь отмечайте шаги. Это ручной
        дневник, не запись экрана и не автоматическая проверка чужих приложений.
        Начало шага включает режим «Работаю»; ожидание и паузы переключайте
        выше.
      </p>
      {!session.resultKind ? (
        <>
          {!current ? (
            <div className="journey-grid">
              <label>
                Что сейчас будете делать?
                <select
                  aria-label="Действие привычного пути"
                  value={action}
                  onChange={(e) => setAction(e.target.value as typeof action)}
                >
                  <option value="">Выберите действие</option>
                  {Object.entries(BASELINE_ACTIONS).map(([key, label]) => (
                    <option key={key} value={key}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Каким инструментом?
                <select
                  aria-label="Инструмент привычного пути"
                  value={tool}
                  onChange={(e) => setTool(e.target.value as typeof tool)}
                >
                  <option value="">Выберите инструмент</option>
                  {Object.entries(BASELINE_TOOLS).map(([key, label]) => (
                    <option key={key} value={key}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              <button
                className="secondary"
                disabled={disabled || !action || !tool}
                onClick={() => {
                  if (
                    action &&
                    tool &&
                    recorder.beginBaselineStep(action, tool)
                  ) {
                    setBarrier("unknown");
                    setOutcome("unfinished");
                  }
                }}
              >
                Начать шаг привычного пути
              </button>
            </div>
          ) : (
            <div>
              <p role="status">
                Шаг {steps.length}: {BASELINE_ACTIONS[current.action]} ·{" "}
                {BASELINE_TOOLS[current.tool]}. Начат{" "}
                {new Date(current.startedAt).toLocaleTimeString("ru")}.
              </p>
              <p>
                Выполните действие привычным способом. Когда закончите,
                вернитесь сюда и отметьте затруднение и итог. Если не удалось
                продолжить — это тоже результат наблюдения.
              </p>
              <div className="journey-grid">
                <label>
                  Что мешало?
                  <select
                    aria-label="Затруднение на шаге"
                    value={barrier}
                    onChange={(e) =>
                      setBarrier(e.target.value as typeof barrier)
                    }
                  >
                    {Object.entries(BASELINE_BARRIERS).map(([key, label]) => (
                      <option key={key} value={key}>
                        {label}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  Что получилось?
                  <select
                    aria-label="Итог шага"
                    value={outcome}
                    onChange={(e) =>
                      setOutcome(e.target.value as typeof outcome)
                    }
                  >
                    {Object.entries(BASELINE_OUTCOMES).map(([key, label]) => (
                      <option key={key} value={key}>
                        {label}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <button
                className="secondary"
                disabled={disabled}
                onClick={() => recorder.endBaselineStep(barrier, outcome)}
              >
                Завершить шаг привычного пути
              </button>
            </div>
          )}
        </>
      ) : (
        <p className="note">
          Привычный результат отмечен: {RESULT_KINDS[session.resultKind]}.
          Теперь оцените результат и завершите замер.
        </p>
      )}
      {steps.length ? (
        <ol className="baseline-steps">
          {steps.map((s, i) => (
            <li key={s.id}>
              <strong>
                Шаг {i + 1}. {BASELINE_ACTIONS[s.action]}
              </strong>
              <span>
                {BASELINE_TOOLS[s.tool]} ·{" "}
                {s.endedAt
                  ? `${((Date.parse(s.endedAt) - Date.parse(s.startedAt)) / 1000).toFixed(1)} с всего`
                  : "Идёт"}
              </span>
              {s.endedAt ? (
                <span>
                  Затруднение: {BASELINE_BARRIERS[s.barrier || "unknown"]}.
                  Итог: {BASELINE_OUTCOMES[s.outcome || "unfinished"]}.
                </span>
              ) : null}
            </li>
          ))}
        </ol>
      ) : (
        <p className="muted">
          Шагов пока нет. Например: найти источники → отобрать → прочитать →
          объединить информацию → проверить → оформить. Отмечайте свой реальный
          порядок, а не обязательно этот пример.
        </p>
      )}
      {!session.resultKind ? (
        <>
          <label>
            Формат сохранённого результата
            <select
              aria-label="Формат результата привычного способа"
              value={kind}
              onChange={(e) => setKind(e.target.value as typeof kind)}
            >
              <option value="">Выберите после сохранения</option>
              {Object.entries(RESULT_KINDS).map(([key, label]) => (
                <option key={key} value={key}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <p className="muted">
            Сам результат сохраните там, где обычно работаете. Здесь фиксируется
            только формат и ваше подтверждение, не содержимое и не
            доказательство качества. Для оценки покажите обезличенный результат
            исследователю отдельно, только если согласны.
          </p>
          <button
            className="secondary"
            disabled={disabled || !kind || !steps.length || Boolean(current)}
            onClick={() => kind && recorder.saveBaselineResult(kind)}
          >
            Результат baseline сохранён
          </button>
        </>
      ) : null}
    </section>
  );
}
