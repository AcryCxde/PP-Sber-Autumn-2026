export type Persona = "owner" | "implementer";
export type Stage = "start" | "questions" | "team" | "work" | "result";
export type OutputType = "document" | "skill";
export type Format = "pdf" | "docx" | "pptx" | "xlsx" | "md";
export type DialogueEntry = { question: string; answer: string };
export type TeamRole = { name: string; task: string; added?: boolean };
export type PlanStep = { title: string; detail: string; roles: string[] };
export type Snapshot = {
  persona: Persona;
  brief: string;
  context: string;
  dialogue: DialogueEntry[];
  resultDialogue: DialogueEntry[];
  team: TeamRole[];
  plan: PlanStep[];
  outputType: OutputType;
  format: Format;
};
export type Version = { n: number; date: string; changed: string; snapshot: Snapshot };
export type Skill = { version: number; slug: string; date: string; files: Record<string, string>; trial: "not_run" };
export type Project = {
  id: string;
  stage: Stage;
  persona: Persona | null;
  brief: string;
  context: string;
  dialogue: DialogueEntry[];
  resultDialogue: DialogueEntry[];
  team: TeamRole[];
  plan: PlanStep[];
  outputType: OutputType;
  format: Format;
  versions: Version[];
  skills: Skill[];
  workStep: number;
  updatedAt: string;
  deletedAt?: string;
};
export type Store = { projects: Project[]; activeId: string; view: "workspace" | "projects" };

export const STORAGE_KEY = "coreteams-react-prototype-2026-11-11-v1";
export const PROJECT_LIMIT = 5;
export const stages: Stage[] = ["start", "questions", "team", "work", "result"];
export const stageNames = ["Начало", "Уточнение", "Команда и план", "Работа команды", "Результат"];

export const personaInfo = {
  owner: {
    name: "Владелец решения",
    short: "Для руководителя или инициатора, который отвечает за выбор и его обоснование.",
    question: "Какой продукт вы создаёте или развиваете и какое решение по нему нужно принять?",
    placeholder: "Например: запускаем кофейню; нужно решить, арендовать ли помещение сейчас",
    examples: ["Запустить кофейню", "Выбрать направление продукта", "Подготовить новую услугу"],
    questions: [
      "Что сейчас мешает принять решение по этому продукту?",
      "Что уже известно или проверено? Какие факты и материалы есть?",
      "Какие сроки, бюджет и другие условия влияют на выбор?",
      "По каким признакам вы поймёте, что решение подходит?",
    ],
    team: [
      { name: "Аналитик", task: "Собирает основания и пробелы в данных" },
      { name: "Исследователь", task: "Формулирует проверки и альтернативы" },
      { name: "Фасилитатор", task: "Сводит выводы в решение и план" },
    ],
  },
  implementer: {
    name: "Ответственный за реализацию",
    short: "Проверьте техническое решение и при необходимости создайте скилл для повторного применения.",
    question: "Какой продукт вы реализуете и какое техническое решение нужно проверить?",
    placeholder: "Например: сервис для мероприятий; проверить вариант навигации посетителей",
    examples: ["Проверить архитектуру сервиса", "Оценить технические риски", "Собрать скилл для задачи"],
    questions: [
      "Какой технический вопрос нужно проверить первым?",
      "Что уже пробовали и какие данные или материалы есть?",
      "Какие сроки, совместимость и другие ограничения важны?",
      "Какой тест покажет, что решение можно реализовать?",
    ],
    team: [
      { name: "Технический аналитик", task: "Раскладывает решение на проверяемые части" },
      { name: "Специалист по рискам", task: "Ищет ограничения и зависимости" },
      { name: "Фасилитатор", task: "Сводит проверку в план и результат" },
    ],
  },
} as const;

export const dateLabel = () => new Intl.DateTimeFormat("ru-RU").format(new Date());
export const id = () => crypto.randomUUID();
export function freshProject(): Project {
  return { id: id(), stage: "start", persona: null, brief: "", context: "", dialogue: [], resultDialogue: [], team: [], plan: [], outputType: "document", format: "pdf", versions: [], skills: [], workStep: 0, updatedAt: new Date().toISOString() };
}
export function defaultPlan(persona: Persona, team: TeamRole[]): PlanStep[] {
  const others = team.filter(role => role.name !== "Фасилитатор").map(role => role.name);
  return [
    { title: "Уточнить основания", detail: persona === "implementer" ? "Зафиксировать технический вопрос, данные и ограничения" : "Собрать факты, критерий решения и ограничения", roles: ["Фасилитатор"] },
    { title: "Проверить варианты и риски", detail: persona === "implementer" ? "Сопоставить технические варианты, зависимости и риски" : "Сравнить варианты и обозначить пробелы в данных", roles: others },
    { title: "Собрать результат", detail: "Зафиксировать выводы, неизвестное и следующий шаг", roles: ["Фасилитатор"] },
  ];
}
export function selectPersona(project: Project, persona: Persona): Project {
  if (project.persona === persona) return project;
  const team = personaInfo[persona].team.map(role => ({ ...role }));
  return { ...project, persona, brief: "", context: "", dialogue: [], resultDialogue: [], team, plan: defaultPlan(persona, team), outputType: "document", format: "pdf", versions: [], skills: [], workStep: 0, stage: "start" };
}
export function versionFrom(project: Project, changed: string): Version {
  if (!project.persona) throw new Error("Сначала выберите роль");
  const snapshot: Snapshot = {
    persona: project.persona, brief: project.brief, context: project.context,
    dialogue: project.dialogue.map(item => ({ ...item })), resultDialogue: project.resultDialogue.map(item => ({ ...item })),
    team: project.team.map(item => ({ ...item })), plan: project.plan.map(item => ({ ...item, roles: [...item.roles] })),
    outputType: project.outputType, format: project.format,
  };
  return { n: project.versions.length + 1, date: dateLabel(), changed, snapshot };
}
export function sections(version: Version) {
  const s = version.snapshot;
  const firstAnswer = s.dialogue[0]?.answer || "Пока не указано";
  return [
    { title: "Что уже решено", lines: ["Сформулирована задача, выбран состав и согласован план проверки."] },
    { title: "Что изменилось", lines: [version.changed] },
    { title: "Что нужно от вас", lines: ["Проверьте основания и выберите следующий шаг."] },
    { title: "Основания и допущения", lines: ["Доступны только введённые вами данные. Независимая проверка не проводилась.", `Условия: ${s.context || "пока не указаны"}.`] },
    { title: "Карта гипотез", lines: [`Проверяемое предположение: ${firstAnswer}.`, "Источник подтверждения и критерий успеха нужно уточнить."] },
    { title: "План проверки", lines: s.plan.map((step, index) => `${index + 1}. ${step.title}: ${step.detail}. Участвуют: ${step.roles.join(", ")}.`) },
    { title: "Первый шаг", lines: [s.plan[0]?.detail || "Уточнить критерий решения", "Зафиксировать наблюдение, источник и следующий шаг."] },
  ];
}
export function markdown(version: Version): string {
  const s = version.snapshot;
  return `# Результат по задаче · версия ${version.n}\n\n> Локальный макет. Анализ файлов, запуск агентов и серверное сохранение не выполнялись.\n\nДата: ${version.date}\nРоль: ${personaInfo[s.persona].name}\n\n## Задача\n\n${s.brief}\n\n## Вводные\n\n${s.dialogue.map(item => `- **${item.question}** ${item.answer}`).join("\n")}\n\n${sections(version).map(section => `## ${section.title}\n\n${section.lines.join("\n\n")}`).join("\n\n")}\n`;
}
export function createSkill(project: Project, version: Version): Skill {
  const slug = `coreteams-technical-check-${project.id.slice(0, 8)}`;
  const base = `${slug}/`;
  const topic = version.snapshot.brief.split(/[:\n]/)[0].trim().slice(0, 100) || "технического решения";
  const skill = `---\nname: ${slug}\ndescription: ${JSON.stringify(`Применять при проверке технического решения для «${topic}» до реализации; результат — основания, ограничения и минимальный тест.`)}\n---\n\n# Проверка технического решения\n\n## Триггер\n\nПрименяйте до реализации, когда нужно проверить похожее техническое решение.\n\n## Вход\n\n- Описание продукта и решения.\n- Сроки, материалы, ограничения и источники, если они известны.\n\n## Шаги\n\n1. Сформулируйте проверяемое техническое утверждение.\n2. Отделите факты от допущений и назовите источники.\n3. Перечислите ограничения и зависимости.\n4. Опишите минимальный тест и измеримый критерий успеха.\n5. Сравните наблюдение с критерием или укажите «Не проверено».\n6. Заполните шаблон результата и предложите следующий шаг.\n\n## Запреты\n\n- Не выдавайте предположение за факт.\n- Не утверждайте, что тест или анализ файла выполнен, без результата и источника.\n\n## Проверка\n\nИспользуйте references/acceptance-checklist.md. До пробного запуска статус — «Черновик».\n\n## Владелец и ревизия\n\nНазначьте владельца перед использованием. Следующая ревизия — после пробного запуска.\n`;
  return {
    version: version.n, slug, date: dateLabel(), trial: "not_run",
    files: {
      [`${base}SKILL.md`]: skill,
      [`${base}README.md`]: "# Черновик скилла CoreTeams\n\nОткройте SKILL.md. Подставьте тестовую задачу, выполните пробный запуск в совместимой среде и заполните чек-лист. Прототип не запускал скилл и не сохранял его на сервере.\n",
      [`${base}REPORT.md`]: markdown(version),
      [`${base}references/result-template.md`]: "# Результат проверки\n\n## Основания и подтверждения\n\n## Допущения и неизвестное\n\n## Ограничения и зависимости\n\n## Критерии проверки и статус\n",
      [`${base}references/example-input.md`]: `# Пример входа\n\nЗадача: ${version.snapshot.brief}\n\nУсловия: ${version.snapshot.context || "не указаны"}\n\nЭто исходные данные макета, не проверенные факты.\n`,
      [`${base}references/acceptance-checklist.md`]: "# Чек-лист\n\n- [ ] У каждого факта есть источник.\n- [ ] Задан минимальный тест и критерий успеха.\n- [ ] Результат заполнен по четырём блокам.\n- [ ] Пробный запуск проведён на отдельной задаче.\n- [ ] Назначен владелец скилла.\n",
    },
  };
}
export function readStore(): Store {
  try {
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null") as Store | null;
    if (raw && Array.isArray(raw.projects) && raw.projects.length) {
      const open = new URLSearchParams(location.search).get("open");
      const activeId = open && raw.projects.some(item => item.id === open && !item.deletedAt) ? open : raw.activeId;
      return { ...raw, activeId, view: "workspace" };
    }
  } catch { /* Keep the prototype usable when browser storage is unavailable. */ }
  const project = freshProject();
  return { projects: [project], activeId: project.id, view: "workspace" };
}
