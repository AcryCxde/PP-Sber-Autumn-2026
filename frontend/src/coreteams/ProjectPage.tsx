import { useCallback, useEffect, useState, type FormEvent } from 'react';
import {
  answerText,
  api,
  assumptions,
  scenarioOf,
  type Project,
  type ProjectPatch,
  type SetupStep,
} from './api';
import { errorText, projectRoute } from './actions';
import { MEDIA, useShell } from './context';
import { navigate, type Route } from './router';
import { useSocket } from './socket';
import { Assistant, Flow, GuideCard, Icon, Tag } from './ui';
import { Upload } from './Upload';
import { Workspace, TeamCards } from './Workspace';

type ProjectRoute = Extract<Route, { page: 'project' }>;
type StepProps = {
  project: Project;
  update: (patch: ProjectPatch) => Promise<Project | null>;
  setProject: (project: Project) => void;
  go: (step: SetupStep, question?: number) => void;
  reload: () => Promise<void>;
};

export function ProjectPage({ route }: { route: ProjectRoute }) {
  const { toast } = useShell();
  const [project, setProject] = useState<Project | null>(null);
  const [missing, setMissing] = useState(false);

  const reload = useCallback(async () => {
    try {
      setProject(await api.project(route.projectId));
    } catch (error) {
      const message = errorText(error, 'Проект не найден');
      if (message) setMissing(true);
    }
  }, [route.projectId]);

  useEffect(() => {
    let cancelled = false;
    api.project(route.projectId).then(
      (loaded) => {
        if (!cancelled) setProject(loaded);
      },
      (error: unknown) => {
        if (!cancelled && errorText(error, 'Проект не найден')) setMissing(true);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [route.projectId]);

  const update = useCallback(
    async (patch: ProjectPatch) => {
      try {
        const next = await api.patch(route.projectId, patch);
        setProject(next);
        return next;
      } catch (error) {
        const message = errorText(error, 'Изменения не сохранены');
        if (message) toast(message);
        return null;
      }
    },
    [route.projectId, toast],
  );

  const go = useCallback(
    (step: SetupStep, question = 0) => {
      void update({ setup_step: step, question_index: question });
      navigate({ page: 'project', projectId: route.projectId, step, question });
    },
    [route.projectId, update],
  );

  // Шаги подготовки доступны только черновику, рабочее пространство — только начатому проекту.
  const redirectToSetup = project?.status === 'draft' && route.step === 'workspace';
  useEffect(() => {
    if (redirectToSetup && project) navigate(projectRoute(project, project.question_index));
  }, [redirectToSetup, project]);

  if (missing) {
    return (
      <div className="page">
        <h1>Проект не найден</h1>
        <p className="lead">Возможно, он удалён или принадлежит другому аккаунту.</p>
        <a className="button primary" href="#profile">Мои проекты</a>
      </div>
    );
  }
  if (!project) {
    return <div className="page"><p className="helper">Загрузка проекта…</p></div>;
  }

  if (redirectToSetup) {
    return null;
  }
  if (project.status !== 'draft') {
    return <Workspace project={project} setProject={setProject} update={update} reload={reload} />;
  }

  const props = { project, update, setProject, go, reload };
  switch (route.step) {
    case 'context':
      return <ContextStep {...props} />;
    case 'question':
      return <QuestionStep key={route.question} index={route.question} {...props} />;
    case 'team-plan':
      return <TeamPlan {...props} />;
    case 'plan':
      return <PlanStep {...props} />;
    default:
      return <Welcome {...props} />;
  }
}

function Welcome({ project, go }: StepProps) {
  return (
    <Flow
      step={0}
      onBack={() => navigate({ page: 'home' })}
      aside={<GuideCard title="Я буду вашим проводником" copy="Меня называют фасилитатором. Я уточняю цель, передаю контекст специалистам и возвращаюсь к вам с вопросами и результатами." />}
    >
      {project.example ? <Tag>Вы пробуете на готовом примере</Tag> : <Tag>Знакомство · меньше минуты</Tag>}
      <h1 className="spacer-top">Вам не нужно знать,<br />с чего начать</h1>
      <p className="lead">Я помогу описать задачу и организую работу команды. Вы сможете остановиться, уточнить или вернуться позже.</p>
      <ol className="welcome-points">
        <li><span className="step-number">01</span><div><b>Принесите то, что уже есть</b><p>Описание идеи, документы, заметки или просто вопрос.</p></div></li>
        <li><span className="step-number">02</span><div><b>Ответьте на три вопроса</b><p>Выбирайте варианты, добавляйте свой ответ или попросите подсказку.</p></div></li>
        <li><span className="step-number">03</span><div><b>Согласуйте команду и план</b><p>Покажу, какие роли нужны, что они подготовят и как соединят выводы.</p></div></li>
      </ol>
      <button className="button primary" type="button" onClick={() => go('context')}>Рассказать о задаче <Icon name="arrow" /></button>
      <p className="helper">Подготовка занимает около 3 минут. Claude начнёт работу только после вашего согласования плана.</p>
    </Flow>
  );
}

function ContextStep({ project, update, setProject, go }: StepProps) {
  const { catalog, toast } = useShell();
  const [name, setName] = useState(project.name);
  const [problem, setProblem] = useState(project.problem);
  const [context, setContext] = useState(project.context);

  const changeScenario = async (scenario: string) => {
    if (scenario === project.scenario) return;
    const next = await update({ scenario: scenario as Project['scenario'], answers: [[], [], []], custom: ['', '', ''] });
    if (next) toast('Направление обновлено. Уточняющие вопросы и команда будут другими.');
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!name.trim() || !problem.trim()) {
      toast('Добавьте название и хотя бы короткое описание задачи.');
      return;
    }
    const next = await update({ name: name.trim(), problem: problem.trim(), context: context.trim() });
    if (next) go('question', 0);
  };

  return (
    <Flow
      step={0}
      onBack={() => go('welcome')}
      aside={<GuideCard title="Хорошее начало — конкретный пример" copy="Подойдут черновик, расчёты, отзывы или заметки. Если материалов пока нет, начнём с описания и отметим, что ещё нужно проверить." />}
    >
      <p className="eyebrow">1. Ваша задача</p>
      <h1>Что вы хотите сделать?</h1>
      <p className="lead">Начните с того, что знаете. Необязательно сразу описывать всё.</p>
      {project.example && <div className="notice">Это вымышленный курс для знакомства с сервисом. Любое поле можно изменить.</div>}
      <form onSubmit={(event) => void submit(event)}>
        <label className="field">
          <span>Какое направление ближе к задаче?</span>
          <select value={project.scenario} onChange={(event) => void changeScenario(event.target.value)}>
            {catalog.scenarios.map((item) => <option key={item.key} value={item.key}>{item.label}</option>)}
          </select>
        </label>
        <p className="helper">От направления зависят вопросы и состав команды. Если сомневаетесь, оставьте «Другая задача».</p>
        <label className="field">
          <span>Название проекта</span>
          <input value={name} onChange={(event) => setName(event.target.value)} placeholder="Например, моя кофейня, книга или учебный курс" required maxLength={120} />
        </label>
        <label className="field">
          <span>Что хочется изменить или понять?</span>
          <textarea value={problem} onChange={(event) => setProblem(event.target.value)} placeholder="Например: хочу открыть кофейню, но не знаю, что проверить до аренды помещения." required maxLength={4000} />
        </label>
        <details className="spacer-top" open={Boolean(project.context)}>
          <summary>Добавить контекст: сроки, ограничения, аудитория</summary>
          <label className="field">
            <span>Что ещё важно команде <span className="optional">· необязательно</span></span>
            <textarea value={context} onChange={(event) => setContext(event.target.value)} placeholder="Для кого проект, что уже сделано, что нельзя менять…" maxLength={8000} />
          </label>
        </details>
        <Upload project={project} onProject={setProject} />
        <div className="actions">
          <button className="button primary" type="submit">К уточняющим вопросам <Icon name="arrow" /></button>
        </div>
      </form>
    </Flow>
  );
}

function QuestionStep({ project, update, go, index }: StepProps & { index: number }) {
  const { catalog } = useShell();
  const scenario = scenarioOf(catalog, project.scenario);
  const question = scenario.questions[index];
  const [answers, setAnswers] = useState(project.answers);
  const [custom, setCustom] = useState(project.custom);
  const chosen = answers[index] ?? [];
  const suggested = chosen.includes(catalog.team_suggestion);
  const ready = chosen.length > 0 || Boolean(custom[index]?.trim());

  const setChosen = (next: string[]) => {
    const all = answers.map((item, i) => (i === index ? next : item));
    setAnswers(all);
    void update({ answers: all });
  };
  const toggle = (option: string) =>
    setChosen(chosen.includes(option) ? chosen.filter((x) => x !== option) : [...chosen.filter((x) => x !== catalog.team_suggestion), option]);

  const next = async () => {
    if (!ready) return;
    const saved = await update({ answers, custom });
    if (!saved) return;
    if (index < 2) go('question', index + 1);
    else go('team-plan', 2);
  };

  return (
    <Flow
      step={1}
      onBack={() => (index === 0 ? go('context') : go('question', index - 1))}
      aside={<GuideCard title={question.guide} copy={question.copy} />}
    >
      <p className="eyebrow">2. Уточнение · вопрос {index + 1} из 3</p>
      <h1>{question.title}</h1>
      <p className="lead">{question.help}</p>
      <div className="choices" role="group" aria-label="Варианты ответа">
        {question.options.map((option) => (
          <button key={option} className="choice" type="button" aria-pressed={chosen.includes(option)} onClick={() => toggle(option)}>
            <span className="check">✓</span>{option}
          </button>
        ))}
      </div>
      <label className="field">
        <span>Свой ответ <span className="optional">· можно дополнить выбранное</span></span>
        <input
          value={custom[index] ?? ''}
          onChange={(event) => setCustom(custom.map((item, i) => (i === index ? event.target.value : item)))}
          placeholder="Напишите, что ещё важно"
          maxLength={1000}
        />
      </label>
      <div className="suggestion">
        <button className="text-button" type="button" onClick={() => setChosen([catalog.team_suggestion])}>Не уверен — предложите вариант</button>
      </div>
      {suggested && <div className="notice">Начнём с предположения команды: «{scenario.suggestions[index]}». В плане оно будет отмечено как требующее проверки.</div>}
      <div className="actions">
        <button className="button primary" type="button" disabled={!ready} onClick={() => void next()}>
          {index < 2 ? 'Следующий вопрос' : 'Посмотреть команду'} <Icon name="arrow" />
        </button>
      </div>
    </Flow>
  );
}

function DiscussTeam({ project, onSave }: { project: Project; onSave: (text: string) => Promise<void> }) {
  const [text, setText] = useState(project.team_feedback);
  return (
    <>
      <p>Какой стороны задачи не хватает или какая роль кажется лишней?</p>
      <form onSubmit={(event) => {
        event.preventDefault();
        if (text.trim()) void onSave(text.trim());
      }}>
        <label className="field">
          <span>Ваше уточнение</span>
          <textarea value={text} onChange={(event) => setText(event.target.value)} required maxLength={2000} />
        </label>
        <p className="helper">Уточнение войдёт в задание Claude на каждом этапе. Карточки ролей ниже не меняются автоматически.</p>
        <button type="submit" className="button primary">Сохранить уточнение</button>
      </form>
    </>
  );
}

function TeamPlan({ project, update, go }: StepProps) {
  const { catalog, openDialog, closeDialog, toast } = useShell();
  const scenario = scenarioOf(catalog, project.scenario);
  return (
    <Flow
      step={2}
      onBack={() => go('question', 2)}
      aside={<GuideCard title="Я организую работу" copy="Передам цель и материалы каждой роли, свяжу их выводы и остановлюсь, если понадобится ваше решение. Вы обсуждаете задачу со мной, а не управляете агентами." />}
    >
      <p className="eyebrow">3. Команда вокруг вашей задачи</p>
      <h1>Вот кто поможет её разобрать</h1>
      <p className="lead">Не нужно выбирать агентов или раздавать поручения. Посмотрите, зачем нужна каждая роль и что получится вместе.</p>
      <p className="helper">Для проекта «{project.name}» · {scenario.label}.<br />Ваш приоритет: {answerText(catalog, project, 0)}.</p>
      <Assistant>Данных достаточно для первого разбора. Предлагаю эти роли: я свяжу их работу и вернусь к вам с единым результатом. Есть что добавить?</Assistant>
      <TeamCards team={project.team} />
      <section className="team-unified">
        <h3>Вместе — один результат</h3>
        <p>{scenario.stages.map((stage) => stage.title).join(' → ')}. Выводы ролей дополняют друг друга и входят в общий результат проекта.</p>
      </section>
      <details className="spacer-top">
        <summary>Откуда берутся роли, которых нет в CTF?</summary>
        <p>CTF предусматривает подключение готовых ролей, специализацию под предметную область и добавление новой роли с инструкциями, знаниями, памятью и проверками. Одного названия специалиста недостаточно.</p>
        <p>Здесь команда предложена по выбранному направлению и ответам. Claude работает по этой схеме ролей в одном запуске; отдельные агенты во фреймворке не создаются.</p>
      </details>
      {project.team_feedback && <div className="notice spacer-top">Ваше уточнение к команде: {project.team_feedback}</div>}
      <div className="actions">
        <button className="button primary" type="button" onClick={() => go('plan', 2)}>Команда подходит — к плану <Icon name="arrow" /></button>
        <button
          className="button"
          type="button"
          onClick={() => openDialog('Обсудить команду с фасилитатором', (
            <DiscussTeam
              project={project}
              onSave={async (text) => {
                if (await update({ team_feedback: text })) {
                  closeDialog();
                  toast('Уточнение сохранено и войдёт в задание команде.');
                }
              }}
            />
          ))}
        >
          Обсудить состав
        </button>
      </div>
    </Flow>
  );
}

function PlanStep({ project, go, reload }: StepProps) {
  const { catalog, toast } = useShell();
  const socket = useSocket();
  const scenario = scenarioOf(catalog, project.scenario);
  const [confirmed, setConfirmed] = useState(false);
  const [starting, setStarting] = useState(false);
  const guessed = assumptions(catalog, project);

  const start = () => {
    const clientRequestId = crypto.randomUUID();
    const unsubscribe = socket.subscribe((event) => {
      if (event.client_request_id !== clientRequestId) return;
      if (event.type === 'run.accepted') {
        unsubscribe();
        void reload().then(() => navigate({ page: 'project', projectId: project.id, step: 'workspace', question: 0 }));
      } else if (event.type === 'command.error') {
        unsubscribe();
        setStarting(false);
        toast(event.message ?? 'Не удалось начать работу');
      }
    });
    if (!socket.send({ type: 'project.run', project_id: project.id, client_request_id: clientRequestId, kind: 'stage' })) {
      unsubscribe();
      toast('Нет соединения с сервером. Попробуйте через несколько секунд.');
      return;
    }
    setStarting(true);
  };

  return (
    <Flow
      step={3}
      onBack={() => go('team-plan', 2)}
      aside={
        <aside className="plan-aside">
          <div className="guide-card">
            <img src={MEDIA.team} alt="Команда из ролика CoreTeams" />
            <div className="guide-copy">
              <span className="guide-role">Предложенная команда</span>
              <h3>{project.team.map((role) => role.name).join(', ')}</h3>
              <p>Фасилитатор организует передачу результатов и согласование с вами.</p>
              <button className="text-button" type="button" onClick={() => go('team-plan', 2)}>Посмотреть ответственность ролей ↗</button>
            </div>
          </div>
          <div className="plan-outcomes">
            <p className="side-label">Что вы получите</p>
            <ol className="plan-timeline">
              {scenario.stages.map((stage, index) => (
                <li key={stage.title}><span className="num">{index + 1}</span><div><h4>{stage.title}</h4><p>{stage.result}</p></div></li>
              ))}
            </ol>
            <p className="helper">Каждый результат согласуем с вами перед следующим этапом.</p>
          </div>
        </aside>
      }
    >
      <p className="eyebrow">4. План и согласование</p>
      <h1>Вот как мы поняли задачу</h1>
      <p className="lead">Данных достаточно для первого разбора. Проверьте фокус и границы.</p>
      <div className="plan-summary">
        <h3>{project.name}</h3>
        <dl className="spacer-top">
          <div><dt>Ваша цель</dt><dd>{project.problem}</dd></div>
          <div><dt>Фокус</dt><dd>{answerText(catalog, project, 0)}</dd></div>
          <div><dt>Аудитория</dt><dd>{answerText(catalog, project, 1)}</dd></div>
          <div><dt>Границы</dt><dd>{answerText(catalog, project, 2)}</dd></div>
        </dl>
        <details>
          <summary>Контекст и материалы · файлов: {project.files.length}</summary>
          <p>{project.context || 'Дополнительных ограничений нет'}</p>
          <p>{project.files.map((file) => file.name).join(', ') || 'Пока только описание'}</p>
        </details>
        <button className="text-button" type="button" onClick={() => go('context')}>Изменить описание или добавить файлы</button>
      </div>
      {guessed.length > 0 && <p className="helper">Требует проверки: {guessed.join('; ')}.</p>}
      <div className="cost-box">
        <div><b>{scenario.stages[0].time}</b><small>до первого результата</small></div>
        <div><b>3 этапа</b><small>стоимость — по фактическим запускам Claude</small></div>
      </div>
      <p className="helper">Стоимость и время каждого запуска будут видны в рабочем пространстве. Пока вы проверяете результат или отвечаете на вопрос, команда не работает.</p>
      {project.team_feedback && <div className="notice">Уточнение к команде: {project.team_feedback}</div>}
      <label className="checkline">
        <input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
        Да, задача, команда и ограничения указаны верно
      </label>
      <div className="actions">
        <button className="button primary" type="button" disabled={!confirmed || starting || socket.status !== 'connected'} onClick={start}>
          {starting ? 'Передаём задачу команде…' : 'Начать работу команды'} <Icon name="arrow" />
        </button>
      </div>
      {socket.status !== 'connected' && <p className="helper">Подключаемся к серверу…</p>}
    </Flow>
  );
}
