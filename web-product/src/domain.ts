export const IDENTITY =
  "Рабочее пространство с AI-командой для сложных задач вне одной экспертизы";
export const KEY = "coreteams-product-workspace:v2";
export type Role = {
  id: string;
  name: string;
  purpose: string;
  output: string;
};
export const ROLES: Role[] = [
  {
    id: "researcher",
    name: "Исследователь",
    purpose: "Найти источники и проверить, что известно",
    output: "Источники, свидетельства и открытые вопросы",
  },
  {
    id: "analyst",
    name: "Аналитик",
    purpose: "Сопоставить варианты и ограничения",
    output: "Сравнение вариантов с основаниями",
  },
  {
    id: "specialist",
    name: "Предметный специалист",
    purpose: "Проверить решение с другой точки зрения",
    output: "Замечания к рискам и допущениям",
  },
  {
    id: "editor",
    name: "Редактор",
    purpose: "Собрать выводы в понятный результат",
    output: "Документ для следующего шага",
  },
];
export const QUESTIONS = [
  "Какое решение вы хотите принять или что получить?",
  "Для кого нужен результат и что уже известно?",
  "Какие ограничения и критерии успеха важны?",
];
export type Stage = { id: string; title: string; goal: string };
export type InputFile = {
  id: string;
  name: string;
  size: number;
  type: string;
  sha256: string;
  uploadedId?: string;
};
export type Message = {
  id: string;
  role: "user" | "facilitator" | "system";
  text: string;
  at: string;
};
export type Artifact = {
  id: string;
  documentId: string;
  stageId: string;
  version: number;
  title: string;
  content: string;
  saved: boolean;
  savedAt?: string;
  approved?: boolean;
};
export type Usage = {
  inputTokens?: number;
  outputTokens?: number;
  requests?: number;
  computeSeconds?: number;
  cost?: number;
  currency?: string;
};
export type Run = {
  id: string;
  revision: number;
  status: "running" | "clarification" | "review" | "completed" | "failed";
  stageId: string;
  question?: { id: string; text: string };
  messages: Message[];
  artifacts: Artifact[];
  usage?: Usage;
};
export type Pending = {
  action: "start" | "answer" | "message" | "approve";
  id: string;
  body: Record<string, unknown>;
  targetId?: string;
};
export type Project = {
  id: string;
  title: string;
  goal: string;
  context: string;
  scenario: string;
  answers: string[];
  team: string[];
  teamNote: string;
  stages: Stage[];
  files: InputFile[];
  createdAt: string;
  updatedAt: string;
  step: number;
  serverId?: string;
  run?: Run;
  pending?: Pending;
  draftMessage: string;
  answerDraft?: { questionId: string; text: string };
};
export function newProject(): Project {
  const now = new Date().toISOString();
  return {
    id: crypto.randomUUID(),
    title: "",
    goal: "",
    context: "",
    scenario: "other",
    answers: ["", "", ""],
    team: ["researcher", "analyst", "editor"],
    teamNote: "",
    stages: [
      {
        id: "understand",
        title: "Разбор задачи",
        goal: "Факты, ограничения и открытые вопросы",
      },
      {
        id: "options",
        title: "Варианты решения",
        goal: "Варианты, источники и основания выбора",
      },
      {
        id: "result",
        title: "Итоговый результат",
        goal: "Решение, ограничения и следующий шаг",
      },
    ],
    files: [],
    createdAt: now,
    updatedAt: now,
    step: 0,
    draftMessage: "",
  };
}
const obj = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);
const str = (v: unknown): v is string => typeof v === "string";
const nonempty = (v: unknown): v is string => str(v) && v.trim().length > 0;
const count = (v: unknown) =>
  typeof v === "number" && Number.isFinite(v) && v >= 0;
export function parseRun(v: unknown): Run {
  if (
    !obj(v) ||
    !nonempty(v.id) ||
    !Number.isInteger(v.revision) ||
    Number(v.revision) < 0 ||
    !["running", "clarification", "review", "completed", "failed"].includes(
      String(v.status),
    ) ||
    !nonempty(v.stageId) ||
    !Array.isArray(v.messages) ||
    !Array.isArray(v.artifacts)
  )
    throw new Error("Несовместимый ответ сервиса");
  if (
    v.status === "clarification" &&
    (!obj(v.question) || !nonempty(v.question.id) || !nonempty(v.question.text))
  )
    throw new Error("Нет вопроса фасилитатора");
  const messages = v.messages.map((m: unknown) => {
    if (
      !obj(m) ||
      !nonempty(m.id) ||
      !["user", "facilitator", "system"].includes(String(m.role)) ||
      !str(m.text) ||
      !str(m.at) ||
      !Number.isFinite(Date.parse(m.at))
    )
      throw new Error("Некорректное сообщение");
    return m as Message;
  });
  const artifacts = v.artifacts.map((a: unknown) => {
    if (
      !obj(a) ||
      !nonempty(a.id) ||
      !nonempty(a.stageId) ||
      !nonempty(a.documentId) ||
      !Number.isInteger(a.version) ||
      Number(a.version) < 1 ||
      !nonempty(a.title) ||
      !str(a.content) ||
      typeof a.saved !== "boolean" ||
      (a.approved !== undefined && typeof a.approved !== "boolean") ||
      (a.saved && (!str(a.savedAt) || !Number.isFinite(Date.parse(a.savedAt))))
    )
      throw new Error("Некорректная версия артефакта");
    return a as Artifact;
  });
  if (
    new Set(artifacts.map((a) => a.id)).size !== artifacts.length ||
    new Set(
      artifacts.map(
        (a) => `${a.stageId}\u0000${a.documentId}\u0000${a.version}`,
      ),
    ).size !== artifacts.length ||
    new Set(messages.map((m) => m.id)).size !== messages.length
  )
    throw new Error("Дубли в ответе сервиса");
  let usage: Usage | undefined;
  if (v.usage !== undefined) {
    if (!obj(v.usage)) throw new Error("Некорректные замеры");
    usage = {};
    for (const field of [
      "inputTokens",
      "outputTokens",
      "requests",
      "computeSeconds",
      "cost",
    ] as const) {
      if (v.usage[field] !== undefined) {
        if (!count(v.usage[field])) throw new Error("Некорректный замер");
        if (
          ["inputTokens", "outputTokens", "requests"].includes(field) &&
          !Number.isInteger(v.usage[field])
        )
          throw new Error("Счётчик должен быть целым");
        usage[field] = v.usage[field] as number;
      }
    }
    if (v.usage.currency !== undefined) {
      if (!nonempty(v.usage.currency)) throw new Error("Некорректная валюта");
      usage.currency = v.usage.currency;
    }
    if (usage.cost !== undefined && !usage.currency)
      throw new Error("Затраты без валюты");
  }
  return {
    id: v.id,
    revision: v.revision as number,
    status: v.status as Run["status"],
    stageId: v.stageId,
    ...(v.status === "clarification"
      ? { question: v.question as Run["question"] }
      : {}),
    messages,
    artifacts,
    ...(usage ? { usage } : {}),
  };
}
export function mergeRun(p: Project, run: Run): Project {
  if (
    !p.stages.some((s) => s.id === run.stageId) ||
    run.artifacts.some((a) => !p.stages.some((s) => s.id === a.stageId))
  )
    throw new Error("Сервис вернул неизвестный этап");
  if (p.run && p.run.id !== run.id)
    throw new Error("Ответ относится к другому запуску");
  if (p.run && run.revision < p.run.revision) return p;
  const artifacts = new Map(p.run?.artifacts.map((a) => [a.id, a]) || []);
  for (const a of run.artifacts) {
    const old = artifacts.get(a.id);
    if (
      old &&
      (old.documentId !== a.documentId ||
        old.version !== a.version ||
        old.stageId !== a.stageId ||
        old.content !== a.content ||
        old.title !== a.title)
    )
      throw new Error("Сервис попытался изменить уже существующую версию");
    artifacts.set(a.id, { ...old, ...a });
  }
  const messages = new Map(p.run?.messages.map((m) => [m.id, m]) || []);
  for (const m of run.messages) {
    const old = messages.get(m.id);
    if (old && (old.text !== m.text || old.role !== m.role))
      throw new Error("Сервис изменил сообщение в истории");
    messages.set(m.id, m);
  }
  return {
    ...p,
    run: {
      ...run,
      artifacts: [...artifacts.values()],
      messages: [...messages.values()],
    },
    updatedAt: new Date().toISOString(),
  };
}
export function latest(artifacts: Artifact[], stageId: string) {
  return artifacts
    .filter((a) => a.stageId === stageId)
    .sort((a, b) => b.version - a.version)[0];
}
export function readProjects(value: string | null): Project[] {
  if (value === null) return [];
  const data: unknown = JSON.parse(value);
  if (!obj(data) || data.version !== 2 || !Array.isArray(data.projects))
    throw new Error("Неизвестный формат черновиков");
  return data.projects.map((v: unknown) => {
    if (
      !obj(v) ||
      !nonempty(v.id) ||
      !str(v.title) ||
      !str(v.goal) ||
      !str(v.context) ||
      !str(v.scenario) ||
      !Array.isArray(v.answers) ||
      v.answers.length !== 3 ||
      !v.answers.every(str) ||
      !Array.isArray(v.team) ||
      !v.team.every(str) ||
      !str(v.teamNote) ||
      !Array.isArray(v.stages) ||
      v.stages.length === 0 ||
      !Array.isArray(v.files) ||
      !Number.isInteger(v.step) ||
      Number(v.step) < 0 ||
      Number(v.step) > 4 ||
      !str(v.createdAt) ||
      !str(v.updatedAt) ||
      !str(v.draftMessage)
    )
      throw new Error("Некорректный черновик");
    if (
      v.stages.some(
        (s: unknown) =>
          !obj(s) || !nonempty(s.id) || !str(s.title) || !str(s.goal),
      ) ||
      new Set(v.stages.map((s: any) => s.id)).size !== v.stages.length
    )
      throw new Error("Некорректный план");
    if (
      v.files.some(
        (f: unknown) =>
          !obj(f) ||
          !nonempty(f.id) ||
          !str(f.name) ||
          !count(f.size) ||
          !str(f.type) ||
          !str(f.sha256) ||
          (f.uploadedId !== undefined && !nonempty(f.uploadedId)),
      )
    )
      throw new Error("Некорректные материалы");
    if (v.serverId !== undefined && !nonempty(v.serverId))
      throw new Error("Некорректный ID проекта");
    if (
      v.pending !== undefined &&
      (!obj(v.pending) ||
        !nonempty(v.pending.id) ||
        !["start", "answer", "message", "approve"].includes(
          String(v.pending.action),
        ) ||
        !obj(v.pending.body) ||
        (v.pending.targetId !== undefined && !str(v.pending.targetId)))
    )
      throw new Error("Некорректный запрос");
    if (
      v.answerDraft !== undefined &&
      (!obj(v.answerDraft) ||
        !nonempty(v.answerDraft.questionId) ||
        !str(v.answerDraft.text))
    )
      throw new Error("Некорректный черновик ответа");
    const p = v as Project;
    if (p.run) p.run = parseRun(p.run);
    return p;
  });
}
export function projectPayload(p: Project) {
  return {
    clientProjectId: p.id,
    title: p.title,
    goal: p.goal,
    context: p.context,
    scenario: p.scenario,
    answers: p.answers,
    team: p.team,
    teamNote: p.teamNote,
    stages: p.stages,
  };
}
export function exampleProject(): Project {
  const p = newProject();
  p.id = "example-coffee";
  p.title = "Стоит ли открыть кофейню у университета?";
  p.goal = "Выбрать первый шаг проверки идеи до аренды помещения";
  p.context =
    "Учебный пример: небольшая команда, ограниченный бюджет. Пока нет подтверждённых данных о спросе.";
  p.answers = [
    "Понять, что проверить до вложений",
    "Будущие посетители университета",
    "Не арендовать помещение до проверки спроса",
  ];
  p.step = 4;
  p.run = {
    id: "example-run",
    revision: 2,
    status: "review",
    stageId: "options",
    usage: undefined,
    messages: [
      {
        id: "e1",
        role: "user",
        text: "Хочу открыть кофейню, но сначала проверить, нужна ли она рядом с университетом.",
        at: "2026-10-01T10:00:00Z",
      },
      {
        id: "e2",
        role: "facilitator",
        text: "В этом учебном примере сначала отделим факты от предположений. Какое решение вы пока не хотите принимать?",
        at: "2026-10-01T10:01:00Z",
      },
      {
        id: "e3",
        role: "user",
        text: "Не хочу подписывать аренду, пока не понятен спрос.",
        at: "2026-10-01T10:02:00Z",
      },
      {
        id: "e4",
        role: "facilitator",
        text: "Откройте «Варианты решения» слева. Справа можно сравнить версии артефакта и написать замечание. Все данные здесь учебные, CTF не запускался.",
        at: "2026-10-01T10:03:00Z",
      },
    ],
    artifacts: [
      {
        id: "e-a1",
        documentId: "map",
        stageId: "understand",
        version: 1,
        title: "Карта исходных данных",
        content:
          "# Что известно\nИдея кофейни рядом с университетом. Есть ограничение по бюджету.\n# Что предстоит проверить\n- Поток посетителей и время визитов\n- Существующие альтернативы\n- Причины выбора места\n# Граница примера\nЭто учебный документ. Интервью и поиск источников не проводились.",
        saved: false,
        approved: true,
      },
      {
        id: "e-a2",
        documentId: "plan",
        stageId: "options",
        version: 1,
        title: "План проверки спроса",
        content:
          "# Первый вариант\nСначала поговорить с потенциальными посетителями.\n# Открытый вопрос\nКак отличить интерес к идее от готовности покупать?\n# Статус\nУчебная версия v1, не вывод CTF.",
        saved: false,
      },
      {
        id: "e-a3",
        documentId: "plan",
        stageId: "options",
        version: 2,
        title: "План проверки спроса",
        content:
          "# Проверка до аренды\n1. Сформулировать проверяемую гипотезу.\n2. Спросить о последних реальных покупках кофе.\n3. Подготовить небольшой тест с реальным предложением после согласования.\n# Критерий решения\nЧисловой порог нужно согласовать до теста.\n# Источники и ограничения\nЭто пример структуры. Реальные ответы, цены и подтверждённые источники отсутствуют.",
        saved: false,
      },
    ],
  };
  p.run.artifacts.push({
    id: "e-a4",
    documentId: "interview",
    stageId: "options",
    version: 1,
    title: "Вопросы для проверки",
    content:
      "# Пример вопросов\n- Расскажите о последней покупке кофе рядом с университетом.\n- Как вы выбирали место и что мешало?\n- Каким способом вы решили бы эту задачу сейчас?\n# Граница\nЭто учебный список. Интервью ещё не проводились.",
    saved: false,
  });
  return p;
}
