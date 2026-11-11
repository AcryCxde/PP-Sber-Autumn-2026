import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import {
  PROJECT_LIMIT, STORAGE_KEY, createSkill, dateLabel, defaultPlan, freshProject, markdown,
  personaInfo, readStore, sections, selectPersona, stageNames, stages, versionFrom,
  type DialogueEntry, type Format, type Persona, type PlanStep, type Project, type Skill,
  type Stage, type Store, type TeamRole, type Version,
} from "./model.ts";
import { downloadSkill, downloadVersion } from "./downloads.ts";
import "./react.css";

const formats: Format[] = ["pdf", "docx", "pptx", "xlsx", "md"];
const resultQuestions = [
  "Что в результате хотите уточнить или улучшить?",
  "Какой новый факт, отзыв или ограничение стоит учесть?",
  "Какой вывод или следующий шаг вы ожидаете от новой версии?",
];

function questionFor(project: Project, kind: "initial" | "result") {
  const entries = kind === "initial" ? project.dialogue : project.resultDialogue;
  const questions = kind === "initial" && project.persona ? personaInfo[project.persona].questions : resultQuestions;
  if (entries.length < questions.length) return questions[entries.length];
  return kind === "initial" ? "Что ещё важно знать, прежде чем предложить план?" : "Что ещё нужно учесть в следующей версии?";
}

function Chat({ project, kind, onAnswer, onReady }: {
  project: Project;
  kind: "initial" | "result";
  onAnswer: (answer: string) => void;
  onReady: () => void;
}) {
  const [draft, setDraft] = useState("");
  const [fileName, setFileName] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);
  const entries = kind === "initial" ? project.dialogue : project.resultDialogue;
  const ready = entries.length >= (kind === "initial" ? 4 : 3);
  const submit = () => {
    const answer = draft.trim();
    if (!answer) return;
    onAnswer(answer);
    setDraft("");
  };
  const keyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  };
  return <div className="facilitator-chat surface" aria-label="Диалог с фасилитатором">
    <div className="chat-header">
      <div className="question-top"><span className="facilitator-icon" aria-hidden="true">✳</span><div><strong>Фасилитатор</strong><small>{ready ? "Основные вопросы пройдены. Можно добавить детали." : "Демо: вопросы по сценарию, без анализа ответов"}</small></div></div>
      {ready && <div className="chat-head-actions"><button className="button primary" onClick={onReady}>{kind === "initial" ? "Посмотреть команду и план →" : "Создать новую версию →"}</button></div>}
    </div>
    <div className="chat-thread" role="log" aria-live="polite" aria-label="История диалога">
      {entries.map((entry, index) => <div className="chat-pair" key={index}>
        <div className="chat-message facilitator"><small>Фасилитатор</small><p>{entry.question}</p></div>
        <div className="chat-message user"><small>Вы</small><p>{entry.answer}</p></div>
      </div>)}
      <div className="chat-pair current-pair"><div className="chat-message facilitator current"><small>Фасилитатор · вопрос {entries.length + 1}</small><p>{questionFor(project, kind)}</p></div></div>
    </div>
    <form className="chat-composer" onSubmit={event => { event.preventDefault(); submit(); }}>
      <div className="chat-label-row"><label className="field-label" htmlFor={`${kind}-chat-input`}>Ваш ответ</label><small>Enter — отправить · Shift+Enter — новая строка</small></div>
      <div className="chat-input-row">
        <button className="chat-attach-button" type="button" title="Добавить файл" aria-label="Добавить файл" data-tooltip="Добавить файл" onClick={() => fileInput.current?.click()}><svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="m20.5 11.5-8.8 8.8a5 5 0 0 1-7.1-7.1l9.2-9.2a3.5 3.5 0 0 1 5 5l-9.2 9.2a2 2 0 0 1-2.8-2.8l8.5-8.5" /></svg></button>
        <textarea id={`${kind}-chat-input`} className="textarea" rows={3} maxLength={2000} value={draft} onChange={event => setDraft(event.target.value)} onKeyDown={keyDown} placeholder="Ответьте своими словами; можно добавить факты, сомнения и ограничения" />
        <div className="chat-actions"><button className="button primary" type="submit">Отправить ответ ↗</button><button className="button quiet" type="button" onClick={() => onAnswer("Пока не знаю")}>Пока не знаю</button></div>
      </div>
      <input ref={fileInput} className="visually-hidden" type="file" aria-label="Выберите файл для макета" onChange={event => setFileName(event.target.files?.[0]?.name || "")} />
      {fileName && <p className="chat-file-status">Выбран: {fileName}. Файл не загружен и не анализировался.</p>}
    </form>
  </div>;
}

function StartScreen({ project, onSelect, onBrief, onStart }: {
  project: Project; onSelect: (persona: Persona) => void; onBrief: (value: string) => void; onStart: () => void;
}) {
  const info = project.persona ? personaInfo[project.persona] : null;
  return <section aria-labelledby="start-title">
    <p className="eyebrow">CORETEAMS · НОВЫЙ ПРОЕКТ</p>
    <h1 className="page-title" id="start-title">Опишите сложную задачу по продукту — <em>получите решение.</em></h1>
    <p className="lead">CoreTeams задаст уточняющие вопросы, предложит состав команды и подготовит файл с выводами, допущениями и следующим шагом.</p>
    <div className="section-block"><p className="section-kicker">01 / ВАША РОЛЬ В ЭТОМ ПРОЕКТЕ</p><h2 className="section-title">Как вы участвуете в решении?</h2>
      <div className="role-grid">{(["owner", "implementer"] as Persona[]).map((persona, index) => <article className={`role-card ${project.persona === persona ? "selected" : ""}`} key={persona}>
        <span className="role-symbol" aria-hidden="true">{index === 0 ? "◇" : "⌘"}</span><h3>{personaInfo[persona].name}</h3><p>{personaInfo[persona].short}</p>
        <button className="button primary small" onClick={() => onSelect(persona)}>{project.persona === persona ? "Выбрано ✓" : "Выбрать →"}</button>
      </article>)}</div>
    </div>
    <div className="brief-box"><p className="section-kicker">02 / СУТЬ ЗАДАЧИ</p><h2 className="section-title">{info ? "Назовите продукт и решение" : "Сначала выберите роль"}</h2>
      {info ? <><label className="field-label" htmlFor="brief">{info.question}</label><textarea className="textarea" id="brief" maxLength={1000} value={project.brief} onChange={event => onBrief(event.target.value)} placeholder={info.placeholder} /><p className="helper">Достаточно сути. Срок, бюджет, материалы и файлы добавите в диалоге.</p>
        <div className="examples"><span className="helper">Примеры:</span>{info.examples.map(example => <button className="example-chip" key={example} onClick={() => onBrief(example)}>{example}</button>)}</div></> : <p className="helper">Роль выбирается для каждого нового проекта.</p>}
      <div className="form-actions"><button className="button primary" disabled={!info} onClick={onStart}>Разобрать задачу ↗</button><span className="notice-line">Для знакомства с пространством вход не нужен.</span></div>
    </div>
  </section>;
}

function QuestionsScreen({ project, onAnswer, onReady }: { project: Project; onAnswer: (answer: string) => void; onReady: () => void }) {
  return <section className="questions-screen" aria-labelledby="questions-title">
    <p className="eyebrow">УТОЧНЕНИЕ · ШАГ 1</p><h1 className="page-title" id="questions-title">Расскажите о задаче <em>своими словами.</em></h1>
    <p className="lead">Фасилитатор задаёт вопросы по одному прямо здесь.</p>
    <div className="workspace-summary"><small>ВАША ЗАДАЧА · {project.persona ? personaInfo[project.persona].name : ""}</small><strong>{project.brief}</strong></div>
    <Chat project={project} kind="initial" onAnswer={onAnswer} onReady={onReady} />
  </section>;
}

function TeamScreen({ project, onBack, onConfirm, onAddRole, onPlan, onOutput, onFormat }: {
  project: Project; onBack: () => void; onConfirm: () => void; onAddRole: (name: string, task: string) => void;
  onPlan: (plan: PlanStep[]) => void; onOutput: (output: "document" | "skill") => void; onFormat: (format: Format) => void;
}) {
  const [roleOpen, setRoleOpen] = useState(false);
  const [roleName, setRoleName] = useState("");
  const [roleTask, setRoleTask] = useState("");
  const [planOpen, setPlanOpen] = useState(false);
  const [planDraft, setPlanDraft] = useState<PlanStep[]>(project.plan);
  const [error, setError] = useState("");
  const addRole = (event: FormEvent) => {
    event.preventDefault();
    if (project.team.some(role => role.name.toLowerCase() === roleName.trim().toLowerCase())) return setError("Такая роль уже есть в команде.");
    onAddRole(roleName.trim(), roleTask.trim()); setRoleOpen(false); setRoleName(""); setRoleTask(""); setError("");
  };
  const editStep = (index: number, patch: Partial<PlanStep>) => setPlanDraft(previous => previous.map((step, i) => i === index ? { ...step, ...patch } : step));
  const savePlan = (event: FormEvent) => {
    event.preventDefault();
    if (planDraft.some(step => !step.title.trim() || !step.detail.trim() || !step.roles.length)) return setError("Заполните шаги и назначьте хотя бы одну роль каждому.");
    onPlan(planDraft.map(step => ({ ...step, title: step.title.trim(), detail: step.detail.trim() })));
    setPlanOpen(false); setError("");
  };
  return <section className="team-page" aria-labelledby="team-title">
    <p className="eyebrow">КОМАНДА И ПЛАН · ШАГ 2</p><h1 className="page-title" id="team-title">Соберите проверку <em>вместе.</em></h1><p className="lead">Проверьте роли и план, затем выберите результат.</p>
    <div className="workspace-summary"><small>ЗАДАЧА · {project.persona ? personaInfo[project.persona].name : ""}</small><strong>{project.brief}</strong></div>
    <div className="team-decision-grid">
      <div className="team-pane surface"><div className="team-pane-head"><div><p className="section-kicker">01 / КОМАНДА</p><h2 className="section-title">Кто участвует</h2></div><button className="button secondary small" onClick={() => setRoleOpen(!roleOpen)}>+ Добавить роль</button></div>
        <div className="team-list">{project.team.map((role, index) => <article className={`team-role-row ${role.added ? "added" : ""}`} key={`${role.name}-${index}`}><span className="team-role-number" aria-hidden="true">{role.added ? "+" : String(index + 1).padStart(2, "0")}</span><div><strong>{role.name}</strong><p>{role.task}</p></div>{role.added && <span className="team-added-label">Добавлена</span>}</article>)}</div>
        {roleOpen && <form className="mini-form" onSubmit={addRole}><label className="field-label" htmlFor="role-name">Название роли</label><input className="input" id="role-name" required maxLength={60} value={roleName} onChange={event => setRoleName(event.target.value)} placeholder="Например, дизайнер" /><label className="field-label" htmlFor="role-task">Что она должна проверить или подготовить?</label><input className="input" id="role-task" required maxLength={160} value={roleTask} onChange={event => setRoleTask(event.target.value)} /><button className="button primary small">Добавить в состав</button><p className="helper">Роль добавится в план макета. Агент не запускается.</p></form>}
      </div>
      <div className="team-pane surface"><div className="team-pane-head"><div><p className="section-kicker">02 / ПЛАН</p><h2 className="section-title">Как проверим</h2></div><button className="button secondary small" onClick={() => { setPlanOpen(!planOpen); setPlanDraft(project.plan.map(step => ({ ...step, roles: [...step.roles] }))); setError(""); }}>{planOpen ? "Закрыть" : "Поправить план"}</button></div>
        {planOpen ? <form className="plan-editor" onSubmit={savePlan}>{planDraft.map((step, index) => <fieldset className="plan-edit-step" key={index}><legend>Шаг {index + 1}</legend><label className="field-label" htmlFor={`plan-title-${index}`}>Название</label><input className="input" id={`plan-title-${index}`} required maxLength={80} value={step.title} onChange={event => editStep(index, { title: event.target.value })} /><label className="field-label" htmlFor={`plan-detail-${index}`}>Что сделать</label><input className="input" id={`plan-detail-${index}`} required maxLength={180} value={step.detail} onChange={event => editStep(index, { detail: event.target.value })} /><span className="field-label">Кто участвует</span><div className="plan-role-options">{project.team.map(role => <label className="plan-role-check" key={role.name}><input type="checkbox" checked={step.roles.includes(role.name)} onChange={event => editStep(index, { roles: event.target.checked ? [...step.roles, role.name] : step.roles.filter(name => name !== role.name) })} /><span>{role.name}</span></label>)}</div></fieldset>)}<div className="plan-editor-actions"><button className="button primary small">Завершить редактирование</button><button className="button secondary small" type="button" onClick={() => setPlanOpen(false)}>Вернуться без изменений</button></div></form> : <div className="plan-steps">{project.plan.map((step, index) => <div className="plan-step" key={index}><b>{String(index + 1).padStart(2, "0")}</b><div><strong>{step.title}</strong><small>{step.detail}</small><span className="plan-step-roles">{step.roles.join(" · ")}</span></div></div>)}</div>}
      </div>
    </div>
    <div className={`team-output-grid ${project.persona === "implementer" ? "" : "single"}`}>
      {project.persona === "implementer" && <div className="team-option-pane surface"><p className="section-kicker">03 / ТИП РЕЗУЛЬТАТА</p><h2 className="section-title">Что подготовить</h2><div className="output-choice-grid" role="group" aria-label="Тип результата"><button className={`output-choice ${project.outputType === "document" ? "selected" : ""}`} aria-pressed={project.outputType === "document"} onClick={() => onOutput("document")}><strong>Документ</strong><span>Выводы и план проверки</span></button><button className={`output-choice ${project.outputType === "skill" ? "selected" : ""}`} aria-pressed={project.outputType === "skill"} onClick={() => onOutput("skill")}><strong>Создать скилл</strong><span>Файл SKILL.md и архив с примерами</span></button></div><p className="helper">Скилл сохраняется как черновик; пробный запуск недоступен.</p></div>}
      <div className="team-option-pane surface"><p className="section-kicker">{project.persona === "implementer" ? "04" : "03"} / ФОРМАТ ФАЙЛА</p><h2 className="section-title">{project.outputType === "skill" ? "Формат отчёта" : "Формат результата"}</h2><div className="format-grid">{formats.map(format => <button className={`format-option ${project.format === format ? "active" : ""}`} aria-pressed={project.format === format} key={format} onClick={() => onFormat(format)}>{format.toUpperCase()}</button>)}</div><p className="helper">Файл создаётся локально в браузере по шаблону.</p></div>
    </div>
    {error && <p className="error" role="alert">{error}</p>}
    <div className="team-confirm-bar"><button className="button secondary" onClick={onBack}>← Вернуться к вопросам</button><span>Это предложение макета. Агенты не запускались.</span><button className="button primary" disabled={planOpen} onClick={onConfirm}>Согласовать план и продолжить →</button></div>
  </section>;
}

function WorkScreen({ project, onBack, onNext, onFinish }: { project: Project; onBack: () => void; onNext: () => void; onFinish: () => void }) {
  const version = project.versions.at(-1);
  const plan = version?.snapshot.plan || project.plan;
  const visible = Math.max(1, Math.min(project.workStep || 1, plan.length));
  const current = plan[visible - 1];
  return <section className="work-page" aria-labelledby="work-title">
    <p className="eyebrow">РАБОТА КОМАНДЫ · ШАГ 3</p><h1 className="page-title" id="work-title">Как команда <em>собирает результат.</em></h1><p className="lead">Схема показывает, как общий контекст и выводы переходят между этапами согласованного плана.</p>
    <div className="work-honesty" role="note"><strong>Схема работы · демо</strong><span>Подсветка показывает этапы плана. Агенты не запускались; факты и выводы не проверялись.</span></div>
    <div className="flow-board" aria-label="Схема передачи информации между ролями"><div className="flow-board-head"><strong>Как движется результат</strong><span>Этап {visible} из {plan.length}</span></div><div className="flow-map">{plan.map((step, index) => {
      const status = index < visible - 1 ? "complete" : index === visible - 1 ? "current" : "pending";
      return <div className="flow-unit" key={index}><div className={`flow-step ${status}`} aria-current={status === "current" ? "step" : undefined}><span className="flow-step-number">{String(index + 1).padStart(2, "0")}</span><div className="flow-step-content"><span className="flow-step-state">{status === "complete" ? "Передано дальше" : status === "current" ? "Показан сейчас" : "Следующий этап"}</span><h2>{step.title}</h2><div className="flow-agents" aria-label="Участники этапа">{step.roles.map(name => <span className="flow-agent" key={name}><i aria-hidden="true" />{name}</span>)}</div></div></div>{index < plan.length - 1 && <span className={`flow-connector ${index < visible - 1 ? "passed" : ""} ${index === visible - 2 ? "handoff" : ""}`} aria-hidden="true"><span className="flow-line" /><span className="flow-packet" /><span className="flow-arrow">→</span></span>}</div>;
    })}</div></div>
    <div className="flow-current" role="status"><span className="flow-current-symbol" aria-hidden="true">✳</span><div><small>СЕЙЧАС В СХЕМЕ · {current?.roles.join(" · ")}</small><strong>{current?.title}</strong><p>{current?.detail}. {visible === plan.length ? "Выход по плану — итоговый файл." : "Выход по плану — вводные для следующего этапа."}</p></div></div>
    <div className="work-footer"><button className="button secondary" onClick={onBack}>← Вернуться к команде и плану</button>{visible < plan.length ? <button className="button primary" onClick={onNext}>Показать передачу дальше →</button> : <button className="button primary" onClick={onFinish}>Завершить и открыть результат →</button>}</div>
  </section>;
}

function SkillCard({ project, skill }: { project: Project; skill: Skill }) {
  const current = project.versions.at(-1)?.n === skill.version;
  return <div className="result-skill-artifact" aria-label="Скилл для задачи"><div className="result-skill-head"><span className="result-skill-icon" aria-hidden="true">ZIP</span><div><strong>Скилл для задачи · версия {skill.version}</strong><small>Черновик сохранён в этом браузере · {skill.date}{current ? "" : " · предыдущая версия"}</small></div></div><p>Внутри: SKILL.md, шаблоны проверки и отчёт по задаче.</p><div className="skill-actions"><a className="button secondary small" href={`?skill=${skill.version}&project=${project.id}`} target="_blank" rel="noopener">Посмотреть SKILL.md</a><button className="button primary small" onClick={() => downloadSkill(skill)}>Скачать архив скилла</button></div><p className="helper">Архив собран локально по шаблону. Скилл ещё не проверен пробным запуском.</p></div>;
}

function ResultScreen({ project, onAnswer, onNewVersion, onFormat, onError }: {
  project: Project; onAnswer: (answer: string) => void; onNewVersion: () => void; onFormat: (format: Format) => void; onError: (message: string) => void;
}) {
  const [showVersions, setShowVersions] = useState(false);
  const version = project.versions.at(-1)!;
  const isSkill = version.snapshot.outputType === "skill" && version.snapshot.persona === "implementer";
  const skill = [...project.skills].reverse().find(item => project.versions.some(version => version.n === item.version));
  const exportFile = async (target: Version) => { try { await downloadVersion(target, project.format); } catch (error) { onError(error instanceof Error ? error.message : "Не удалось скачать файл"); } };
  return <section className={skill ? "with-skill" : ""} aria-labelledby="result-title">
    <p className="eyebrow">РЕЗУЛЬТАТ · ШАГ 4</p><h1 className="result-title" id="result-title">{isSkill ? "Скилл для задачи" : "Результат по задаче"}</h1><p className="result-subtitle">{isSkill ? "Откройте черновик SKILL.md или скачайте архив для проверки в совместимой среде." : "Откройте документ в отдельном окне или скачайте его."}</p>
    {skill && <SkillCard project={project} skill={skill} />}
    <div className="result-dashboard"><div className="result-dialog"><Chat project={project} kind="result" onAnswer={onAnswer} onReady={onNewVersion} /></div><div className="result-files">
      <div className="artifact result-artifact"><div className="artifact-head"><div className="artifact-icon">{project.format.toUpperCase()}</div><div><strong>{isSkill ? "Короткий отчёт" : "Результат по задаче"} · версия {version.n}</strong><small>{version.date} · текущая · черновик в этом браузере</small><span className="artifact-contents">Внутри: карта гипотез · план проверки · первый шаг</span></div></div><div className="artifact-actions"><a className="button secondary small" href={`?preview=${version.n}&project=${project.id}`} target="_blank" rel="noopener">Посмотреть версию</a></div></div>
      <div className="export-bar"><div><strong>Файл результата</strong><span>Выберите формат и скачайте свою версию</span></div><div className="format-grid" role="group" aria-label="Формат скачиваемого файла">{formats.map(format => <button className={`format-option ${project.format === format ? "active" : ""}`} aria-pressed={project.format === format} key={format} onClick={() => onFormat(format)}>{format.toUpperCase()}</button>)}</div><button className="button primary" onClick={() => void exportFile(version)}>Скачать {project.format.toUpperCase()}</button></div>
      <p className="result-caption">Файл создаётся в браузере по шаблону CoreTeams. Это макет без анализа файлов, запуска агентов и серверного сохранения.</p>
    </div></div>
    <button className="button ghost small" onClick={() => setShowVersions(!showVersions)}>{showVersions ? "Скрыть сохранённые версии" : "Открыть все сохранённые версии"} →</button>
    {showVersions && <div className="version-list">{[...project.versions].reverse().map((item, index) => <div className="version-row" key={item.n}><div><strong>Версия {item.n} · {index === 0 ? "текущая" : "предыдущая"}</strong><small>{item.date} · {item.changed}{project.skills.some(skill => skill.version === item.n) ? " · скилл сохранён" : ""}</small></div><div className="version-actions"><a className="button secondary small" href={`?preview=${item.n}&project=${project.id}`} target="_blank" rel="noopener">Посмотреть версию</a><button className="button secondary small" onClick={() => void exportFile(item)}>Скачать {project.format.toUpperCase()}</button>{project.skills.find(skill => skill.version === item.n) && <button className="button secondary small" onClick={() => downloadSkill(project.skills.find(skill => skill.version === item.n)!)}>Скачать архив скилла</button>}</div></div>)}</div>}
    <div className="result-followup surface"><div><p className="section-kicker">СЛЕДУЮЩИЙ ШАГ</p><h2 className="section-title">{isSkill ? "Проверьте скилл на тестовой задаче" : "Проверьте первый шаг из документа"}</h2><p className="helper">{isSkill ? "Скачайте архив скилла и выполните пробный запуск в совместимой среде; результат расскажите фасилитатору выше." : "Расскажите фасилитатору выше, что удалось узнать: он поможет подготовить следующую версию."}</p></div></div>
  </section>;
}

function ProjectsScreen({ store, onOpen, onNew, onDelete, onRestore }: { store: Store; onOpen: (id: string) => void; onNew: () => void; onDelete: (id: string) => void; onRestore: (id: string) => void }) {
  const active = store.projects.filter(project => !project.deletedAt).sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
  const trash = store.projects.filter(project => project.deletedAt);
  return <section className="projects-page" aria-labelledby="projects-title"><p className="eyebrow">ПРОЕКТЫ НА ЭТОМ УСТРОЙСТВЕ</p><h1 className="page-title" id="projects-title">Мои проекты</h1><p className="lead">Откройте проект, чтобы продолжить с того же этапа. Черновики и версии остаются только в этом браузере.</p><div className="projects-section-head"><h2 className="section-title">Активные · {active.length} из {PROJECT_LIMIT}</h2><button className="button primary small" disabled={active.length >= PROJECT_LIMIT} onClick={onNew}>Новый проект</button></div>{active.length >= PROJECT_LIMIT && <p className="helper projects-helper">Достигнут лимит {PROJECT_LIMIT} активных проектов. Переместите один в корзину, чтобы создать новый.</p>}
    <div className="project-list">{active.map(project => <article className="project-row" key={project.id}><div className="project-details"><h3>{project.brief || "Новый проект"}</h3><p>{project.persona ? personaInfo[project.persona].name : "Роль ещё не выбрана"} · {project.versions.length} версий · {project.skills.length ? "скилл в проекте · " : ""}{project.id === store.activeId ? "сейчас открыт" : "локальный черновик"}</p></div><div className="project-actions"><button className="button secondary small" onClick={() => onOpen(project.id)}>Открыть проект</button><button className="button danger small" onClick={() => onDelete(project.id)}>Удалить</button></div></article>)}</div>
    <div className="projects-section-head trash-head"><h2 className="section-title">Корзина · {trash.length}</h2></div><p className="helper projects-helper">Удалённые проекты можно восстановить, пока они находятся в корзине.</p><div className="project-list">{trash.map(project => <article className="project-row trashed" key={project.id}><div className="project-details"><h3>{project.brief || "Новый проект"}</h3><p>Удалён {project.deletedAt ? new Intl.DateTimeFormat("ru-RU").format(new Date(project.deletedAt)) : ""}</p></div><div className="project-actions"><button className="button secondary small" disabled={active.length >= PROJECT_LIMIT} onClick={() => onRestore(project.id)}>Восстановить</button></div></article>)}</div>
  </section>;
}

function AccountModal({ onClose, onNotice }: { onClose: () => void; onNotice: (message: string) => void }) {
  const [mode, setMode] = useState<"register" | "login">("register");
  return <div className="dialog-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}><section className="dialog account-dialog" role="dialog" aria-modal="true" aria-labelledby="account-title"><div className="dialog-top"><div><p className="eyebrow">АККАУНТ CORETEAMS</p><h2 id="account-title">{mode === "register" ? "Создать аккаунт" : "Войти в аккаунт"}</h2></div><button className="dialog-close" aria-label="Закрыть" onClick={onClose}>×</button></div><div className="account-tabs"><button className={`account-tab ${mode === "register" ? "active" : ""}`} onClick={() => setMode("register")}>Регистрация</button><button className={`account-tab ${mode === "login" ? "active" : ""}`} onClick={() => setMode("login")}>Вход</button></div><p className="account-intro">{mode === "register" ? "Начните без входа. Для сохранения в аккаунте потребуется регистрация." : "В пилоте вход предусмотрен по почте и паролю после регистрации."}</p><div className="muted-panel"><strong>Это прототип.</strong><p>Аккаунт здесь не создаётся, данные формы не отправляются. Проект хранится только в этом браузере.</p></div><form autoComplete="off" onSubmit={event => { event.preventDefault(); onNotice("В прототипе аккаунт не создаётся. Проект остаётся в этом браузере."); onClose(); }}><label className="field-label" htmlFor="account-email">Электронная почта</label><input className="input" id="account-email" type="email" required placeholder="name@example.com" /><label className="field-label" htmlFor="account-pass">Пароль</label><input className="input" id="account-pass" type="password" required minLength={8} placeholder="Не вводите настоящий пароль" />{mode === "register" && <><label className="field-label" htmlFor="account-pass-repeat">Повторите пароль</label><input className="input" id="account-pass-repeat" type="password" required minLength={8} placeholder="Повторите пароль" /><label className="agreement-check"><input type="checkbox" required /><span>Я принимаю <a href="./agreement.html" target="_blank" rel="noopener">пользовательское соглашение</a></span></label><p className="helper">Это проект соглашения; перед запуском его должен утвердить владелец продукта.</p></>}<button className="button primary">{mode === "register" ? "Создать аккаунт" : "Войти"}</button></form><button className="button ghost" onClick={onClose}>Вернуться к проекту</button></section></div>;
}

function Preview({ store, kind, number, projectId }: { store: Store; kind: "skill" | "preview"; number: number; projectId: string }) {
  const project = store.projects.find(item => item.id === projectId);
  const version = project?.versions.find(item => item.n === number);
  const skill = project?.skills.find(item => item.version === number);
  return <div className="react-preview"><div className="preview-toolbar"><a href={`./?open=${projectId}`}>← Вернуться к проекту</a><span className="badge amber">Локальный макет · версия {number}</span></div><article className="document" id="main-content"><p className="demo-stamp">Без анализа файлов, запуска агентов и серверного сохранения</p>{kind === "skill" ? skill ? <><h1>SKILL.md</h1><pre className="skill-source">{skill.files[`${skill.slug}/SKILL.md`]}</pre></> : <><h1>Скилл не найден</h1><p>Он доступен в браузере, где сохранён проект.</p></> : version ? <><h1>Результат по задаче · версия {number}</h1><p className="doc-meta">{version.date} · {version.changed}</p><h2>Задача</h2><p>{version.snapshot.brief}</p>{sections(version).map(section => <section key={section.title}><h2>{section.title}</h2>{section.lines.map((line, index) => <p key={index}>{line}</p>)}</section>)}</> : <><h1>Версия не найдена</h1><p>Она доступна в браузере, где сохранён проект.</p></>}</article></div>;
}

export function App() {
  const [store, setStore] = useState<Store>(readStore);
  const [accountOpen, setAccountOpen] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  const [toast, setToast] = useState("");
  const project = store.projects.find(item => item.id === store.activeId && !item.deletedAt) || store.projects.find(item => !item.deletedAt) || store.projects[0];
  const activeCount = store.projects.filter(item => !item.deletedAt).length;
  useEffect(() => { try { localStorage.setItem(STORAGE_KEY, JSON.stringify(store)); } catch { /* The interface stays usable without storage. */ } }, [store]);
  useEffect(() => { if (!toast) return; const timer = window.setTimeout(() => setToast(""), 4300); return () => window.clearTimeout(timer); }, [toast]);
  const modify = (change: (project: Project) => Project) => setStore(previous => ({ ...previous, projects: previous.projects.map(item => item.id === previous.activeId ? { ...change(item), updatedAt: new Date().toISOString() } : item) }));
  const stage = (next: Stage) => modify(project => ({ ...project, stage: next }));
  const canOpen = (next: Stage) => {
    const index = stages.indexOf(next);
    if (index === 0) return true;
    if (!project.persona || !project.brief.trim()) return false;
    if (index === 1) return true;
    if (project.dialogue.length < 4 && !project.versions.length) return false;
    if (index === 2) return true;
    if (index === 3) return project.versions.length > 0;
    return project.versions.length > 0 && (project.workStep >= (project.versions.at(-1)?.snapshot.plan.length || 3) || project.stage === "result");
  };
  const newProject = () => {
    if (activeCount >= PROJECT_LIMIT) return setToast(`Лимит: ${PROJECT_LIMIT} активных проектов.`);
    const fresh = freshProject();
    setStore(previous => ({ ...previous, projects: [...previous.projects, fresh], activeId: fresh.id, view: "workspace" }));
  };
  const initialAnswer = (answer: string) => modify(project => ({ ...project, dialogue: [...project.dialogue, { question: questionFor(project, "initial"), answer } satisfies DialogueEntry] }));
  const resultAnswer = (answer: string) => modify(project => ({ ...project, resultDialogue: [...project.resultDialogue, { question: questionFor(project, "result"), answer } satisfies DialogueEntry] }));
  const confirmPlan = () => modify(project => {
    const previous = project.versions.at(-1);
    const changed = !previous ? "Первая версия плана" : JSON.stringify(previous.snapshot.plan) !== JSON.stringify(project.plan) || JSON.stringify(previous.snapshot.team) !== JSON.stringify(project.team) ? "Изменён состав команды или план проверки" : previous.snapshot.outputType !== project.outputType || previous.snapshot.format !== project.format ? "Изменён тип или формат результата" : "";
    return { ...project, versions: changed ? [...project.versions, versionFrom(project, changed)] : project.versions, workStep: changed ? 1 : project.workStep || 1, stage: "work" };
  });
  const finish = () => modify(project => {
    const version = project.versions.at(-1);
    const skills = version && project.persona === "implementer" && version.snapshot.outputType === "skill" && !project.skills.some(skill => skill.version === version.n) ? [...project.skills, createSkill(project, version)] : project.skills;
    return { ...project, skills, stage: "result" };
  });
  const nextVersion = () => modify(project => {
    if (project.resultDialogue.length < 3) return project;
    const version = versionFrom(project, "Уточнены условия после обсуждения результата");
    const skills = project.persona === "implementer" && project.outputType === "skill" ? [...project.skills, createSkill(project, version)] : project.skills;
    return { ...project, versions: [...project.versions, version], skills, resultDialogue: [], stage: "result" };
  });
  const params = new URLSearchParams(location.search);
  const previewKind = params.has("skill") ? "skill" : params.has("preview") ? "preview" : null;
  if (previewKind) return <Preview store={store} kind={previewKind} number={Number(params.get(previewKind))} projectId={params.get("project") || store.activeId} />;
  const next = project.stage === "start" ? "Опишите задачу по продукту" : project.stage === "questions" ? project.dialogue.length >= 4 ? "Посмотреть команду и план" : "Ответьте на вопрос фасилитатора" : project.stage === "team" ? "Согласуйте команду и план" : project.stage === "work" ? project.workStep >= project.plan.length ? "Открыть результат" : "Показать передачу дальше" : "Обсудите результат и следующий шаг";
  return <><a className="skip-link" href="#main-content">К содержимому</a><div className="app">
    <aside className="sidebar" aria-label="Навигация по проекту"><a className="brand" href="./" onClick={event => { event.preventDefault(); setStore(previous => ({ ...previous, view: "workspace" })); stage("start"); }}><span className="brand-mark" aria-hidden="true"><i /><i /><i /><i /></span><span>coreteams</span></a><button className={`projects-link ${store.view === "projects" ? "active" : ""}`} onClick={() => setStore(previous => ({ ...previous, view: "projects" }))}>Мои проекты <span>{activeCount} / {PROJECT_LIMIT}</span></button><div><p className="sidebar-caption">Рабочее пространство</p><nav className="nav-list" aria-label="Этапы">{stages.map((item, index) => <button className={`nav-button ${store.view === "workspace" && project.stage === item ? "active" : ""}`} key={item} disabled={!canOpen(item)} aria-current={store.view === "workspace" && project.stage === item ? "step" : undefined} onClick={() => { setStore(previous => ({ ...previous, view: "workspace" })); stage(item); }}><span className="nav-num">{index + 1}</span>{stageNames[index]}</button>)}</nav></div><div className="sidebar-bottom"><div className="draft-note"><strong>Локальные проекты</strong>Остаются только в этом браузере. На сервере не сохранены.</div></div></aside>
    <div className="main-shell"><header className="topbar"><div className="topbar-left"><span className="project-label">{store.view === "projects" ? "Мои проекты" : project.brief || "Новый проект"}</span><span className="topbar-dot" /><span className="topbar-sub">В этом браузере</span></div><div className="topbar-actions">{store.view === "projects" ? <button className="text-button" onClick={() => setStore(previous => ({ ...previous, view: "workspace" }))}>Вернуться к проекту</button> : <><button className="text-button" onClick={newProject}>Новый проект</button><button className="small-button" onClick={() => setAccountOpen(true)}>Сохранить проект</button></>}<button className="text-button account-entry" onClick={() => setAccountOpen(true)}>Регистрация / вход</button></div></header>
      <div className={`work-grid ${store.view === "projects" ? "projects-grid" : ""}`}><main className="main-content" id="main-content">{store.view === "projects" ? <ProjectsScreen store={store} onOpen={id => setStore(previous => ({ ...previous, activeId: id, view: "workspace" }))} onNew={newProject} onDelete={setPendingDelete} onRestore={id => setStore(previous => ({ ...previous, projects: previous.projects.map(item => item.id === id ? { ...item, deletedAt: undefined } : item) }))} /> : project.stage === "start" ? <StartScreen project={project} onSelect={persona => modify(project => selectPersona(project, persona))} onBrief={brief => modify(project => ({ ...project, brief }))} onStart={() => project.persona && project.brief.trim() ? stage("questions") : setToast("Выберите роль и опишите задачу.")} /> : project.stage === "questions" ? <QuestionsScreen project={project} onAnswer={initialAnswer} onReady={() => stage("team")} /> : project.stage === "team" ? <TeamScreen project={project} onBack={() => stage("questions")} onConfirm={confirmPlan} onAddRole={(name, task) => modify(project => ({ ...project, team: [...project.team, { name, task, added: true }], plan: project.plan.map((step, index) => index === 1 ? { ...step, roles: [...step.roles, name] } : step) }))} onPlan={plan => modify(project => ({ ...project, plan }))} onOutput={outputType => modify(project => ({ ...project, outputType }))} onFormat={format => modify(project => ({ ...project, format }))} /> : project.stage === "work" ? <WorkScreen project={project} onBack={() => stage("team")} onNext={() => modify(project => ({ ...project, workStep: Math.min(project.workStep + 1, project.plan.length) }))} onFinish={finish} /> : <ResultScreen project={project} onAnswer={resultAnswer} onNewVersion={nextVersion} onFormat={format => modify(project => ({ ...project, format }))} onError={setToast} />}</main>
        {store.view !== "projects" && <aside className="aside" aria-label="Состояние работы"><div className="aside-head"><h3>Сейчас</h3></div><div className="aside-body"><div className="aside-item"><small>Этап</small><strong>{stageNames[stages.indexOf(project.stage)]}</strong></div><div className="aside-item"><small>Следующий шаг</small><strong>{next}</strong></div><div className="aside-item"><small>Лимит демо</small><strong>Число кредитов уточняется</strong></div></div></aside>}
      </div>
    </div>
  </div>
  {accountOpen && <AccountModal onClose={() => setAccountOpen(false)} onNotice={setToast} />}
  {pendingDelete && <div className="dialog-backdrop" role="presentation"><section className="dialog" role="dialog" aria-modal="true" aria-labelledby="delete-title"><h2 id="delete-title">Переместить проект в корзину?</h2><p>Проект и версии можно восстановить из корзины.</p><button className="button danger confirm" onClick={() => { const deleted = pendingDelete; setStore(previous => { const projects = previous.projects.map(item => item.id === deleted ? { ...item, deletedAt: new Date().toISOString() } : item); const next = projects.find(item => !item.deletedAt); return { ...previous, projects: next ? projects : [...projects, freshProject()], activeId: next?.id || projects.at(-1)!.id, view: "projects" }; }); setPendingDelete(null); }}>Удалить проект</button><button className="button ghost" onClick={() => setPendingDelete(null)}>Оставить проект</button></section></div>}
  {toast && <div className="toast show" role="status">{toast}</div>}
  </>;
}
