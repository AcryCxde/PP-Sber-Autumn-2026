import { useEffect, useRef, useState } from "react";
import { CtfApi } from "./api.ts";
import {
  IDENTITY,
  KEY,
  ROLES,
  QUESTIONS,
  exampleProject,
  latest,
  mergeRun,
  newProject,
  projectPayload,
  readProjects,
  type Project,
  type Pending,
  type Run,
  type Artifact,
} from "./domain.ts";
import { getFile, keepFile, removeFile } from "./files.ts";
import { JourneyPanel } from "./JourneyPanel.tsx";
import { JourneyRecorder, type EventName, type Fields } from "./journey.ts";

const api = new CtfApi(
  import.meta.env.VITE_CTF_API_URL || "",
  import.meta.env.VITE_CTF_V2_ENABLED === "true",
);
const labels = {
  running: "Команда работает",
  clarification: "Нужен ваш ответ",
  review: "Результат на проверке",
  completed: "Работа завершена",
  failed: "Запуск завершился ошибкой",
};
const STEP_NAMES = ["Задача и материалы", "Вопросы", "Команда", "План"];
const TOUR = [
  {
    title: "Начните с решения, которое вам нужно",
    body: "Например: хочу открыть кофейню у университета, но не знаю, что проверить до аренды. Добавьте факты, ссылки, расчёты или заметки. Идеальная формулировка не нужна.",
    detail:
      "Вы передаёте контекст. Он остаётся рядом с обсуждением и результатами.",
  },
  {
    title: "Фасилитатор задаёт уточняющие вопросы",
    body: "Какое решение вы пока не готовы принимать? Для кого нужен результат? Какие ограничения важно соблюдать? Ответы помогают не подменять вашу задачу предположением.",
    detail:
      "Вы отвечаете одному фасилитатору, а не управляете каждым AI-агентом.",
  },
  {
    title: "Посмотрите и измените состав команды",
    body: "Исследователь собирает основания. Аналитик сравнивает варианты. Предметный специалист проверяет риски. Редактор объединяет выводы. Вы можете исключить роль или указать недостающую экспертизу.",
    detail:
      "Это AI-роли. Они не означают участие реальных людей. Доступность ролей должен подтвердить CTF.",
  },
  {
    title: "Согласуйте этапы и ожидаемые результаты",
    body: "Сначала карта задачи, затем варианты решения, затем итоговый документ. Проверьте границы, добавьте этап или уточните результат. Работа запускается отдельно после вашего подтверждения.",
    detail: "В рабочем пространстве каждый этап имеет свои артефакты и версии.",
  },
  {
    title: "Проверяйте результат и возвращайтесь к версиям",
    body: "Слева выбирайте этап. По центру обсуждайте вопросы и замечания. Справа открывайте артефакты, сравнивайте версии и подтверждайте подходящий результат.",
    detail:
      "Далее можно открыть готовый учебный пример. Это статический образец пути, не выполненная работа CTF.",
  },
];
function initial() {
  try {
    const raw = localStorage.getItem(KEY);
    return { projects: readProjects(raw), raw, issue: "" };
  } catch {
    return {
      projects: [] as Project[],
      raw: null,
      issue:
        "Не удалось прочитать черновики. Существующие данные не перезаписаны. Проверьте хранилище браузера.",
    };
  }
}
function bytes(n: number) {
  return n < 1024
    ? `${n} Б`
    : n < 1048576
      ? `${(n / 1024).toFixed(1)} КБ`
      : `${(n / 1048576).toFixed(1)} МБ`;
}
function Document({ content }: { content: string }) {
  return (
    <div className="document">
      {content.split("\n").map((line, i) =>
        line.startsWith("# ") ? (
          <h3 key={i}>{line.slice(2)}</h3>
        ) : line.startsWith("## ") ? (
          <h4 key={i}>{line.slice(3)}</h4>
        ) : line.startsWith("- ") ? (
          <p className="document-item" key={i}>
            • {line.slice(2)}
          </p>
        ) : (
          <p key={i}>{line || "\u00a0"}</p>
        ),
      )}
    </div>
  );
}
function downloadText(a: Artifact) {
  const link = document.createElement("a"),
    url = URL.createObjectURL(
      new Blob([a.content], { type: "text/markdown;charset=utf-8" }),
    );
  link.href = url;
  link.download = `coreteams-${a.stageId.replace(/[^a-z0-9_-]/gi, "_")}-v${a.version}.md`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export function App() {
  const [study] = useState(
    () =>
      new JourneyRecorder({
        getItem: (key) => window.localStorage.getItem(key),
        setItem: (key, value) => window.localStorage.setItem(key, value),
      }),
  );
  const [loaded] = useState(initial),
    [projects, setProjects] = useState(loaded.projects),
    [issue, setIssue] = useState(loaded.issue);
  const projectsRef = useRef(projects),
    rawRef = useRef(loaded.raw),
    busyRef = useRef(false);
  const [view, setView] = useState<"home" | "tour" | "onboard" | "workspace">(
      "home",
    ),
    [activeId, setActiveId] = useState<string>(),
    [sample, setSample] = useState(false),
    [tourStep, setTourStep] = useState(0),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const [stageId, setStageId] = useState("understand"),
    [documentId, setDocumentId] = useState(""),
    [artifactId, setArtifactId] = useState(""),
    [sideTab, setSideTab] = useState<"artifacts" | "team" | "usage">(
      "artifacts",
    ),
    [answer, setAnswer] = useState(""),
    [preview, setPreview] = useState<{ name: string; text: string } | null>(
      null,
    ),
    [poll, setPoll] = useState(true);
  const previewOpener = useRef<HTMLElement | null>(null);
  const [example] = useState(exampleProject);
  const p = sample ? example : projects.find((x) => x.id === activeId);
  const block = busy || Boolean(issue);
  const run = p?.run;
  const studyContext = useRef({ projectId: p?.id, sample });
  studyContext.current = { projectId: p?.id, sample };
  function track(name: EventName, fields: Fields = {}, dedupe?: string) {
    if (!sample) study.record(name, { projectId: p?.id, ...fields }, dedupe);
  }
  function observeRun(projectId: string, incoming: Run) {
    const session = study.active();
    if (
      sample ||
      !session ||
      session.phase === "baseline" ||
      (session.projectId && session.projectId !== projectId)
    )
      return;
    study.record(
      "run_received",
      { projectId, runId: incoming.id },
      incoming.id,
    );
    study.record(
      "run_status",
      { projectId, runId: incoming.id, status: incoming.status },
      `${incoming.id}:${incoming.revision}`,
    );
    if (incoming.usage)
      study.record(
        "usage_received",
        { projectId, runId: incoming.id, ...incoming.usage },
        `${incoming.id}:${incoming.revision}`,
      );
    for (const artifact of incoming.artifacts) {
      if (artifact.saved)
        study.record(
          "artifact_saved",
          {
            projectId,
            runId: incoming.id,
            artifactId: artifact.id,
            stageId: artifact.stageId,
          },
          artifact.id,
        );
      if (artifact.approved)
        study.record(
          "artifact_accepted",
          { projectId, runId: incoming.id, artifactId: artifact.id },
          artifact.id,
        );
    }
    if (incoming.status === "running" && study.active()?.mode === "active")
      study.mode("wait");
    else if (incoming.status !== "running" && study.active()?.mode === "wait")
      study.mode("active");
  }
  useEffect(() => {
    api.onMetric = (name, fields) => {
      const context = studyContext.current;
      if (!context.sample)
        study.record(name, { projectId: context.projectId, ...fields });
    };
    const timer = setInterval(() => study.checkpoint(), 5000);
    const hidden = () => {
      if (document.hidden && study.active()?.phase !== "baseline")
        study.mode("pause");
    };
    const leave = () => study.mode("unobserved");
    document.addEventListener("visibilitychange", hidden);
    window.addEventListener("pagehide", leave);
    return () => {
      api.onMetric = undefined;
      clearInterval(timer);
      document.removeEventListener("visibilitychange", hidden);
      window.removeEventListener("pagehide", leave);
    };
  }, [study]);
  useEffect(() => {
    if (sample) {
      study.record("sample_viewed");
      return;
    }
    if (view === "tour") study.record("guide_viewed", { step: tourStep });
    if (view === "onboard" && p) track("preparation_step", { step: p.step });
    if (view === "workspace" && p) {
      track("workspace_entered", {}, `${p.id}:${run?.id || "draft"}`);
      if (run?.status === "clarification" && run.question)
        track(
          "clarification_shown",
          { runId: run.id, questionId: run.question.id },
          run.question.id,
        );
    }
  }, [view, tourStep, p?.id, p?.step, run?.id, run?.question?.id, sample]);
  useEffect(() => {
    setAnswer(
      p?.answerDraft?.questionId === run?.question?.id
        ? p?.answerDraft?.text || ""
        : "",
    );
  }, [p?.id, run?.question?.id]);
  function save(next: Project[]) {
    if (issue) return false;
    try {
      if (localStorage.getItem(KEY) !== rawRef.current) {
        setIssue(
          "Черновики изменились в другой вкладке. Обновите страницу перед продолжением.",
        );
        return false;
      }
      const raw = JSON.stringify({ version: 2, projects: next });
      localStorage.setItem(KEY, raw);
      rawRef.current = raw;
      projectsRef.current = next;
      setProjects(next);
      return true;
    } catch {
      setIssue(
        "Не удалось записать черновик. Проверьте место и разрешение браузера. Файлы не отправлялись автоматически.",
      );
      return false;
    }
  }
  function update(next: Project) {
    return save(
      projectsRef.current.map((x) =>
        x.id === next.id ? { ...next, updatedAt: new Date().toISOString() } : x,
      ),
    );
  }
  function patch(changes: Partial<Project>) {
    if (!p || sample) return;
    if (update({ ...p, ...changes })) {
      if (changes.team) track("team_changed", { count: changes.team.length });
      if (changes.stages)
        track(
          "plan_changed",
          { count: changes.stages.length },
          `${p.id}:plan-${changes.stages.length}`,
        );
    }
  }
  function startProject() {
    const next = newProject();
    if (save([next, ...projectsRef.current])) {
      study.record("project_created", { projectId: next.id });
      setSample(false);
      setActiveId(next.id);
      setView("onboard");
      setStageId(next.stages[0].id);
      setArtifactId("");
      setError("");
      setNotice("");
    }
  }
  function begin() {
    try {
      if (localStorage.getItem(`${KEY}:learned`) === "true") {
        startProject();
        return;
      }
    } catch {
      /* The guide remains available if browser storage is unavailable. */
    }
    setTourStep(0);
    setView("tour");
    setSample(false);
  }
  function finishTour() {
    try {
      localStorage.setItem(`${KEY}:learned`, "true");
    } catch {}
    startProject();
  }
  function openProject(project: Project) {
    study.record("project_reopened", { projectId: project.id });
    setActiveId(project.id);
    setSample(false);
    setView(project.step === 4 ? "workspace" : "onboard");
    setStageId(project.run?.stageId || project.stages[0].id);
    setArtifactId("");
    setAnswer("");
    setError("");
    setNotice("");
    setPoll(true);
  }
  function home() {
    if (busy) return;
    if (view === "workspace") track("workspace_left", { runId: run?.id });
    setSample(false);
    setView("home");
    setError("");
    setNotice("");
  }
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === KEY || e.key === null)
        setIssue(
          "Черновики изменились в другой вкладке. Обновите страницу перед продолжением.",
        );
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);
  function working(flag: boolean) {
    busyRef.current = flag;
    setBusy(flag);
  }
  async function upload(files: FileList | null) {
    if (!p || !files || sample || block || p.run || p.pending) return;
    working(true);
    setError("");
    const kept = [];
    try {
      for (const file of Array.from(files))
        kept.push(await keepFile(p.id, file));
      if (!update({ ...p, files: [...p.files, ...kept] })) {
        for (const f of kept) await removeFile(p.id, f.id);
      } else track("file_added", { count: kept.length });
    } catch (e) {
      setError(
        e instanceof Error ? e.message : "Не удалось добавить материалы",
      );
    } finally {
      working(false);
    }
  }
  async function inspectFile(id: string) {
    if (!p) return;
    previewOpener.current = document.activeElement as HTMLElement;
    try {
      const f = p.files.find((f) => f.id === id)!;
      const blob = await getFile(p.id, id);
      const isText =
        /\.(txt|md|csv|json|tsv)$/i.test(f.name) || /^text\//i.test(f.type);
      setPreview({
        name: f.name,
        text: isText
          ? await blob.slice(0, 16000).text()
          : `${f.name}\n${bytes(f.size)}\nФайл сохранён полностью в этом браузере. Предпросмотр этого формата не встроен. Разбор содержимого требует подключённого CTF. ${f.uploadedId ? "Получение файла сервером подтверждено." : "Файл ещё не передан сервису."}`,
      });
    } catch (e) {
      setError(String(e));
    }
  }
  useEffect(() => {
    if (!preview) return;
    const close =
      document.querySelector<HTMLButtonElement>(".file-modal button");
    close?.focus();
    const keys = (e: KeyboardEvent) => {
      if (e.key === "Escape") setPreview(null);
      if (e.key === "Tab") {
        e.preventDefault();
        close?.focus();
      }
    };
    document.addEventListener("keydown", keys);
    return () => {
      document.removeEventListener("keydown", keys);
      previewOpener.current?.focus();
    };
  }, [preview]);
  async function deleteFile(id: string) {
    if (!p || sample || block || p.run || p.pending) return;
    try {
      if (update({ ...p, files: p.files.filter((f) => f.id !== id) })) {
        await removeFile(p.id, id);
        track("file_removed", { count: 1 });
      }
    } catch (e) {
      setError(String(e));
    }
  }
  async function perform(
    action: Pending["action"],
    body: Record<string, unknown>,
    targetId?: string,
  ) {
    if (!p || sample || block) return;
    setError("");
    setNotice("");
    if (!api.available) {
      track("request_failed", { status: "unavailable" });
      setError(
        "CTF пока не подключён. Ваши материалы, ответы, команда и план сохранены локально. Это не запуск AI-команды.",
      );
      return;
    }
    let next = { ...p };
    const pending = next.pending || {
      action,
      id: crypto.randomUUID(),
      body,
      targetId,
    };
    next.pending = pending;
    if (!update(next)) return;
    if (pending.action === "answer")
      track(
        "answer_sent",
        { runId: next.run?.id, requestId: pending.id },
        pending.id,
      );
    if (pending.action === "message")
      track(
        "message_submitted",
        { runId: next.run?.id, requestId: pending.id },
        pending.id,
      );
    if (
      study.active()?.phase !== "baseline" &&
      study.active()?.projectId === p.id &&
      study.active()?.mode === "active"
    )
      study.mode("wait");
    working(true);
    try {
      let result: Run;
      if (pending.action === "start") {
        if (!next.serverId) {
          next.serverId = await api.create(pending.body, `${next.id}:create`);
          if (!update(next))
            throw new Error(
              "Нельзя сохранить ID сервера: продолжение остановлено",
            );
        }
        const serverId = next.serverId;
        if (!serverId) throw new Error("Нет подтверждённого ID проекта");
        for (const file of next.files.filter((f) => !f.uploadedId)) {
          const blob = await getFile(next.id, file.id);
          const uploadedId = await api.upload(
            serverId,
            file.id,
            file.name,
            blob,
            file.sha256,
          );
          next = {
            ...next,
            files: next.files.map((f) =>
              f.id === file.id ? { ...f, uploadedId } : f,
            ),
          };
          if (!update(next))
            throw new Error("Нельзя сохранить подтверждение загрузки");
        }
        result = await api.run(
          `projects/${encodeURIComponent(serverId)}/runs`,
          { ...pending.body, fileIds: next.files.map((f) => f.uploadedId) },
          pending.id,
        );
      } else {
        if (!next.serverId || !next.run)
          throw new Error("Нет подтверждённого запуска");
        const prefix = `projects/${encodeURIComponent(next.serverId)}/runs/${encodeURIComponent(next.run.id)}`;
        const route =
          pending.action === "answer"
            ? `${prefix}/answers`
            : pending.action === "message"
              ? `${prefix}/messages`
              : `${prefix}/artifacts/${encodeURIComponent(pending.targetId || "")}/approve`;
        result = await api.run(route, pending.body, pending.id);
        if (result.id !== next.run.id)
          throw new Error("Ответ относится к другому запуску");
      }
      const current = projectsRef.current.find((x) => x.id === next.id) || next;
      next = mergeRun(
        { ...current, serverId: next.serverId, files: next.files },
        result,
      );
      next.pending = undefined;
      next.draftMessage = "";
      next.answerDraft = undefined;
      if (!update(next)) throw new Error("Ответ не сохранён в браузере");
      observeRun(next.id, result);
      if (pending.action === "answer")
        track(
          "answer_acknowledged",
          { runId: result.id, requestId: pending.id },
          pending.id,
        );
      if (pending.action === "message")
        track(
          "message_acknowledged",
          { runId: result.id, requestId: pending.id },
          pending.id,
        );
      setAnswer("");
      setPoll(true);
      setNotice(
        "Ответ сервиса получен. Сохранение каждого артефакта подтверждается отдельно.",
      );
    } catch (e) {
      if (
        study.active()?.phase !== "baseline" &&
        study.active()?.projectId === p.id &&
        study.active()?.mode === "wait"
      )
        study.mode("active");
      track("operation_failed", {
        requestId: pending.id,
        status: "unconfirmed",
      });
      setError(
        `${e instanceof Error ? e.message : "Не удалось обработать ответ"} ID запроса: ${pending.id}`,
      );
    } finally {
      working(false);
    }
  }
  async function refresh() {
    if (!p?.serverId || !run || sample || busyRef.current || issue) return;
    setError("");
    try {
      const nextRun = await api.run(
        `projects/${encodeURIComponent(p.serverId)}/runs/${encodeURIComponent(run.id)}`,
      );
      if (nextRun.id !== run.id) throw new Error("Другой ID запуска");
      const current = projectsRef.current.find((x) => x.id === p.id);
      if (current && update(mergeRun(current, nextRun)))
        observeRun(current.id, nextRun);
      setPoll(true);
    } catch (e) {
      setPoll(false);
      setError(
        `Статус не обновлён: ${e instanceof Error ? e.message : String(e)}. Можно проверить вручную.`,
      );
    }
  }
  async function downloadZip() {
    if (!p?.serverId || !run || !a?.saved || sample || block) return;
    working(true);
    setError("");
    try {
      const blob = await api.zip(
          `projects/${encodeURIComponent(p.serverId)}/runs/${encodeURIComponent(run.id)}/artifacts/${encodeURIComponent(a.id)}/zip`,
        ),
        url = URL.createObjectURL(blob),
        link = document.createElement("a");
      link.href = url;
      link.download = `coreteams-v${a.version}.zip`;
      link.click();
      track("zip_download_handed", { runId: run.id, artifactId: a.id });
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      setNotice(
        "ZIP получен с сервера и передан браузеру. Сохранение на диске выполняет браузер.",
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "Не удалось скачать ZIP");
    } finally {
      working(false);
    }
  }
  useEffect(() => {
    if (
      sample ||
      !api.available ||
      !p?.serverId ||
      !run ||
      run.status !== "running" ||
      p.pending ||
      !poll ||
      issue
    )
      return;
    let stopped = false,
      inFlight = false;
    const timer = setInterval(async () => {
      if (stopped || busyRef.current || inFlight) return;
      inFlight = true;
      try {
        const incoming = await api.run(
          `projects/${encodeURIComponent(p.serverId!)}/runs/${encodeURIComponent(run.id)}`,
        );
        if (stopped) return;
        if (incoming.id !== run.id) throw new Error("Другой ID запуска");
        const current = projectsRef.current.find((x) => x.id === p.id);
        if (current && update(mergeRun(current, incoming)))
          observeRun(current.id, incoming);
      } catch (e) {
        if (!stopped) {
          setPoll(false);
          setError(
            `Автообновление остановлено: ${e instanceof Error ? e.message : String(e)}. Проверьте статус вручную.`,
          );
        }
      } finally {
        inFlight = false;
      }
    }, 2500);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [
    p?.id,
    p?.serverId,
    run?.id,
    run?.status,
    Boolean(p?.pending),
    sample,
    poll,
    issue,
  ]);
  const selectedStage = p?.stages.find((s) => s.id === stageId) || p?.stages[0];
  const stageArtifacts =
    run?.artifacts.filter((a) => a.stageId === selectedStage?.id) || [];
  const documents = Array.from(
    new Map(stageArtifacts.map((a) => [a.documentId, a])).values(),
  );
  const activeDocument =
    documents.find((d) => d.documentId === documentId) || documents[0];
  const versions =
    stageArtifacts
      .filter((a) => a.documentId === activeDocument?.documentId)
      .sort((a, b) => b.version - a.version) || [];
  const a = versions.find((a) => a.id === artifactId) || versions[0];
  useEffect(() => {
    if (!sample && view === "workspace" && sideTab === "artifacts" && a) {
      track(
        "result_shown",
        { runId: run?.id, artifactId: a.id, stageId: a.stageId },
        a.id,
      );
      track("artifact_viewed", {
        runId: run?.id,
        artifactId: a.id,
        stageId: a.stageId,
      });
    }
  }, [view, p?.id, sample, sideTab, a?.id]);
  function team() {
    return (
      <>
        <div className="role-card">
          <strong>Фасилитатор</strong>
          <p>Связывает работу ролей и обсуждает с вами вопросы и результаты.</p>
        </div>
        {ROLES.filter((r) => p?.team.includes(r.id)).map((r) => (
          <div className="role-card" key={r.id}>
            <strong>{r.name}</strong>
            <p>{r.purpose}</p>
            <small>Результат: {r.output}</small>
          </div>
        ))}
        {p?.teamNote ? (
          <p className="note">Ваше уточнение: {p.teamNote}</p>
        ) : null}
        <p className="muted">
          {sample
            ? "Состав учебного примера. CTF не запускался."
            : "Выбранные AI-роли — часть согласуемого плана. Их доступность и назначение должен подтвердить backend."}
        </p>
      </>
    );
  }
  function fileList() {
    return (
      <ul className="files">
        {p?.files.map((f) => (
          <li key={f.id}>
            <button className="file-link" onClick={() => inspectFile(f.id)}>
              <span aria-hidden>▤</span>
              <span>
                {f.name}
                <small>
                  {bytes(f.size)} ·{" "}
                  {f.uploadedId
                    ? "Сервис получил файл"
                    : "Полный файл в этом браузере"}
                </small>
              </span>
            </button>
            {!sample && !p.run && !p.pending ? (
              <button
                className="icon-btn"
                disabled={block}
                onClick={() => deleteFile(f.id)}
                aria-label={`Убрать ${f.name}`}
              >
                ×
              </button>
            ) : null}
          </li>
        ))}
      </ul>
    );
  }
  return (
    <div className={view === "workspace" ? "app workspace-app" : "app"}>
      <a className="skip" href="#main">
        Перейти к содержимому
      </a>
      <header className="topbar">
        <button className="brand" onClick={home} disabled={busy}>
          <span aria-hidden>C</span>CoreTeams
        </button>
        <span className="top-caption">Рабочее пространство с AI-командой</span>
        <nav aria-label="Основная навигация">
          <button
            className="text-btn"
            onClick={() => {
              setTourStep(0);
              setView("tour");
            }}
            disabled={busy}
          >
            Как это работает
          </button>
          <button className="text-btn" onClick={home} disabled={busy}>
            Мои проекты
          </button>
        </nav>
        <span className="service-status">
          {sample
            ? "Учебный пример"
            : api.available
              ? "API настроен, готовность проверяется"
              : "CTF не подключён"}
        </span>
      </header>
      <JourneyPanel
        recorder={study}
        configured={api.available}
        projectId={p?.id}
        hasRun={Boolean(run)}
        sample={sample}
        savedArtifact={
          !sample &&
          a?.saved &&
          run &&
          view === "workspace" &&
          sideTab === "artifacts"
            ? { artifactId: a.id, runId: run.id }
            : undefined
        }
      />
      {issue ? (
        <div role="alert" className="banner error">
          {issue}
        </div>
      ) : null}
      {error ? (
        <div role="alert" className="banner error">
          {error}
        </div>
      ) : null}
      {notice ? (
        <div role="status" className="banner notice">
          {notice}
        </div>
      ) : null}
      <main id="main">
        {view === "home" ? (
          <div className="home-page">
            <section className="hero">
              <div>
                <p className="eyebrow">ВАША ЗАДАЧА. НУЖНАЯ КОМАНДА.</p>
                <h1>
                  Когда одной
                  <br />
                  экспертизы мало
                </h1>
                <p className="identity">{IDENTITY}</p>
                <p className="lead">
                  Принесите вводные. Обсудите вопросы с фасилитатором. Выберите
                  команду и план. Проверяйте результаты по этапам и
                  возвращайтесь к версиям.
                </p>
                <div className="actions">
                  <button
                    className="primary"
                    onClick={() => {
                      setTourStep(0);
                      setView("tour");
                    }}
                  >
                    Посмотреть пример пути <span aria-hidden>↗</span>
                  </button>
                  <button
                    className="secondary"
                    disabled={block}
                    onClick={begin}
                  >
                    Начать свою задачу
                  </button>
                </div>
                <p className="muted">
                  Сейчас можно подготовить проект и изучить пример. Реальное
                  исполнение появится после интеграции CTF.
                </p>
              </div>
              <div className="hero-example">
                <p className="eyebrow">ПРИМЕР, НЕ ЗАПУСК CTF</p>
                <h2>
                  Проверить идею
                  <br />
                  до первых вложений
                </h2>
                <ol>
                  <li>
                    <span>01</span>
                    <div>
                      <strong>Контекст</strong>
                      <p>Что известно и что пока нельзя менять</p>
                    </div>
                  </li>
                  <li>
                    <span>02</span>
                    <div>
                      <strong>AI-команда и план</strong>
                      <p>Исследование, сравнение и проверка рисков</p>
                    </div>
                  </li>
                  <li>
                    <span>03</span>
                    <div>
                      <strong>Артефакты по этапам</strong>
                      <p>Выводы, версии и ваш следующий шаг</p>
                    </div>
                  </li>
                </ol>
              </div>
            </section>
            <section className="capabilities">
              <h2>С чем можно начать</h2>
              <div>
                <article>
                  <span>01 / РЕШЕНИЕ</span>
                  <h3>Разобраться в незнакомой области</h3>
                  <p>
                    Собрать основания, сравнить варианты, обозначить
                    неизвестное.
                  </p>
                </article>
                <article>
                  <span>02 / МАТЕРИАЛЫ</span>
                  <h3>Подготовить документ или план</h3>
                  <p>Приложить вводные и согласовать ожидаемый результат.</p>
                </article>
                <article>
                  <span>03 / ПРОВЕРКА</span>
                  <h3>Посмотреть на задачу с разных сторон</h3>
                  <p>Обсудить состав AI-команды и проверить допущения.</p>
                </article>
              </div>
            </section>
            <section className="projects-section">
              <div className="section-heading">
                <h2>Мои проекты</h2>
                <button className="text-btn" disabled={block} onClick={begin}>
                  + Новый проект
                </button>
              </div>
              {projects.length ? (
                <div className="project-cards">
                  {projects.map((x) => (
                    <button
                      key={x.id}
                      className="project-card"
                      onClick={() => openProject(x)}
                    >
                      <span className="eyebrow">
                        {x.run
                          ? labels[x.run.status]
                          : x.step === 4
                            ? "План подготовлен"
                            : "Черновик вводных"}
                      </span>
                      <strong>{x.title || "Новый проект"}</strong>
                      <small>
                        {x.files.length} файлов · {x.stages.length} этапов ·{" "}
                        {new Date(x.updatedAt).toLocaleDateString("ru")}
                      </small>
                    </button>
                  ))}
                </div>
              ) : (
                <p className="empty">
                  Начните с примера пути, затем создайте свой проект. Черновики
                  и файлы пока хранятся на этом устройстве.
                </p>
              )}
            </section>
          </div>
        ) : null}
        {view === "tour" ? (
          <section className="tour-page">
            <p className="eyebrow">
              СНАЧАЛА ПРИМЕР · {tourStep + 1} ИЗ {TOUR.length}
            </p>
            <h1>Как работает CoreTeams</h1>
            <p className="lead">
              Разберём путь на идее кофейни. Здесь нет реальных пользовательских
              данных или результатов CTF.
            </p>
            <nav className="tour-steps" aria-label="Шаги примера">
              {TOUR.map((s, i) => (
                <button
                  key={s.title}
                  onClick={() => setTourStep(i)}
                  className={tourStep === i ? "active" : ""}
                  aria-current={tourStep === i ? "step" : undefined}
                >
                  {String(i + 1).padStart(2, "0")}
                  <span>
                    {["Задача", "Вопросы", "Команда", "План", "Результаты"][i]}
                  </span>
                </button>
              ))}
            </nav>
            <div className="tour-content">
              <span className="tour-number">
                {String(tourStep + 1).padStart(2, "0")}
              </span>
              <div>
                <h2>{TOUR[tourStep].title}</h2>
                <p>{TOUR[tourStep].body}</p>
                <p className="note">{TOUR[tourStep].detail}</p>
              </div>
            </div>
            <div className="actions">
              {tourStep > 0 ? (
                <button
                  className="secondary"
                  onClick={() => setTourStep(tourStep - 1)}
                >
                  Назад
                </button>
              ) : null}
              {tourStep < 4 ? (
                <button
                  className="primary"
                  onClick={() => setTourStep(tourStep + 1)}
                >
                  Дальше
                </button>
              ) : (
                <>
                  <button
                    className="primary"
                    onClick={() => {
                      setSample(true);
                      setView("workspace");
                      setStageId("options");
                      setArtifactId("");
                      setSideTab("artifacts");
                      setError("");
                    }}
                  >
                    Открыть рабочее пространство примера
                  </button>
                  <button
                    className="secondary"
                    disabled={block}
                    onClick={finishTour}
                  >
                    Начать свой проект
                  </button>
                </>
              )}
            </div>
          </section>
        ) : null}
        {view === "onboard" && p ? (
          <section className="onboarding">
            <div className="onboarding-heading">
              <p className="eyebrow">ГОТОВИМ ВАШ ПРОЕКТ</p>
              <h1>
                {p.step === 0
                  ? "Что вы хотите сделать?"
                  : p.step === 1
                    ? "Уточним задачу вместе"
                    : p.step === 2
                      ? "Выберите команду вокруг задачи"
                      : "Согласуйте путь к результату"}
              </h1>
              <nav className="wizard-steps" aria-label="Подготовка проекта">
                {STEP_NAMES.map((name, i) => (
                  <button
                    key={name}
                    disabled={i > p.step || block}
                    aria-current={p.step === i ? "step" : undefined}
                    onClick={() => patch({ step: i })}
                  >
                    {i + 1}
                    <span>{name}</span>
                  </button>
                ))}
              </nav>
            </div>
            <div className="onboarding-grid">
              <div className="form-panel">
                {p.step === 0 ? (
                  <>
                    <label>
                      Название проекта
                      <input
                        name="project-title"
                        autoComplete="off"
                        value={p.title}
                        onChange={(e) => patch({ title: e.target.value })}
                        disabled={block}
                        placeholder="Например, проверить идею нового сервиса"
                      />
                    </label>
                    <label>
                      Какую задачу нужно решить?
                      <textarea
                        name="project-goal"
                        autoComplete="off"
                        rows={4}
                        value={p.goal}
                        onChange={(e) => patch({ goal: e.target.value })}
                        disabled={block}
                        placeholder="Что хочется понять или получить?"
                      />
                    </label>
                    <label>
                      Направление
                      <select
                        name="scenario"
                        value={p.scenario}
                        onChange={(e) => patch({ scenario: e.target.value })}
                        disabled={block}
                      >
                        <option value="other">
                          Другая задача / пока не знаю
                        </option>
                        <option value="business">Бизнес и новая идея</option>
                        <option value="course">Обучение и курс</option>
                        <option value="book">Книга и материалы</option>
                      </select>
                    </label>
                    <label>
                      Контекст, факты и ссылки
                      <textarea
                        name="context"
                        autoComplete="off"
                        rows={5}
                        value={p.context}
                        onChange={(e) => patch({ context: e.target.value })}
                        disabled={block}
                        placeholder="Аудитория, ограничения, известные источники, что нельзя менять…"
                      />
                    </label>
                    <div className="section-heading">
                      <h2>Вводные файлы</h2>
                      <label className="upload-label">
                        + Добавить файлы
                        <input
                          name="input-files"
                          type="file"
                          multiple
                          aria-label="Добавить вводные файлы"
                          onChange={(e) => {
                            void upload(e.target.files);
                            e.target.value = "";
                          }}
                          disabled={block}
                        />
                      </label>
                    </div>
                    {fileList()}
                    <p className="muted">
                      Храним полное содержимое файлов локально, не только
                      названия. Передача сервису начнётся после явного запуска.
                      Не добавляйте секреты и персональные данные.
                    </p>
                    <button
                      className="primary"
                      disabled={block || !p.title.trim() || !p.goal.trim()}
                      onClick={() => patch({ step: 1 })}
                    >
                      К вопросам фасилитатора
                    </button>
                  </>
                ) : null}
                {p.step === 1 ? (
                  <>
                    <p className="note">
                      Пока CTF не подключён, это вопросы для подготовки. Ваши
                      ответы сохраняются в контексте; во время реального запуска
                      фасилитатор сможет задавать дополнительные вопросы.
                    </p>
                    {QUESTIONS.map((q, i) => (
                      <label key={q}>
                        {q}
                        <textarea
                          name={`preparation-${i}`}
                          autoComplete="off"
                          rows={3}
                          value={p.answers[i]}
                          onChange={(e) =>
                            patch({
                              answers: p.answers.map((s, n) =>
                                n === i ? e.target.value : s,
                              ),
                            })
                          }
                          disabled={block}
                          placeholder="Свой ответ или «пока не знаю»"
                        />
                      </label>
                    ))}
                    <button
                      className="primary"
                      disabled={block || p.answers.some((a) => !a.trim())}
                      onClick={() => patch({ step: 2 })}
                    >
                      Посмотреть и выбрать команду
                    </button>
                  </>
                ) : null}
                {p.step === 2 ? (
                  <>
                    <p className="lead">
                      Фасилитатор связывает работу команды. Посмотрите вклад
                      каждой AI-роли: вы можете изменить состав и указать
                      недостающую экспертизу.
                    </p>
                    <div className="role-card fixed-role">
                      <strong>Фасилитатор · связывает работу</strong>
                      <p>
                        Уточняет задачу, передаёт контекст ролям и возвращается
                        к вам за решением.
                      </p>
                    </div>
                    <div className="role-options">
                      {ROLES.map((r) => (
                        <label
                          className={
                            p.team.includes(r.id)
                              ? "role-option chosen"
                              : "role-option"
                          }
                          key={r.id}
                        >
                          <input
                            name="selected-roles"
                            value={r.id}
                            type="checkbox"
                            checked={p.team.includes(r.id)}
                            disabled={block}
                            onChange={(e) =>
                              patch({
                                team: e.target.checked
                                  ? [...p.team, r.id]
                                  : p.team.filter((id) => id !== r.id),
                              })
                            }
                          />
                          <span>
                            <strong>{r.name}</strong>
                            <span>{r.purpose}</span>
                            <small>Результат: {r.output}</small>
                          </span>
                        </label>
                      ))}
                    </div>
                    <label>
                      Что изменить в составе или какую экспертизу добавить?
                      <textarea
                        name="team-note"
                        autoComplete="off"
                        rows={3}
                        value={p.teamNote}
                        onChange={(e) => patch({ teamNote: e.target.value })}
                        disabled={block}
                        placeholder="Например, нужен взгляд специалиста по закупкам…"
                      />
                    </label>
                    <p className="muted">
                      Сейчас это черновик выбора, не запущенные агенты. CTF
                      должен подтвердить доступность ролей. Нельзя создать
                      рабочего специалиста одним названием.
                    </p>
                    <button
                      className="primary"
                      disabled={block || !p.team.length}
                      onClick={() => patch({ step: 3 })}
                    >
                      Команда подходит — к плану
                    </button>
                  </>
                ) : null}
                {p.step === 3 ? (
                  <>
                    <p className="note">Цель: {p.goal}</p>
                    {p.stages.map((s, i) => (
                      <div className="plan-stage" key={s.id}>
                        <span className="eyebrow">ЭТАП {i + 1}</span>
                        <label>
                          Название этапа {i + 1}
                          <input
                            name={`stage-title-${i}`}
                            autoComplete="off"
                            value={s.title}
                            disabled={block}
                            onChange={(e) =>
                              patch({
                                stages: p.stages.map((x) =>
                                  x.id === s.id
                                    ? { ...x, title: e.target.value }
                                    : x,
                                ),
                              })
                            }
                          />
                        </label>
                        <label>
                          Результат этапа {i + 1}
                          <textarea
                            name={`stage-goal-${i}`}
                            autoComplete="off"
                            rows={2}
                            value={s.goal}
                            disabled={block}
                            onChange={(e) =>
                              patch({
                                stages: p.stages.map((x) =>
                                  x.id === s.id
                                    ? { ...x, goal: e.target.value }
                                    : x,
                                ),
                              })
                            }
                          />
                        </label>
                        {p.stages.length > 1 ? (
                          <button
                            className="text-btn"
                            disabled={block}
                            onClick={() =>
                              patch({
                                stages: p.stages.filter((x) => x.id !== s.id),
                              })
                            }
                          >
                            Убрать этап {i + 1}
                          </button>
                        ) : null}
                      </div>
                    ))}
                    <button
                      className="secondary"
                      disabled={block}
                      onClick={() =>
                        patch({
                          stages: [
                            ...p.stages,
                            {
                              id: crypto.randomUUID(),
                              title: "Новый этап",
                              goal: "Какой артефакт нужен?",
                            },
                          ],
                        })
                      }
                    >
                      + Добавить этап
                    </button>
                    <p className="muted">
                      Лимиты проектов, этапов и токенов в интерфейсе не
                      включены. Фактическое потребление покажем из CTF, когда
                      появятся подтверждённые данные.
                    </p>
                    <button
                      className="primary"
                      disabled={
                        block ||
                        p.stages.some((s) => !s.title.trim() || !s.goal.trim())
                      }
                      onClick={() => {
                        patch({ step: 4 });
                        setView("workspace");
                        setStageId(p.stages[0].id);
                        setSideTab("artifacts");
                      }}
                    >
                      План подходит — в рабочее пространство
                    </button>
                  </>
                ) : null}
              </div>
              <aside className="guide-panel">
                <p className="eyebrow">ВЫ ОПРЕДЕЛЯЕТЕ ЗАДАЧУ</p>
                <h2>
                  {
                    [
                      "Не нужно начинать с идеального ТЗ",
                      "Неизвестное можно назвать неизвестным",
                      "Вы участвуете в выборе",
                      "Каждый этап оставляет результат",
                    ][p.step]
                  }
                </h2>
                <p>
                  {
                    [
                      "Расскажите о ситуации и добавьте имеющиеся материалы. Команда получает содержание файлов только при явном запуске.",
                      "Ответ «пока не знаю» лучше выдуманного факта. Во время работы фасилитатор вернётся за уточнением.",
                      "Вы видите, зачем нужна роль и что она должна передать дальше. Роли ИИ не заменяют проверку реальными специалистами.",
                      "Артефакты и версии появятся справа. В центре можно задать вопрос, попросить доработку или согласовать результат.",
                    ][p.step]
                  }
                </p>
                <div className="service-note">
                  CTF{" "}
                  {api.available
                    ? "настроен, ответы ещё нужно проверить"
                    : "пока не подключён"}
                  . Подготовка проекта работает локально.
                </div>
              </aside>
            </div>
            {p.step > 0 ? (
              <button
                className="text-btn"
                disabled={block}
                onClick={() => patch({ step: p.step - 1 })}
              >
                ← Назад
              </button>
            ) : null}
          </section>
        ) : null}
        {view === "workspace" && p ? (
          <section className="workspace">
            {sample ? (
              <div className="sample-banner">
                <strong>Учебный пример · CTF не запускался</strong>
                <span>
                  Изучите этапы и версии. Данные и роли здесь подготовлены
                  заранее.
                </span>
                <button
                  className="primary"
                  onClick={finishTour}
                  disabled={block}
                >
                  Начать свой проект
                </button>
              </div>
            ) : null}
            <div className="workspace-heading">
              <div>
                <p className="eyebrow">ВАШ ПРОЕКТ</p>
                <h1>{p.title}</h1>
                <p>
                  {sample
                    ? "Пример пути"
                    : run
                      ? labels[run.status]
                      : "Вводные, команда и план подготовлены. Работа ещё не запущена."}
                </p>
              </div>
              <div className="actions">
                {!sample && !run ? (
                  <>
                    <button
                      className="secondary"
                      disabled={block || Boolean(p.pending)}
                      onClick={() => {
                        patch({ step: 0 });
                        setView("onboard");
                      }}
                    >
                      Изменить вводные
                    </button>
                    <button
                      className="primary"
                      disabled={block}
                      onClick={() =>
                        perform("start", {
                          ...projectPayload(p),
                          files: p.files,
                        })
                      }
                    >
                      {p.pending
                        ? "Повторить прежний запрос"
                        : "Запустить работу команды"}
                    </button>
                  </>
                ) : null}
                {!sample && run ? (
                  <button
                    className="secondary"
                    disabled={block || Boolean(p.pending)}
                    onClick={refresh}
                  >
                    Обновить статус
                  </button>
                ) : null}
              </div>
            </div>
            {p.pending && !sample ? (
              <div className="banner pending">
                Исход запроса не подтверждён. Повтор отправляет прежние данные и
                тот же ID.
                <button
                  className="text-btn"
                  disabled={block}
                  onClick={() =>
                    perform(
                      p.pending!.action,
                      p.pending!.body,
                      p.pending!.targetId,
                    )
                  }
                >
                  Повторить запрос
                </button>
              </div>
            ) : null}
            <div className="workspace-columns">
              <aside className="project-sidebar">
                <h2>Путь проекта</h2>
                <nav aria-label="Этапы работы">
                  {p.stages.map((s, i) => {
                    const result = latest(run?.artifacts || [], s.id);
                    const ids = new Set(
                      (run?.artifacts || [])
                        .filter((a) => a.stageId === s.id)
                        .map((a) => a.documentId),
                    );
                    const phaseApproved =
                      ids.size > 0 &&
                      [...ids].every(
                        (id) =>
                          (run?.artifacts || [])
                            .filter(
                              (a) => a.stageId === s.id && a.documentId === id,
                            )
                            .sort((a, b) => b.version - a.version)[0]?.approved,
                      );
                    return (
                      <button
                        key={s.id}
                        className={
                          selectedStage?.id === s.id
                            ? "stage-nav active"
                            : "stage-nav"
                        }
                        onClick={() => {
                          setStageId(s.id);
                          setDocumentId("");
                          setArtifactId("");
                          setSideTab("artifacts");
                        }}
                        aria-current={
                          selectedStage?.id === s.id ? "step" : undefined
                        }
                      >
                        <span>
                          {phaseApproved ? "✓" : String(i + 1).padStart(2, "0")}
                        </span>
                        <span>
                          <strong>{s.title}</strong>
                          <small>
                            {result
                              ? phaseApproved
                                ? "Последние версии согласованы"
                                : `Артефактов: ${ids.size}`
                              : run?.stageId === s.id
                                ? labels[run.status]
                                : "Планируется"}
                          </small>
                        </span>
                      </button>
                    );
                  })}
                </nav>
                <div className="sidebar-divider">
                  <h2>Материалы</h2>
                  {p.files.length ? (
                    fileList()
                  ) : (
                    <p className="muted">
                      {sample
                        ? "В примере используются только текстовые вводные."
                        : "Вводные файлы не добавлены"}
                    </p>
                  )}
                  <details>
                    <summary>Контекст задачи</summary>
                    <p className="context-text">{p.goal}</p>
                    <p className="context-text">
                      {p.context || "Дополнительный контекст не указан"}
                    </p>
                  </details>
                </div>
                <p className="local-note">
                  {sample
                    ? "Учебные данные"
                    : `${p.files.length} файлов · ${bytes(p.files.reduce((n, f) => n + f.size, 0))}`}
                  <br />
                  {sample
                    ? "Не пользовательский результат"
                    : "Черновики и файлы на этом устройстве"}
                </p>
              </aside>
              <section className="conversation">
                <div className="pane-heading">
                  <div>
                    <span className="eyebrow">РАБОТА НАД ЗАДАЧЕЙ</span>
                    <h2>Диалог с фасилитатором</h2>
                  </div>
                  <span className="status-pill">
                    {sample
                      ? "Пример"
                      : run
                        ? labels[run.status]
                        : "Не запущено"}
                  </span>
                </div>
                <div className="conversation-body">
                  {!run ? (
                    <div className="welcome-message">
                      <span className="message-avatar">C</span>
                      <div>
                        <strong>Ваш следующий шаг</strong>
                        <p>
                          Контекст, материалы, команда и план собраны. После
                          подключения CTF нажмите «Запустить работу команды».
                          Здесь появятся реальные вопросы, объяснения и
                          результаты.
                        </p>
                        <p className="muted">
                          До запуска ничего не передаём сервису. Сейчас нет
                          выполненных артефактов или измеренного потребления.
                        </p>
                      </div>
                    </div>
                  ) : (
                    run.messages.map((m) => (
                      <article key={m.id} className={`message ${m.role}`}>
                        <span className="message-avatar">
                          {m.role === "user" ? "Вы" : "C"}
                        </span>
                        <div>
                          <div className="message-label">
                            {m.role === "user"
                              ? "Вы"
                              : m.role === "facilitator"
                                ? "Фасилитатор"
                                : "Сервис"}
                            {sample ? " · учебный пример" : ""}
                          </div>
                          <p>{m.text}</p>
                        </div>
                      </article>
                    ))
                  )}
                  {run?.status === "clarification" && run.question ? (
                    <div className="question-box">
                      <span className="eyebrow">НУЖНО ВАШЕ РЕШЕНИЕ</span>
                      <h3>{run.question.text}</h3>
                      <label>
                        Ответ фасилитатору
                        <textarea
                          name="facilitator-answer"
                          aria-label="Ответ фасилитатору"
                          autoComplete="off"
                          rows={3}
                          value={answer}
                          disabled={block || sample || Boolean(p.pending)}
                          onChange={(e) => {
                            setAnswer(e.target.value);
                            patch({
                              answerDraft: {
                                questionId: run.question!.id,
                                text: e.target.value,
                              },
                            });
                          }}
                        />
                      </label>
                      <button
                        className="primary"
                        disabled={
                          block ||
                          sample ||
                          !answer.trim() ||
                          Boolean(p.pending)
                        }
                        onClick={() =>
                          perform("answer", {
                            questionId: run.question!.id,
                            answer: answer.trim(),
                          })
                        }
                      >
                        Ответить и продолжить
                      </button>
                    </div>
                  ) : null}
                  {run?.status === "failed" ? (
                    <p className="error-note">
                      Запуск завершился ошибкой. ID: {run.id}. Новый запуск не
                      создаётся автоматически.
                    </p>
                  ) : null}
                </div>
                <form
                  className="composer"
                  onSubmit={(e) => {
                    e.preventDefault();
                    if (p.draftMessage.trim())
                      void perform("message", {
                        text: p.draftMessage.trim(),
                        stageId: selectedStage?.id,
                        artifactId: a?.id,
                      });
                  }}
                >
                  <label>
                    Вопрос или замечание к результату
                    <textarea
                      name="revision-message"
                      autoComplete="off"
                      rows={3}
                      disabled={sample || block || Boolean(p.pending)}
                      value={sample ? "" : p.draftMessage}
                      onChange={(e) => patch({ draftMessage: e.target.value })}
                      placeholder={
                        sample
                          ? "В учебном примере отправка отключена"
                          : "Что уточнить или доработать? Укажите нужный этап или версию…"
                      }
                    />
                  </label>
                  <div className="composer-footer">
                    <small>
                      {sample
                        ? "Для своей задачи создайте проект"
                        : run
                          ? "Отправка относится к текущему запуску"
                          : "Черновик замечания сохранён. Для отправки нужен запуск CTF."}
                    </small>
                    <button
                      className="primary"
                      disabled={
                        sample ||
                        block ||
                        !run ||
                        Boolean(p.pending) ||
                        !p.draftMessage.trim()
                      }
                    >
                      Передать фасилитатору
                    </button>
                  </div>
                </form>
              </section>
              <aside className="results-pane">
                <nav className="pane-tabs" aria-label="Панель проекта">
                  {(["artifacts", "team", "usage"] as const).map((t) => (
                    <button
                      key={t}
                      aria-pressed={sideTab === t}
                      className={sideTab === t ? "active" : ""}
                      onClick={() => setSideTab(t)}
                    >
                      {t === "artifacts"
                        ? "Артефакты"
                        : t === "team"
                          ? "Команда"
                          : "Замеры"}
                    </button>
                  ))}
                </nav>
                <div className="results-body">
                  {sideTab === "team" ? team() : null}
                  {sideTab === "usage" ? (
                    <>
                      <p className="eyebrow">ФАКТИЧЕСКОЕ ПОТРЕБЛЕНИЕ</p>
                      <h2>Замеры CTF</h2>
                      <dl className="usage-list">
                        {[
                          ["Входные токены", run?.usage?.inputTokens],
                          ["Выходные токены", run?.usage?.outputTokens],
                          ["Запросы", run?.usage?.requests],
                          ["Время вычисления, с", run?.usage?.computeSeconds],
                        ].map(([label, value]) => (
                          <div key={String(label)}>
                            <dt>{label}</dt>
                            <dd>
                              {value === undefined
                                ? "Не измерено"
                                : Number(value).toLocaleString("ru")}
                            </dd>
                          </div>
                        ))}
                        <div>
                          <dt>Затраты</dt>
                          <dd>
                            {run?.usage?.cost === undefined
                              ? "Не измерено"
                              : `${run.usage.cost.toFixed(4)} ${run.usage.currency}`}
                          </dd>
                        </div>
                      </dl>
                      <p className="note">
                        Продуктовые ограничения по токенам, запускам и проектам
                        пока не включены. Неизмеренные значения не заменяются
                        нулём.
                      </p>
                      <p className="muted">
                        Показываем данные из backend, не прогноз. Ограничения
                        безопасности и техническая ёмкость сервера/браузера
                        сохраняются.
                      </p>
                    </>
                  ) : null}
                  {sideTab === "artifacts" ? (
                    <>
                      <span className="eyebrow">РЕЗУЛЬТАТ ЭТАПА</span>
                      <h2>{selectedStage?.title}</h2>
                      <p className="muted">{selectedStage?.goal}</p>
                      {a ? (
                        <>
                          <label>
                            Артефакт этапа
                            <select
                              name="artifact-document"
                              aria-label="Артефакт этапа"
                              value={activeDocument?.documentId}
                              onChange={(e) => {
                                setDocumentId(e.target.value);
                                setArtifactId("");
                              }}
                            >
                              {documents.map((d) => (
                                <option value={d.documentId} key={d.documentId}>
                                  {d.title}
                                </option>
                              ))}
                            </select>
                          </label>
                          <label className="version-select">
                            Версия артефакта
                            <select
                              name="artifact-version"
                              aria-label="Версия артефакта"
                              value={a.id}
                              onChange={(e) => setArtifactId(e.target.value)}
                            >
                              {versions.map((v) => (
                                <option key={v.id} value={v.id}>
                                  v{v.version} · {v.title}
                                  {v.approved ? " · согласована" : ""}
                                </option>
                              ))}
                            </select>
                          </label>
                          <div className="artifact-heading">
                            <h3>{a.title}</h3>
                            <span className="status-pill">v{a.version}</span>
                          </div>
                          <Document content={a.content} />
                          <p className="artifact-status">
                            {sample
                              ? "Учебная версия, не результат CTF"
                              : a.saved
                                ? `Сервер подтвердил запись${a.savedAt ? ` · ${new Date(a.savedAt).toLocaleString("ru")}` : ""}`
                                : "Постоянное сохранение не подтверждено"}
                          </p>
                          <div className="artifact-actions">
                            <button
                              className="secondary"
                              onClick={() => {
                                downloadText(a);
                                track("text_download_handed", {
                                  runId: run?.id,
                                  artifactId: a.id,
                                });
                              }}
                            >
                              Скачать текст версии
                            </button>
                            {!sample ? (
                              <button
                                className="secondary"
                                disabled={
                                  block ||
                                  !a.saved ||
                                  !p.serverId ||
                                  Boolean(p.pending)
                                }
                                onClick={downloadZip}
                              >
                                Скачать ZIP версии
                              </button>
                            ) : null}
                            {!sample ? (
                              <button
                                className="primary"
                                disabled={
                                  block ||
                                  Boolean(p.pending) ||
                                  a.approved ||
                                  a.id !== versions[0]?.id ||
                                  !a.saved
                                }
                                onClick={() =>
                                  perform(
                                    "approve",
                                    { artifactId: a.id, version: a.version },
                                    a.id,
                                  )
                                }
                              >
                                {a.approved
                                  ? "Версия согласована"
                                  : "Согласовать результат"}
                              </button>
                            ) : null}
                          </div>
                          {!sample ? (
                            <p className="muted">
                              Для доработки напишите замечание в центре. Новая
                              версия появится только из ответа CTF; прежняя
                              останется доступна.
                            </p>
                          ) : null}
                        </>
                      ) : (
                        <div className="artifact-empty">
                          <span aria-hidden>▤</span>
                          <h3>Здесь появится артефакт этапа</h3>
                          <p>
                            Результат, его версии и подтверждение сохранения
                            будут получены из сервиса. Пока этап только в плане.
                          </p>
                        </div>
                      )}
                    </>
                  ) : null}
                </div>
              </aside>
            </div>
          </section>
        ) : null}
      </main>
      {preview ? (
        <div className="modal-backdrop">
          <section
            className="file-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="file-preview-title"
          >
            <div className="section-heading">
              <h2 id="file-preview-title">{preview.name}</h2>
              <button
                autoFocus
                className="secondary"
                onClick={() => setPreview(null)}
              >
                Закрыть просмотр
              </button>
            </div>
            <p className="muted">
              Без выполнения содержимого. Для текстовых файлов — первые16КБ,
              оригинал хранится полностью.
            </p>
            <pre>{preview.text}</pre>
          </section>
        </div>
      ) : null}
    </div>
  );
}
