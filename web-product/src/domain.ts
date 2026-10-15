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
  status: "queued" | "running" | "clarification" | "waiting_for_input" | "review" | "completed" | "failed" | "outcome_unknown";
  stageId: string;
  question?: QuestionGroup;
  progress?: { stage: "interrupted" | "recovering" | "working"; attempt?: number; context_restored?: boolean };
  parentRunId?: string;
  sourceArtifactId?: string;
  messages: Message[];
  artifacts: Artifact[];
  roleEvents?: RoleEvent[];
  usage?: Usage;
};
export type RoleEvent = { id: string; roleId: string; type: "role.started" | "role.completed" | "role.failed"; at: string };
export type QuestionGroup = { id: string; questions: {
  text: string; header: string | null;
  options: { label: string; description: string | null }[];
  multi_select: boolean; allow_custom: boolean;
}[] };
export type AnswerValue = { selected: string[]; custom: string };
export type Approval = { receiptId: string; artifactId: string; version: number; approved: true; approvedAt: string };
export type Pending = {
  action: "start" | "answer" | "message" | "rework" | "approve";
  id: string;
  body: Record<string, unknown>;
  targetId?: string;
  runId?: string;
  legacy?: boolean;
  rejected?: boolean;
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
  runs?: Run[];
  approvals?: Record<string, Approval>;
  pending?: Pending;
  draftMessage: string;
  answerDraft?: { questionId: string; text?: string; answers?: Record<string, AnswerValue> };
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
export const isWaiting = (run?: Run) => run?.status === "clarification" || run?.status === "waiting_for_input";
export const isTerminal = (run?: Run) => Boolean(run && ["completed", "failed", "outcome_unknown"].includes(run.status));
export const canRework = (run?: Run) => run?.status === "completed";
export function parseQuestion(v: unknown): QuestionGroup {
  if (!obj(v) || !nonempty(v.id) || !Array.isArray(v.questions) || !v.questions.length)
    throw new Error("Нет структурированных вопросов фасилитатора");
  for (const q of v.questions) {
    if (!obj(q) || !nonempty(q.text) || !(q.header === null || str(q.header)) ||
        typeof q.multi_select !== "boolean" || typeof q.allow_custom !== "boolean" || !Array.isArray(q.options) ||
        q.options.some(o => !obj(o) || !nonempty(o.label) || !(o.description === null || str(o.description))) ||
        new Set(q.options.map((o: any) => o.label)).size !== q.options.length ||
        (!q.allow_custom && !q.options.length)) throw new Error("Некорректный вопрос фасилитатора");
  }
  if (new Set(v.questions.map((q: any) => q.text)).size !== v.questions.length)
    throw new Error("Одинаковые тексты вопросов: ответы нельзя передать однозначно");
  return v as QuestionGroup;
}
export function buildAnswers(group: QuestionGroup, draft: Record<string, AnswerValue>): Record<string, string> {
  parseQuestion(group);
  return Object.fromEntries(group.questions.map(q => {
    const value = draft[q.text] || { selected: [], custom: "" };
    const custom = value.custom.trim();
    if (new Set(value.selected).size !== value.selected.length ||
        value.selected.some(v => !q.options.some(o => o.label === v)) ||
        (custom && !q.allow_custom) ||
        (!q.multi_select && value.selected.length + Number(Boolean(custom)) > 1))
      throw new Error("Ответ не соответствует вариантам вопроса");
    const parts = [...value.selected, ...(custom ? [custom] : [])];
    if (!parts.length) throw new Error("Ответьте на каждый вопрос фасилитатора");
    return [q.text, parts.join(", ")];
  }));
}
export function parseApproval(v: unknown, artifact: Artifact): Approval {
  if (!obj(v) || !artifact.saved || !nonempty(v.receiptId) || v.artifactId !== artifact.id ||
      v.version !== artifact.version || v.approved !== true || !str(v.approvedAt) || !Number.isFinite(Date.parse(v.approvedAt)))
    throw new Error("Сервис не подтвердил одобрение выбранной сохранённой версии");
  return v as Approval;
}
export function projectRuns(p: Project): Run[] {
  return [...new Map([...(p.runs || []), ...(p.run ? [p.run] : [])].map(r => [r.id, r])).values()];
}
export function projectArtifacts(p: Project): Artifact[] {
  return [...new Map(projectRuns(p).flatMap(r => r.artifacts).map(a => [a.id, a])).values()];
}
export function projectMessages(p: Project): Message[] {
  return [...new Map(projectRuns(p).flatMap(r => r.messages).map(m => [m.id, m])).values()];
}
export function artifactRunId(p: Project, artifactId: string): string | undefined {
  return projectRuns(p).find(r => r.artifacts.some(a => a.id === artifactId))?.id;
}
export function approved(p: Project, a: Artifact): boolean {
  const receipt = p.approvals?.[a.id];
  return Boolean(a.saved && receipt && receipt.artifactId === a.id && receipt.version === a.version);
}
export function parseRun(v: unknown): Run {
  if (
    !obj(v) ||
    !nonempty(v.id) ||
    !Number.isInteger(v.revision) ||
    Number(v.revision) < 0 ||
    !["queued", "running", "clarification", "waiting_for_input", "review", "completed", "failed", "outcome_unknown"].includes(
      String(v.status),
    ) ||
    !nonempty(v.stageId) ||
    !Array.isArray(v.messages) ||
    !Array.isArray(v.artifacts)
  )
    throw new Error("Несовместимый ответ сервиса");
  const replacingQuestion = obj(v.progress) && ["interrupted", "recovering"].includes(String(v.progress.stage));
  const question = isWaiting(v as Run) && !(replacingQuestion && v.question == null) ? parseQuestion(v.question) : undefined;
  if (v.roleEvents !== undefined && (!Array.isArray(v.roleEvents) || v.roleEvents.some(e =>
    !obj(e) || !nonempty(e.id) || !nonempty(e.roleId) || !["role.started", "role.completed", "role.failed"].includes(String(e.type)) ||
    !str(e.at) || !Number.isFinite(Date.parse(e.at))) || new Set(v.roleEvents.map((e: any) => e.id)).size !== v.roleEvents.length))
    throw new Error("Некорректные события исполнения ролей");
  if (v.progress !== undefined && (!obj(v.progress) || !["interrupted", "recovering", "working"].includes(String(v.progress.stage)) ||
      (v.progress.attempt !== undefined && (!Number.isInteger(v.progress.attempt) || Number(v.progress.attempt) < 1)) ||
      (v.progress.context_restored !== undefined && typeof v.progress.context_restored !== "boolean")))
    throw new Error("Некорректное состояние восстановления");
  if (isTerminal(v as Run) && v.progress && (v.progress as any).stage !== "working")
    throw new Error("Терминальный запуск не может продолжать восстановление");
  for (const field of ["parentRunId", "sourceArtifactId"])
    if (v[field] !== undefined && !nonempty(v[field])) throw new Error("Некорректная связь доработки");
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
      if (v.usage[field] !== undefined && v.usage[field] !== null) {
        if (!count(v.usage[field])) throw new Error("Некорректный замер");
        if (
          ["inputTokens", "outputTokens", "requests"].includes(field) &&
          !Number.isInteger(v.usage[field])
        )
          throw new Error("Счётчик должен быть целым");
        usage[field] = v.usage[field] as number;
      }
    }
    if (v.usage.currency !== undefined && v.usage.currency !== null) {
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
    ...(question ? { question } : {}),
    ...(v.progress ? { progress: v.progress as Run["progress"] } : {}),
    ...(v.parentRunId ? { parentRunId: v.parentRunId as string } : {}),
    ...(v.sourceArtifactId ? { sourceArtifactId: v.sourceArtifactId as string } : {}),
    messages,
    artifacts,
    ...(v.roleEvents ? { roleEvents: v.roleEvents as RoleEvent[] } : {}),
    ...(usage ? { usage } : {}),
  };
}
export function mergeRun(p: Project, run: Run): Project {
  const historical = (p.runs || []).flatMap(r => r.artifacts);
  for (const a of run.artifacts) {
    const existing = historical.find(old => old.id === a.id);
    if (existing) {
      if (JSON.stringify(a) !== JSON.stringify(existing)) throw new Error("Изменилась историческая версия документа");
    } else if (historical.some(old => a.documentId === old.documentId && a.stageId === old.stageId && a.version <= old.version))
      throw new Error("Версия не продолжает историю документа");
  }
  for (const m of run.messages) {
    const existing = (p.runs || []).flatMap(r => r.messages).find(old => old.id === m.id);
    if (existing && JSON.stringify(existing) !== JSON.stringify(m)) throw new Error("Изменено сообщение прежнего запуска");
  }
  if (
    !p.stages.some((s) => s.id === run.stageId) ||
    run.artifacts.some((a) => !p.stages.some((s) => s.id === a.stageId))
  )
    throw new Error("Сервис вернул неизвестный этап");
  if (p.run && p.run.id !== run.id)
    throw new Error("Ответ относится к другому запуску");
  if (p.run && run.revision < p.run.revision) return p;
  if (isTerminal(p.run) && (run.status !== p.run!.status || run.revision !== p.run!.revision))
    throw new Error("Завершённый Run нельзя переоткрыть или дополнять событиями");
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
  const roleEvents = new Map(p.run?.roleEvents?.map(e => [e.id, e]) || []);
  for (const e of run.roleEvents || []) {
    const old = roleEvents.get(e.id);
    if (old && JSON.stringify(old) !== JSON.stringify(e)) throw new Error("Изменено событие исполнения роли");
    roleEvents.set(e.id, e);
  }
  for (const m of run.messages) {
    const old = messages.get(m.id);
    if (old && (old.text !== m.text || old.role !== m.role))
      throw new Error("Сервис изменил сообщение в истории");
    messages.set(m.id, m);
  }
  if (isTerminal(p.run) && (JSON.stringify([...artifacts.values()]) !== JSON.stringify(p.run!.artifacts) ||
      JSON.stringify([...messages.values()]) !== JSON.stringify(p.run!.messages) ||
      run.stageId !== p.run!.stageId || JSON.stringify(run.usage) !== JSON.stringify(p.run!.usage) ||
      JSON.stringify(run.progress) !== JSON.stringify(p.run!.progress) ||
      JSON.stringify([...roleEvents.values()]) !== JSON.stringify(p.run!.roleEvents || []) ||
      run.parentRunId !== p.run!.parentRunId || run.sourceArtifactId !== p.run!.sourceArtifactId))
    throw new Error("История закрытого Run изменилась");
  return {
    ...p,
    run: {
      ...run,
      artifacts: [...artifacts.values()],
      messages: [...messages.values()],
      ...(roleEvents.size ? { roleEvents: [...roleEvents.values()] } : {}),
    },
    updatedAt: new Date().toISOString(),
  };
}
export function mergeRework(p: Project, run: Run, parentRunId: string, sourceArtifactId: string): Project {
  const parent = p.run;
  const source = projectArtifacts(p).find(a => a.id === sourceArtifactId);
  if (!parent || parent.id !== parentRunId || !canRework(parent) || !source?.saved ||
      run.id === parent.id || projectRuns(p).some(r => r.id === run.id) ||
      run.parentRunId !== parentRunId || run.sourceArtifactId !== sourceArtifactId)
    throw new Error("Сервис не подтвердил связь нового Run с доработкой");
  // Validate against ALL closed runs before adding the new current attempt.
  return mergeRun({ ...p, runs: projectRuns(p), run: undefined }, run);
}
export function latest(artifacts: Artifact[], stageId: string) {
  return artifacts
    .filter((a) => a.stageId === stageId)
    .sort((a, b) => b.version - a.version)[0];
}
export function readProjects(value: string | null): Project[] {
  if (value === null) return [];
  const data: unknown = JSON.parse(value);
  if (!obj(data) || (data.version !== 2 && data.version !== 3) || !Array.isArray(data.projects))
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
        !["start", "answer", "message", "rework", "approve"].includes(
          String(v.pending.action),
        ) ||
        !obj(v.pending.body) ||
        (v.pending.targetId !== undefined && !str(v.pending.targetId)) ||
        (v.pending.runId !== undefined && !nonempty(v.pending.runId)) ||
        (v.pending.legacy !== undefined && typeof v.pending.legacy !== "boolean") ||
        (v.pending.rejected !== undefined && typeof v.pending.rejected !== "boolean"))
    )
      throw new Error("Некорректный запрос");
    if (
      v.answerDraft !== undefined &&
      (!obj(v.answerDraft) ||
        !nonempty(v.answerDraft.questionId) ||
        (v.answerDraft.text !== undefined && !str(v.answerDraft.text)) ||
        (v.answerDraft.answers !== undefined && (!obj(v.answerDraft.answers) ||
          Object.values(v.answerDraft.answers).some(a => !obj(a) || !Array.isArray(a.selected) || !a.selected.every(str) || !str(a.custom)))))
    )
      throw new Error("Некорректный черновик ответа");
    const p = v as Project;
    const migrateRun = (r: Run): Run => {
      const q = r.question as any;
      const upgraded = data.version === 2 && q && !q.questions && nonempty(q.text)
        ? { ...r, question: { id: q.id, questions: [{ text: q.text, header: null, options: [], multi_select: false, allow_custom: true }] } } : r;
      return parseRun(upgraded);
    };
    if (p.run) p.run = migrateRun(p.run);
    if (p.runs !== undefined) {
      if (!Array.isArray(p.runs)) throw new Error("Некорректная история запусков");
      p.runs = p.runs.map(migrateRun);
      if (new Set(p.runs.map(r => r.id)).size !== p.runs.length || p.runs.some(r => r.id === p.run?.id)) throw new Error("Дубли запусков");
    }
    for (let i = 0; i < (p.runs?.length || 0); i++)
      mergeRun({ ...p, runs: p.runs!.slice(0, i), run: undefined }, p.runs![i]);
    if (p.run) mergeRun(p, p.run);
    if (data.version === 2 && p.pending) p.pending = { ...p.pending, legacy: true };
    if (p.approvals !== undefined) {
      if (!obj(p.approvals)) throw new Error("Некорректные подтверждения одобрения");
      for (const [id, receipt] of Object.entries(p.approvals)) {
        const a = projectArtifacts(p).find(a => a.id === id);
        if (!a) throw new Error("Неизвестная одобренная версия");
        parseApproval(receipt, a);
      }
    }
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
