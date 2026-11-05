import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import {
  api,
  downloadMarkdown,
  exportMarkdown,
  formatCost,
  latestVersion,
  scenarioOf,
  type Project,
  type ProjectPatch,
  type ResultVersion,
  type Role,
} from './api';
import { errorText } from './actions';
import { PHASE_LABELS, useShell } from './context';
import { navigate } from './router';
import { useSocket } from './socket';
import { Assistant, Icon, PhaseTag, ResultPaper, Tag } from './ui';
import { Upload } from './Upload';
import { sequenceDecision } from '../events';

const TOUR_KEY = 'coreteams.tour-seen';

const ERROR_TEXT: Record<string, string> = {
  claude_session_unavailable: 'Контекст Claude недоступен. Повторный запуск начнёт этап заново с полным заданием.',
  gateway_busy: 'Сервер сейчас занят другими запусками.',
};

export function TeamCards({ team }: { team: Role[] }) {
  return (
    <div className="team-cards">
      {team.map((role, index) => (
        <article key={role.name} className="team-card">
          <div className="team-card-heading"><span className="role-symbol">{index + 1}</span><h3>{role.name}</h3></div>
          <p><b>Зачем:</b> {role.why}.</p>
          <dl>
            <div><dt>Результат роли</dt><dd>{role.result}.</dd></div>
            <div>
              <dt>Передаёт дальше</dt>
              <dd>{index < team.length - 1 ? `${team[index + 1].name} использует выводы в своей части.` : 'Фасилитатору — для объединения и согласования с вами.'}</dd>
            </div>
          </dl>
          <small className="role-origin">{role.origin} · роль в задании Claude, не отдельный агент</small>
        </article>
      ))}
    </div>
  );
}

function StatusPanel({ project }: { project: Project }) {
  const { catalog } = useShell();
  const scenario = scenarioOf(catalog, project.scenario);
  const done = project.results.flatMap((versions, stage) =>
    versions.map((item) => `${scenario.stages[stage].title}: версия ${item.version}${item.accepted ? ' одобрена' : ' готова'}.`),
  );
  return (
    <>
      <h3>Работа команды</h3>
      <PhaseTag phase={project.phase} />
      <div className="status-heading"><span>Этап {project.stage + 1} из 3</span></div>
      {project.phase === 'working' && <div className="progress ct-indeterminate" role="progressbar" aria-label="Команда работает"><span /></div>}
      <p className="eta">
        {project.phase === 'waiting' ? 'Команда ждёт вашего ответа и не работает.'
          : project.phase === 'review' ? 'Этап готов. Следующий начнётся только после вашего одобрения.'
            : ['ready', 'done'].includes(project.phase) ? 'Все три этапа завершены.'
              : `Ориентир: ${scenario.stages[project.stage].time} на этап.`}
      </p>
      <section className="status-section">
        <p className="side-label">Кто помогает</p>
        <div className="role-list">
          {project.team.map((role) => (
            <div key={role.name} className={`role-row ${project.phase === 'working' ? 'active' : ''}`}>
              <span className="role-symbol">{role.name[0]}</span>
              <div><b>{role.name}</b><small>{role.result}</small></div>
            </div>
          ))}
        </div>
      </section>
      <section className="status-section">
        <p className="side-label">Стоимость проекта</p>
        <p className="eta">{formatCost(project)}</p>
        <p className="eta">Считается по фактическим запускам Claude.</p>
      </section>
      <details className="status-section">
        <summary>Что уже сделано</summary>
        <ul>{(done.length ? done.slice(-6) : ['Команда получила описание задачи.']).map((item) => <li key={item}>{item}</li>)}</ul>
      </details>
    </>
  );
}

function Materials({ project, onProject, onSave }: {
  project: Project;
  onProject: (project: Project) => void;
  onSave: (context: string) => Promise<void>;
}) {
  const [context, setContext] = useState(project.context);
  return (
    <>
      <p>Всё важное для команды — в одном месте. Дополнения относятся только к проекту «{project.name || 'Новый проект'}».</p>
      <Upload project={project} onProject={onProject} />
      <form onSubmit={(event) => {
        event.preventDefault();
        void onSave(context.trim());
      }}>
        <label className="field">
          <span>Контекст проекта</span>
          <textarea rows={6} maxLength={8000} value={context} onChange={(event) => setContext(event.target.value)} placeholder="Ограничения, договорённости, важные детали" />
        </label>
        <p className="helper">Уже одобренные версии не меняются. Дополнения попадут в следующий результат или доработку.</p>
        <div className="actions"><button type="submit" className="button primary">Сохранить контекст</button></div>
      </form>
    </>
  );
}

function Waiting({ project, onAnswer }: { project: Project; onAnswer: (text: string) => void }) {
  const questions = project.question ?? [];
  const [picked, setPicked] = useState<Record<number, string[]>>({});
  const [note, setNote] = useState('');
  const answer = [
    ...questions.map((question, index) => {
      const chosen = picked[index] ?? [];
      return chosen.length ? (questions.length > 1 ? `${question.text} — ${chosen.join(', ')}` : chosen.join(', ')) : '';
    }),
    note.trim(),
  ].filter(Boolean).join('. ');

  const toggle = (index: number, label: string, multi: boolean) => {
    const current = picked[index] ?? [];
    const next = current.includes(label) ? current.filter((x) => x !== label) : multi ? [...current, label] : [label];
    setPicked({ ...picked, [index]: next });
  };

  return (
    <>
      <Assistant>Команда дошла до развилки. Прежде чем продолжить, уточню ваш выбор.</Assistant>
      <section className="phase-callout">
        <span className="tag amber">Нужен ваш ответ · работа на паузе</span>
        {questions.map((question, index) => (
          <div key={question.text}>
            <h3>{question.header ? `${question.header}: ` : ''}{question.text}</h3>
            <div className="choices" role="group" aria-label="Решение для продолжения">
              {question.options.map((option) => (
                <button
                  key={option.label}
                  className="choice"
                  type="button"
                  aria-pressed={(picked[index] ?? []).includes(option.label)}
                  onClick={() => toggle(index, option.label, question.multi_select)}
                >
                  <span className="check">✓</span>
                  <div>{option.label}{option.description && <small>{option.description}</small>}</div>
                </button>
              ))}
            </div>
          </div>
        ))}
        <label className="field">
          <span>{questions.length ? 'Добавить уточнение' : 'Ваш ответ'}</span>
          <input value={note} onChange={(event) => setNote(event.target.value)} placeholder="Что важно учесть при выборе?" maxLength={1000} />
        </label>
        <div className="actions">
          <button className="button primary" type="button" disabled={!answer} onClick={() => onAnswer(answer)}>Ответить и продолжить</button>
        </div>
        <p className="helper">Пока вы решаете, команда не работает и ничего не тратит.</p>
      </section>
    </>
  );
}

function RevisionForm({ onSubmit, onCancel }: { onSubmit: (text: string) => void; onCancel: () => void }) {
  const [text, setText] = useState('');
  const ref = useRef<HTMLTextAreaElement | null>(null);
  useEffect(() => ref.current?.focus(), []);
  return (
    <form className="revision-box" onSubmit={(event: FormEvent) => {
      event.preventDefault();
      if (text.trim()) onSubmit(text.trim());
    }}>
      <h3>Что изменить в этом результате?</h3>
      <label className="field">
        <span>Ваше замечание</span>
        <textarea ref={ref} required maxLength={3000} value={text} onChange={(event) => setText(event.target.value)} placeholder="Опишите, что не подходит и какого изменения вы ждёте" />
      </label>
      <p className="helper">Предыдущая версия сохранится. Доработка — ещё один запуск Claude по этому этапу.</p>
      <div className="actions">
        <button className="button primary" type="submit">Подготовить новую версию</button>
        <button type="button" className="text-button" onClick={onCancel}>Отмена</button>
      </div>
    </form>
  );
}

export function Workspace({ project, setProject, update, reload }: {
  project: Project;
  setProject: (project: Project) => void;
  update: (patch: ProjectPatch) => Promise<Project | null>;
  reload: () => Promise<void>;
}) {
  const { catalog, toast, openDialog, closeDialog } = useShell();
  const socket = useSocket();
  const scenario = scenarioOf(catalog, project.scenario);
  const [viewedStage, setViewedStage] = useState<number | null>(null);
  const [revising, setRevising] = useState(false);
  const [sending, setSending] = useState(false);
  const [liveLog, setLiveLog] = useState<{ run: string | null; items: string[] }>({ run: null, items: [] });
  const [message, setMessage] = useState('');
  const [tourSeen, setTourSeen] = useState(() => {
    try {
      return window.localStorage.getItem(TOUR_KEY) === '1';
    } catch {
      return true;
    }
  });
  const phase = project.phase;
  const activeRun = project.active_run_id;
  const live = liveLog.run === activeRun ? liveLog.items : [];

  // Ход текущего запуска: подписка на события run и перечитывание проекта по завершении.
  useEffect(() => {
    if (!activeRun || socket.status !== 'connected') return;
    let lastSeq = 0;
    let timer: number | null = null;
    const subscribe = () => socket.send({ type: 'run.subscribe', run_id: activeRun, after_seq: lastSeq });
    const unsubscribe = socket.subscribe((event) => {
      if (event.run_id !== activeRun) return;
      if (event.type === 'replay.complete') {
        if (event.terminal) void reload();
        else timer = window.setTimeout(subscribe, 300);
        return;
      }
      if (typeof event.seq !== 'number') return;
      const decision = sequenceDecision(lastSeq, event.seq);
      if (decision.kind === 'duplicate') return;
      if (decision.kind === 'gap') {
        subscribe();
        return;
      }
      lastSeq = decision.nextSeq;
      if (event.type === 'message.created' && event.content) {
        const text = String(event.content);
        setLiveLog((previous) =>
          previous.run === activeRun ? { run: activeRun, items: [...previous.items, text] } : { run: activeRun, items: [text] },
        );
      }
      if (event.type === 'run.completed' || event.type === 'run.failed' || event.type === 'run.outcome_unknown') {
        void reload();
      }
    });
    subscribe();
    return () => {
      unsubscribe();
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [activeRun, socket, reload]);

  const sendRun = useCallback(
    (kind: 'stage' | 'revision' | 'answer', text?: string) => {
      const clientRequestId = crypto.randomUUID();
      const unsubscribe = socket.subscribe((event) => {
        if (event.client_request_id !== clientRequestId) return;
        unsubscribe();
        setSending(false);
        if (event.type === 'run.accepted') {
          setRevising(false);
          setViewedStage(null);
          void reload();
        } else if (event.type === 'command.error') {
          toast(event.message ?? 'Запуск не начат');
        }
      });
      if (!socket.send({ type: 'project.run', project_id: project.id, client_request_id: clientRequestId, kind, text })) {
        unsubscribe();
        toast('Нет соединения с сервером. Попробуйте через несколько секунд.');
        return;
      }
      setSending(true);
    },
    [project.id, reload, socket, toast],
  );

  const call = async (action: () => Promise<Project>, fallback: string) => {
    try {
      const next = await action();
      setProject(next);
      return next;
    } catch (error) {
      const text = errorText(error, fallback);
      if (text) toast(text);
      return null;
    }
  };

  const approve = async () => {
    const next = await call(() => api.approve(project.id), 'Не удалось одобрить результат');
    if (!next) return;
    setViewedStage(null);
    // Как в v9: одобрение сразу запускает следующий этап.
    if (next.phase === 'idle') sendRun('stage');
  };

  const download = (stages: number[]) => {
    downloadMarkdown(project.name, exportMarkdown(catalog, project, stages));
    toast('Подготовлен файл с результатом проекта.');
  };

  const showVersion = (item: ResultVersion) =>
    openDialog('Сохранённая версия', <ResultPaper version={item.version} accepted={item.accepted} content={item.content} />);

  const openMaterials = () =>
    openDialog('Материалы и контекст', (
      <Materials
        project={project}
        onProject={setProject}
        onSave={async (context) => {
          if (await update({ context })) {
            closeDialog();
            toast('Контекст сохранён для следующего результата.');
          }
        }}
      />
    ));

  const sendNote = async (event: FormEvent) => {
    event.preventDefault();
    const text = message.trim();
    if (!text) return;
    if (await update({ note: text })) {
      setMessage('');
      toast('Уточнение добавлено к проекту.');
    }
  };

  const finished = phase === 'ready' || phase === 'done';
  const stage = viewedStage ?? project.stage;
  const stageInfo = scenario.stages[stage];

  const review = (index: number) => {
    const versions = project.results[index] ?? [];
    const doc = versions.at(-1);
    if (!doc) return null;
    const active = index === project.stage && phase === 'review';
    return (
      <>
        <Assistant>{active ? 'Вариант готов. Посмотрите, подходит ли предложенная логика. Можно принять результат или описать, что изменить.' : 'Это сохранённая версия результата. Она остаётся доступной вместе с проектом.'}</Assistant>
        <ResultPaper version={doc.version} accepted={doc.accepted} content={doc.content} />
        {versions.length > 1 && (
          <details className="spacer-top">
            <summary>Предыдущие версии · {versions.length - 1}</summary>
            {versions.slice(0, -1).map((item) => (
              <div key={item.run_id}><button className="text-button" type="button" onClick={() => showVersion(item)}>Открыть версию {item.version}</button></div>
            ))}
          </details>
        )}
        {active && !revising && (
          <section className="review-actions">
            <h3>Подходит ли вам этот результат?</h3>
            <p>{index < 2 ? 'Если всё верно, одобрите его. После этого команда начнёт следующий этап.' : 'Примите результат, чтобы завершить проект или продолжить его развитие.'}</p>
            <div className="actions">
              <button className="button primary" type="button" disabled={sending} onClick={() => void approve()}>{scenario.stages[index].next} <Icon name="arrow" /></button>
              <button className="button" type="button" onClick={() => setRevising(true)}>Нужно изменить</button>
            </div>
          </section>
        )}
        {(active || (finished && index === 2)) && revising && (
          <RevisionForm onSubmit={(text) => sendRun('revision', text)} onCancel={() => setRevising(false)} />
        )}
        <div className="actions">
          <button className="button" type="button" onClick={() => download([index])}><Icon name="download" /> Скачать этот результат</button>
        </div>
      </>
    );
  };

  let body;
  if (viewedStage !== null && latestVersion(project, viewedStage)) {
    body = (
      <>
        {review(viewedStage)}
        {finished && <div className="actions"><button className="button primary" type="button" onClick={() => setViewedStage(null)}>К итогам проекта</button></div>}
      </>
    );
  } else if (finished) {
    body = (
      <Complete
        project={project}
        onOpen={setViewedStage}
        onDownload={() => download([0, 1, 2])}
        update={update}
        onFinish={async () => {
          if (await call(() => api.finish(project.id), 'Не удалось завершить проект')) navigate({ page: 'profile' });
        }}
        onReopen={async () => {
          if (await call(() => api.reopen(project.id), 'Не удалось открыть проект')) {
            setViewedStage(2);
            setRevising(true);
          }
        }}
      />
    );
  } else if (phase === 'waiting') {
    body = <Waiting project={project} onAnswer={(text) => sendRun('answer', text)} />;
  } else if (phase === 'review') {
    body = review(project.stage);
  } else if (phase === 'failed' || phase === 'idle') {
    body = (
      <>
        <Assistant>
          {phase === 'failed'
            ? `Запуск этапа не удался. ${ERROR_TEXT[project.error_code ?? ''] ?? 'Можно попробовать ещё раз: задание этапа соберётся заново.'}`
            : 'Предыдущий результат одобрен. Следующий этап готов к запуску.'}
        </Assistant>
        <div className="actions">
          <button className="button primary" type="button" disabled={sending || socket.status !== 'connected'} onClick={() => sendRun('stage')}>
            {phase === 'failed' ? 'Повторить этап' : `Начать: ${stageInfo.title.toLowerCase()}`} <Icon name="arrow" />
          </button>
        </div>
        {latestVersion(project, project.stage) && <p className="helper">Последняя готовая версия этапа сохранена — её можно открыть слева.</p>}
      </>
    );
  } else {
    body = (
      <>
        <Assistant>Команда работает по согласованному плану: {project.team.map((role) => role.name.toLowerCase()).join(' → ')}. Я передаю контекст между ролями и верну вам единый результат.</Assistant>
        <ul className="work-steps">
          <li className="done"><span className="state-icon">✓</span><div>Уточняем общее направление<small>{project.stage === 0 ? 'Фасилитатор передал цель, контекст и границы' : 'Фасилитатор передал одобренный результат предыдущего этапа'}</small></div></li>
          {project.team.map((role, index) => (
            <li key={role.name} className="active"><span className="state-icon">◉</span><div>{role.name}<small>{role.result}{index ? ' · с учётом выводов предыдущей роли' : ' · на основе контекста проекта'}</small></div></li>
          ))}
        </ul>
        {live.length > 0 && (
          <details className="spacer-top" open>
            <summary>Ход работы · сообщений: {live.length}</summary>
            {live.slice(-3).map((text, index) => <p key={index} className="helper">{text.length > 400 ? `${text.slice(0, 400)}…` : text}</p>)}
          </details>
        )}
        <p className="quiet-note">Можно заниматься своими делами. Если понадобится решение, вопрос появится здесь. Работа продолжится, даже если закрыть страницу.</p>
      </>
    );
  }

  return (
    <div className="workspace">
      <header className="workspace-top">
        <div>
          <p className="eyebrow">Ваш проект{project.example ? ' · учебный пример' : ''}</p>
          <h1>{project.name}</h1>
          <p className="saved">Сохранено в вашем аккаунте · <a href="#profile">Мои проекты ↗</a></p>
        </div>
        <div className="workspace-tools">
          <button className="button" type="button" onClick={openMaterials}><Icon name="plus" /> Материалы и контекст</button>
          <button className="button" type="button" onClick={() => openDialog('Команда и ответственность', <><TeamCards team={project.team} /><StatusPanel project={project} /></>)}>
            <Icon name="team" /> Команда
          </button>
        </div>
      </header>
      <div className="workspace-grid">
        <aside className="project-side">
          <p className="side-label">Путь проекта</p>
          <ol className="stage-nav">
            {scenario.stages.map((item, index) => {
              const accepted = latestVersion(project, index)?.accepted;
              const state = index > project.stage ? 'Следом' : index < project.stage || finished ? 'Одобрено' : PHASE_LABELS[phase];
              return (
                <li key={item.title}>
                  <button
                    type="button"
                    className={stage === index ? 'active' : ''}
                    disabled={index > project.stage}
                    aria-current={stage === index ? 'step' : undefined}
                    onClick={() => {
                      setRevising(false);
                      setViewedStage(latestVersion(project, index) && (index !== project.stage || finished) ? index : null);
                    }}
                  >
                    <span className="stage-marker">{accepted ? '✓' : String(index + 1).padStart(2, '0')}</span>
                    <span>{item.short}<small>{state}</small></span>
                  </button>
                </li>
              );
            })}
          </ol>
          <div className="side-summary"><p className="side-label">Зачем работаем</p><p>{project.problem}</p></div>
          <section className="side-materials">
            <p className="side-label">Память проекта</p>
            <p className="side-summary">Файлов: {project.files.length}<br />{project.context ? 'Контекст добавлен' : 'Можно добавить контекст'}</p>
            <button className="text-button" type="button" onClick={openMaterials}>Открыть материалы ↗</button>
          </section>
        </aside>
        <section className="working-main">
          {!tourSeen && (
            <div className="workspace-tour">
              <div><b>Вы в рабочем пространстве.</b> Здесь появятся вопросы и результаты. Этапы — слева, работа команды — справа. На узком экране откройте кнопку «Команда».</div>
              <button type="button" aria-label="Закрыть подсказку" onClick={() => {
                setTourSeen(true);
                try {
                  window.localStorage.setItem(TOUR_KEY, '1');
                } catch {
                  // без хранилища подсказка просто покажется снова
                }
              }}>×</button>
            </div>
          )}
          <p className="eyebrow">{finished && viewedStage === null ? 'Все этапы пройдены' : `Этап ${stage + 1} из 3`}</p>
          <h2>{finished && viewedStage === null ? 'Единый результат' : stageInfo.title}</h2>
          <div className="compact-status">
            <div className="status-heading"><PhaseTag phase={phase} /></div>
            <p className="eta">{formatCost(project)}</p>
          </div>
          <div id="work-content">{body}</div>
          {project.notes.length > 0 && (
            <div className="conversation-history">
              {project.notes.slice(-4).map((note, index) => (
                <div key={index} className="conversation-entry"><small>Вы</small>{note}</div>
              ))}
            </div>
          )}
          <form className="composer" onSubmit={(event) => void sendNote(event)}>
            <label htmlFor="message">Есть уточнение к задаче?</label>
            <div className="composer-row">
              <textarea id="message" rows={1} placeholder="Напишите, что команде важно учесть…" maxLength={3000} required value={message} onChange={(event) => setMessage(event.target.value)} />
              <button className="button" type="submit" aria-label="Передать уточнение помощнику"><Icon name="arrow" /></button>
            </div>
            <p className="helper">Уточнение войдёт в задание следующего этапа или доработки. Одобренные версии не меняются.</p>
            <button className="text-button" type="button" onClick={openMaterials}>+ Добавить файл или контекст</button>
          </form>
        </section>
        <aside className="status-panel"><StatusPanel project={project} /></aside>
      </div>
    </div>
  );
}

function Complete({ project, onOpen, onDownload, update, onFinish, onReopen }: {
  project: Project;
  onOpen: (stage: number) => void;
  onDownload: () => void;
  update: (patch: ProjectPatch) => Promise<Project | null>;
  onFinish: () => Promise<void>;
  onReopen: () => Promise<void>;
}) {
  const { catalog, toast } = useShell();
  const scenario = scenarioOf(catalog, project.scenario);
  const [note, setNote] = useState(project.feedback_note);
  return (
    <>
      <div className="completion-hero"><Icon name="check" /><div><h3>Проект готов к следующему шагу</h3><p>Три результата собраны и одобрены вами.</p></div></div>
      <Assistant>
        {project.scenario === 'course'
          ? 'Теперь можно проверить первый модуль на небольшой группе учеников. Материалы сохранены в проекте.'
          : 'У вас есть план для проверки. Сверьте его с реальными данными: команда опиралась на ваше описание и материалы и не проводила исследование. Материалы сохранены в проекте.'}
      </Assistant>
      <div className="choices">
        {scenario.stages.map((item, index) => (
          <button key={item.title} className="choice" type="button" onClick={() => onOpen(index)}>
            <Icon name="file" />
            <div>{item.title}<small>Одобрено · версия {latestVersion(project, index)?.version ?? 1}</small></div>
            <span style={{ marginLeft: 'auto' }}>↗</span>
          </button>
        ))}
      </div>
      <div className="actions"><button className="button primary" type="button" onClick={onDownload}><Icon name="download" /> Скачать всё одним файлом</button></div>
      <p className="export-note">Markdown (.md): результаты, ваши ответы и контекст проекта.</p>
      <section className="review-actions">
        <h3>Помог ли результат сделать следующий шаг?</h3>
        <p>Ответ необязателен. Он сохранится в проекте.</p>
        <div className="feedback-row" role="group" aria-label="Оценка результата">
          {([['yes', 'Да, могу действовать'], ['partly', 'Нужна доработка'], ['no', 'Не помог']] as const).map(([value, label]) => (
            <button key={value} className="choice" type="button" aria-pressed={project.feedback === value} onClick={() => void update({ feedback: value })}>{label}</button>
          ))}
        </div>
        <label className="field">
          <span>Что стоит улучшить? <span className="optional">· необязательно</span></span>
          <textarea placeholder="Ваш комментарий" maxLength={2000} value={note} onChange={(event) => setNote(event.target.value)} onBlur={() => {
            if (note !== project.feedback_note) void update({ feedback_note: note });
          }} />
        </label>
        <p className="helper">{project.feedback ? 'Спасибо, оценка сохранена.' : 'Можно оставить отзыв или сразу завершить проект.'}</p>
      </section>
      <div className="actions">
        <button className="button primary" type="button" onClick={() => void onFinish()}>{project.status === 'done' ? 'Вернуться в мои проекты' : 'Завершить и открыть мои проекты'}</button>
        <button className="button" type="button" onClick={() => void onReopen()}>Продолжить доработку</button>
      </div>
      <details className="spacer-top">
        <summary>Развитие проекта: масштабирование и инвестор</summary>
        <p>Эти направления пока не подключены. Можно сохранить пожелание как следующий шаг проекта — публикации не будет.</p>
        {['Масштабировать проект', 'Подготовить проект для инвестора'].map((value) => (
          <button key={value} className="button" type="button" onClick={async () => {
            if (await update({ note: `Следующий шаг: ${value}` })) toast('Пожелание сохранено в проекте. Ничего не опубликовано.');
          }}>{value}</button>
        ))}
      </details>
      <p className="helper"><Tag>{formatCost(project)}</Tag></p>
    </>
  );
}
