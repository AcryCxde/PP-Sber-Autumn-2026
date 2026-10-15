import { useEffect, useState } from "react";
import { ApiError, ProjectApi } from "./api.ts";
import { emitMetric } from "./metrics.ts";
import { loadProjects, STORAGE_KEY } from "./storage.ts";
import type { Pending, Project } from "./types.ts";

const api = new ProjectApi(
  import.meta.env.VITE_API_BASE_URL ?? "",
  import.meta.env.VITE_PROJECT_API_ENABLED === "true",
);
function initial() {
  try {
    return { projects: loadProjects(localStorage), issue: "" };
  } catch {
    return {
      projects: [] as Project[],
      issue:
        "Не удалось прочитать черновики. Данные не перезаписаны. Проверьте доступ браузера к хранилищу.",
    };
  }
}
export function App() {
  const [state, setState] = useState(initial);
  const [activeId, setActiveId] = useState<string>();
  const [title, setTitle] = useState("");
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const project = state.projects.find((p) => p.id === activeId);
  useEffect(() => {
    const changed = (event: StorageEvent) => {
      if (event.key === STORAGE_KEY || event.key === null)
        setState((s) => ({
          ...s,
          issue:
            "Черновики изменились в другой вкладке. Обновите страницу перед продолжением.",
        }));
    };
    window.addEventListener("storage", changed);
    return () => window.removeEventListener("storage", changed);
  }, []);
  function persist(projects: Project[]) {
    if (state.issue) return false;
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(projects));
      setState({ projects, issue: "" });
      return true;
    } catch {
      setState((s) => ({
        ...s,
        issue:
          "Черновик не удалось записать. Проверьте свободное место и разрешение браузера; обновите страницу перед продолжением.",
      }));
      return false;
    }
  }
  function replace(next: Project) {
    return persist(state.projects.map((p) => (p.id === next.id ? next : p)));
  }
  function create() {
    if (!title.trim()) return;
    const p: Project = {
      id: crypto.randomUUID(),
      title: title.trim(),
      context: "",
      updatedAt: new Date().toISOString(),
    };
    if (persist([p, ...state.projects])) {
      setActiveId(p.id);
      setTitle("");
      setError("");
      setNotice("");
      emitMetric("project_started", { projectId: p.id });
    }
  }
  async function execute(action: Pending["action"]) {
    if (!project || busy || state.issue) return;
    if (!api.available) {
      setError(
        "Сервис пока не подключён. Можно подготовить контекст и вернуться к проекту позже.",
      );
      emitMetric("run_failed", { projectId: project.id, code: "unavailable" });
      return;
    }
    const p = { ...project };
    const pending: Pending = p.pending ?? {
      action,
      requestId: crypto.randomUUID(),
      body:
        action === "create"
          ? { clientProjectId: p.id, title: p.title }
          : action === "start"
            ? { context: p.context }
            : action === "answer"
              ? { questionId: p.run?.question?.id, answer: answer.trim() }
              : { runId: p.run?.id, resultId: p.run?.result?.id },
    };
    if (!replace({ ...p, pending })) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      if (pending.action === "create")
        p.serverId = await api.create(pending.body, pending.requestId, p.id);
      else if (pending.action === "start" && p.serverId) {
        p.run = await api.start(
          p.serverId,
          pending.body,
          pending.requestId,
          p.id,
        );
        p.saved = undefined;
        emitMetric("run_created", { projectId: p.id, runId: p.run.id, requestId: pending.requestId });
      } else if (pending.action === "answer" && p.serverId && p.run) {
        const run = await api.answer(
          p.serverId,
          p.run.id,
          pending.body,
          pending.requestId,
          p.id,
        );
        if (run.id !== p.run.id) throw new Error("run mismatch");
        p.run = run;
        setAnswer("");
      } else if (pending.action === "save" && p.serverId) {
        p.saved = await api.save(
          p.serverId,
          pending.body,
          pending.requestId,
          p.id,
        );
        emitMetric("artifact_saved", {
          projectId: p.id,
          requestId: pending.requestId,
          runId: p.run?.id,
          artifactId: p.saved.artifactId,
        });
      } else throw new Error("invalid state");
      if (p.run?.status === "failed")
        emitMetric("run_failed", {
          projectId: p.id,
          requestId: pending.requestId,
          runId: p.run?.id,
          code: "run_failed",
        });
      p.pending = undefined;
      p.updatedAt = new Date().toISOString();
      replace(p);
      setNotice(
        pending.action === "create"
          ? "Проект зарегистрирован на сервере. Теперь можно запустить задачу."
          : pending.action === "save"
            ? "Сервер подтвердил сохранение результата."
            : "Ответ сервиса получен.",
      );
    } catch (e) {
      const detail =
        e instanceof ApiError
          ? `${e.message} ID запроса: ${e.requestId}`
          : "Не удалось обработать ответ. Повторите прежний запрос.";
      setError(detail);
      emitMetric("run_failed", {
        projectId: p.id,
        requestId: pending.requestId,
        runId: p.run?.id,
        code: e instanceof ApiError ? e.code : "client_error",
      });
    } finally {
      setBusy(false);
    }
  }
  async function refresh() {
    if (!project?.serverId || !project.run || busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const run = await api.run(project.serverId, project.run.id, project.id);
      if (run.id !== project.run.id) throw new Error("run mismatch");
      if (run.status === "failed")
        emitMetric("run_failed", { projectId: project.id, runId: project.run.id, code: "run_failed" });
      replace({ ...project, run, updatedAt: new Date().toISOString() });
    } catch (e) {
      setError(
        e instanceof ApiError
          ? `${e.message} ID запроса: ${e.requestId}`
          : "Сервис вернул другой запуск.",
      );
      emitMetric("run_failed", {
        projectId: project.id,
        runId: project.run.id,
        code: e instanceof ApiError ? e.code : "invalid_response",
      });
    } finally {
      setBusy(false);
    }
  }
  async function download() {
    if (!project?.serverId || !project.saved || busy) return;
    setBusy(true);
    setError("");
    try {
      const blob = await api.zip(
        project.serverId,
        project.saved.artifactId,
        project.id,
      );
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "coreteams-result.zip";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      emitMetric("artifact_downloaded", { projectId: project.id, runId: project.run?.id, artifactId: project.saved.artifactId });
      setNotice("ZIP получен с сервера и передан браузеру для скачивания.");
    } catch (e) {
      setError(
        e instanceof ApiError
          ? `${e.message} ID запроса: ${e.requestId}`
          : "Не удалось скачать ZIP.",
      );
      emitMetric("run_failed", {
        projectId: project.id,
        runId: project.run?.id,
        code: e instanceof ApiError ? e.code : "download_error",
      });
    } finally {
      setBusy(false);
    }
  }
  function back() {
    if (
      project?.run &&
      ["running", "clarification"].includes(project.run.status)
    )
      emitMetric("workspace_left", {
        projectId: project.id,
        runId: project.run.id,
        code: "left_workspace",
      });
    setActiveId(undefined);
    setError("");
    setNotice("");
  }
  const blocked = busy || Boolean(state.issue);
  return (
    <div className="shell">
      <a className="skip-link" href="#main-content">
        Перейти к содержимому
      </a>
      <aside className="sidebar">
        <a
          className="brand"
          href="#"
          onClick={(e) => {
            e.preventDefault();
            if (!busy) back();
          }}
          aria-label="CoreTeams: все проекты"
        >
          <span className="brand-symbol">C</span>CoreTeams
        </a>
        <p className="sidebar-caption">ВАШЕ РАБОЧЕЕ ПРОСТРАНСТВО</p>
        <button
          className={!project ? "nav selected" : "nav"}
          disabled={busy}
          onClick={back}
        >
          Проекты <span>{state.projects.length}</span>
        </button>
        <div className="sidebar-bottom">
          <span className={api.available ? "dot online" : "dot"} />
          {api.available ? "Адаптер настроен" : "Сервис не подключён"}
          <small>Черновики хранятся в этом браузере</small>
        </div>
      </aside>
      <main id="main-content">
        <header>
          <span>Работа над решениями</span>
          <span className="avatar" title="Локальное рабочее пространство">
            К
          </span>
        </header>
        {state.issue ? (
          <div role="alert" className="alert">
            {state.issue}
          </div>
        ) : null}
        {error ? (
          <div role="alert" className="alert">
            {error}
          </div>
        ) : null}
        {notice ? (
          <div role="status" className="notice">
            {notice}
          </div>
        ) : null}
        {!project ? (
          <section className="content">
            <div className="eyebrow">ОТ ИДЕИ К СЛЕДУЮЩЕМУ ШАГУ</div>
            <h1>Ваши проекты</h1>
            <p className="intro">
              Опишите задачу и ограничения. CoreTeams поможет подготовить
              решение, к которому можно вернуться.
            </p>
            <form
              className="create-card"
              onSubmit={(e) => {
                e.preventDefault();
                create();
              }}
            >
              <label htmlFor="project-title">Новый проект</label>
              <div className="input-row">
                <input
                  id="project-title"
                  name="project-title"
                  autoComplete="off"
                  placeholder="Например, выбрать направление для бизнеса…"
                  value={title}
                  maxLength={160}
                  onChange={(e) => setTitle(e.target.value)}
                  disabled={blocked}
                  required
                />
                <button className="primary" disabled={blocked || !title.trim()}>
                  Создать проект <span aria-hidden>↗</span>
                </button>
              </div>
              <small>
                Используйте несекретную задачу. Пароли, персональные данные и
                ключи сюда не добавляйте.
              </small>
            </form>
            <h2 className="list-heading">Последние проекты</h2>
            {state.projects.length ? (
              <div className="project-list">
                {state.projects.map((p) => (
                  <button
                    className="project-card"
                    key={p.id}
                    disabled={busy}
                    onClick={() => {
                      setActiveId(p.id);
                      setAnswer("");
                    }}
                  >
                    <span className="project-icon" aria-hidden>
                      ◇
                    </span>
                    <span>
                      <strong>{p.title}</strong>
                      <small>
                        {p.saved
                          ? "Сохранение подтверждено сервером"
                          : p.run
                            ? "Есть запуск · проверьте статус"
                            : "Черновик контекста"}{" "}
                        · {new Date(p.updatedAt).toLocaleDateString("ru")}
                      </small>
                    </span>
                    <span aria-hidden>→</span>
                  </button>
                ))}
              </div>
            ) : (
              <div className="empty">
                <span aria-hidden>◇</span>
                <h3>Первый проект начинается с вопроса</h3>
                <p>Создайте проект, чтобы собрать контекст в одном месте.</p>
              </div>
            )}
          </section>
        ) : (
          <section className="content">
            <button className="back" onClick={back} disabled={busy}>
              ← Все проекты
            </button>
            <div className="eyebrow">ПРОЕКТ</div>
            <h1>{project.title}</h1>
            <div className="workspace-grid">
              <div>
                <section className="panel">
                  <h2>Контекст задачи</h2>
                  <p>
                    Какое решение нужно принять? Что уже известно, какие есть
                    ограничения и источники?
                  </p>
                  <label htmlFor="context">Задача, факты и ссылки</label>
                  <textarea
                    id="context"
                    name="context"
                    autoComplete="off"
                    value={project.context}
                    rows={8}
                    maxLength={20000}
                    disabled={
                      blocked ||
                      Boolean(project.pending) ||
                      Boolean(project.run)
                    }
                    onChange={(e) =>
                      replace({
                        ...project,
                        context: e.target.value,
                        updatedAt: new Date().toISOString(),
                      })
                    }
                    placeholder="Опишите ситуацию, желаемый результат и критерии выбора…"
                  />
                  <small>
                    Локальный черновик. Передача начнётся только после запуска.
                  </small>
                  <div className="actions">
                    {project.pending ? (
                      <>
                        <p>
                          Есть неподтверждённый запрос. Повтор использует тот же
                          ID и исходные данные.
                        </p>
                        <button
                          className="primary"
                          disabled={blocked}
                          onClick={() => execute(project.pending!.action)}
                        >
                          Повторить запрос
                        </button>
                      </>
                    ) : !project.serverId ? (
                      <button
                        className="primary"
                        disabled={blocked || !project.context.trim()}
                        onClick={() => execute("create")}
                      >
                        Подключить проект к сервису
                      </button>
                    ) : !project.run ? (
                      <button
                        className="primary"
                        disabled={blocked || !project.context.trim()}
                        onClick={() => execute("start")}
                      >
                        Запустить задачу
                      </button>
                    ) : null}
                  </div>
                </section>
                {project.run ? (
                  <section className="panel">
                    <div className="panel-top">
                      <h2>Работа над задачей</h2>
                      <span className="badge">
                        {
                          {
                            running: "В работе",
                            clarification: "Нужен ваш ответ",
                            completed: "Результат готов",
                            failed: "Запуск завершился ошибкой",
                          }[project.run.status]
                        }
                      </span>
                    </div>
                    {project.run.status === "running" ? (
                      <p>
                        Сервис принял запуск. Проверьте статус, чтобы получить
                        следующий шаг.
                      </p>
                    ) : null}
                    {project.run.status === "failed" ? (
                      <p>
                        Сервис сообщил об ошибке запуска. Передайте ID запуска
                        команде интеграции: {project.run.id}
                      </p>
                    ) : null}
                    {project.run.question ? (
                      <form
                        onSubmit={(e) => {
                          e.preventDefault();
                          execute("answer");
                        }}
                      >
                        <p className="question">{project.run.question.text}</p>
                        <label htmlFor="answer">Ваш ответ</label>
                        <textarea
                          id="answer"
                          name="answer"
                          autoComplete="off"
                          rows={4}
                          value={answer}
                          onChange={(e) => setAnswer(e.target.value)}
                          disabled={blocked || Boolean(project.pending)}
                          required
                          maxLength={10000}
                        />
                        <button
                          className="primary"
                          disabled={
                            blocked ||
                            Boolean(project.pending) ||
                            !answer.trim()
                          }
                        >
                          Ответить и продолжить
                        </button>
                      </form>
                    ) : null}
                    {project.run.result ? (
                      <>
                        <h3>{project.run.result.title}</h3>
                        <div className="result">
                          {project.run.result.content}
                        </div>
                        {project.saved ? (
                          <p className="saved">
                            Сервер подтвердил сохранение · версия{" "}
                            {project.saved.version}
                            <br />
                            <small>
                              {new Date(project.saved.savedAt).toLocaleString(
                                "ru",
                              )}
                            </small>
                          </p>
                        ) : (
                          <button
                            className="primary"
                            disabled={blocked || Boolean(project.pending)}
                            onClick={() => execute("save")}
                          >
                            Сохранить результат на сервере
                          </button>
                        )}
                        {project.saved ? (
                          <button
                            className="primary"
                            disabled={blocked}
                            onClick={download}
                          >
                            Скачать ZIP ↓
                          </button>
                        ) : null}
                      </>
                    ) : null}
                    {!project.pending &&
                    ["running", "clarification"].includes(
                      project.run.status,
                    ) ? (
                      <button
                        className="secondary"
                        disabled={blocked}
                        onClick={refresh}
                      >
                        Проверить статус
                      </button>
                    ) : null}
                  </section>
                ) : null}
              </div>
              <aside className="project-note">
                <span className="eyebrow">КАК ЭТО РАБОТАЕТ</span>
                <h2>Контекст остаётся рядом</h2>
                <ol>
                  <li>Опишите свою задачу</li>
                  <li>Запустите работу сервиса</li>
                  <li>Ответьте, если нужно уточнение</li>
                  <li>Проверьте и сохраните результат</li>
                </ol>
                <p>
                  Вы можете вернуться к этому проекту из списка. Сохранение
                  результата на сервере подтверждается отдельно.
                </p>
                {!api.available ? (
                  <div className="service-note">
                    Сервис ещё не подключён. Запуск и ZIP станут доступны после
                    интеграции.
                  </div>
                ) : (
                  <div className="service-note">
                    Настроен адаптер предлагаемого API. Доступность операций
                    подтверждается ответом сервиса.
                  </div>
                )}
              </aside>
            </div>
          </section>
        )}
        <footer>
          CoreTeams · рабочее пространство решений{" "}
          <span>
            {busy
              ? "Ожидаем ответ сервиса…"
              : "Черновики доступны на этом устройстве"}
          </span>
        </footer>
      </main>
    </div>
  );
}
