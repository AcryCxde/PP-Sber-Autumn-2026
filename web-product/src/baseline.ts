export const BASELINE_ACTIONS = {
  frame: "Сформулировать задачу и критерии результата",
  find: "Найти источники",
  select: "Отобрать источники",
  read: "Найти нужную информацию внутри источника",
  synthesize: "Сопоставить и объединить информацию",
  verify: "Проверить достоверность и выводы",
  consult: "Обратиться к специалисту",
  assemble: "Оформить результат",
  other: "Другое действие",
};
export const BASELINE_TOOLS = {
  search: "Поиск / сайты",
  documents: "Документы / заметки",
  spreadsheet: "Таблицы",
  ai: "Другой AI-инструмент",
  specialist: "Специалист / коллега",
  other: "Другой инструмент",
};
export const BASELINE_BARRIERS = {
  none: "Затруднений не было",
  framing: "Сложно сформулировать задачу",
  sources: "Сложно найти или отобрать источники",
  trust: "Неясно, чему доверять",
  synthesis: "Сложно объединить информацию",
  expertise: "Не хватает экспертизы",
  waiting: "Пришлось ждать",
  technical: "Техническая проблема",
  other: "Другое затруднение",
  unknown: "Не отмечено",
};
export const BASELINE_OUTCOMES = {
  progress: "Продвинулся в задаче",
  sources: "Нашёл / отобрал источники",
  draft: "Получил черновик",
  result: "Получил нужный результат",
  blocked: "Не смог продолжить",
  unfinished: "Шаг не завершён",
};
export const RESULT_KINDS = {
  document: "Документ / аналитическая записка",
  table: "Таблица / сравнение",
  presentation: "Презентация",
  decision: "Зафиксированное решение / план",
  other: "Другой сохранённый результат",
};
export type BaselineStep = {
  id: string;
  action: keyof typeof BASELINE_ACTIONS;
  tool: keyof typeof BASELINE_TOOLS;
  startedAt: string;
  endedAt?: string;
  barrier?: keyof typeof BASELINE_BARRIERS;
  outcome?: keyof typeof BASELINE_OUTCOMES;
};
export type ResultKind = keyof typeof RESULT_KINDS;
export function readBaselineSteps(value: unknown): BaselineStep[] {
  if (value === undefined) return []; // Compatible with previously stored sessions.
  if (!Array.isArray(value)) throw Error("baseline steps");
  const steps = value.map((x) => {
    if (
      !x ||
      typeof x.id !== "string" ||
      !/^[a-zA-Z0-9_.:-]{1,128}$/.test(x.id) ||
      !Object.hasOwn(BASELINE_ACTIONS, x.action) ||
      !Object.hasOwn(BASELINE_TOOLS, x.tool) ||
      typeof x.startedAt !== "string" ||
      !Number.isFinite(Date.parse(x.startedAt)) ||
      (x.endedAt !== undefined &&
        (typeof x.endedAt !== "string" ||
          !Number.isFinite(Date.parse(x.endedAt)) ||
          Date.parse(x.endedAt) < Date.parse(x.startedAt))) ||
      (x.barrier !== undefined &&
        !Object.hasOwn(BASELINE_BARRIERS, x.barrier)) ||
      (x.outcome !== undefined && !Object.hasOwn(BASELINE_OUTCOMES, x.outcome))
    )
      throw Error("baseline step");
    return {
      id: x.id,
      action: x.action,
      tool: x.tool,
      startedAt: x.startedAt,
      endedAt: x.endedAt,
      barrier: x.barrier,
      outcome: x.outcome,
    } as BaselineStep;
  });
  if (
    new Set(steps.map((s) => s.id)).size !== steps.length ||
    steps.filter((s) => !s.endedAt).length > 1 ||
    steps.some(
      (s, i) =>
        i > 0 &&
        (!steps[i - 1].endedAt ||
          Date.parse(s.startedAt) < Date.parse(steps[i - 1].endedAt!)),
    )
  )
    throw Error("overlapping steps");
  return steps;
}
