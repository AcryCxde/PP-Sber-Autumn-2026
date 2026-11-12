import { useEffect, useRef, useState } from "react";
import { ApiError, CtfApi, recoverRejectedOperation, assertDraftCurrent, draftStorageChanged } from "./api.ts";
import { FacilitatorQuestions } from "./FacilitatorQuestions.tsx";
import {
  IDENTITY,
  KEY,
  ROLES,
  QUESTIONS,
  exampleProject,
  latest,
  mergeRun,
  mergeRework,
  projectArtifacts,
  projectMessages,
  projectRuns,
  artifactRunId,
  approved,
  canRework,
  isWaiting,
  shouldPollRun,
  newProject,
  projectPayload,
  MAX_PLAN_STAGES,
  readProjects,
  type Project,
  type Pending,
  type Run,
  type Artifact,
} from "./domain.ts";
import { getFile, keepFile, removeFile } from "./files.ts";
import { JourneyPanel } from "./JourneyPanel.tsx";
import { JourneyRecorder, accountJourneyKey, type EventName, type Fields } from "./journey.ts";
import type { User } from "./auth.tsx";
import { LegacyProjects } from "./LegacyProjects.tsx";
import { BrandMark } from "./BrandMark.tsx";

// By default the same-origin CoreTeams backend serves contract V2 at /api/v2; the session
// cookie comes from signing in to the main interface.
const api = new CtfApi(
  import.meta.env.VITE_CTF_API_URL || new URL("/api/v2", window.location.origin).href,
  import.meta.env.VITE_CTF_V2_ENABLED !== "false",
);
const labels = {
  queued: "Запуск в очереди",
  running: "Команда работает",
  clarification: "Нужен ваш ответ",
  waiting_for_input: "Нужен ваш ответ",
  review: "Результат на проверке",
  completed: "Работа завершена",
  failed: "Запуск завершился ошибкой",
  outcome_unknown: "Исход запуска неизвестен — проверьте результат",
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
function initial(key: string) {
  try {
    const raw = localStorage.getItem(key);
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
export function App({ user, onLogout }: { user: User; onLogout: () => void }) {
  // Drafts are kept per account: another user of this browser does not see them.
  const storageKey = `${KEY}:${user.id}`;
  const [study] = useState(
    () =>
      new JourneyRecorder({
        getItem: (key) => window.localStorage.getItem(key),
        setItem: (key, value) => window.localStorage.setItem(key, value),
      }, undefined, undefined, accountJourneyKey(user.id)),
  );
  const [loaded] = useState(() => initial(storageKey)),
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
      leave(); // Account logout/unmount ends observation; remount recovers as paused.
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
      if (isWaiting(run) && run?.question)
        track(
          "clarification_shown",
          { runId: run.id, questionId: run.question.id },
          run.question.id,
        );
    }
  }, [view, tourStep, p?.id, p?.step, run?.id, run?.question?.id, sample]);
  function save(next: Project[]) {
    if (issue) return false;
    try {
      if (localStorage.getItem(storageKey) !== rawRef.current) {
        setIssue(
          "Черновики изменились в другой вкладке. Обновите страницу перед продолжением.",
        );
        return false;
      }
      const raw = JSON.stringify({ version: 3, projects: next });
      localStorage.setItem(storageKey, raw);
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
      if (draftStorageChanged(e.key, storageKey))
        setIssue(
          "Черновики изменились в другой вкладке. Обновите страницу перед продолжением.",
        );
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, [storageKey]);
  function requireCurrentDraft() {
    try { assertDraftCurrent(localStorage, storageKey, rawRef.current); }
    catch (e) {
      setIssue(e instanceof Error ? e.message : "Не удалось проверить черновики");
      throw e;
    }
  }
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
    if (!p || sample || block || busyRef.current) return;
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
      runId: action === "approve" && targetId ? artifactRunId(next, targetId) : next.run?.id,
    };
    if (pending.legacy || pending.action === "message" || pending.rejected) {
      setError("Этот запрос нельзя повторить автоматически: устаревший формат или вопрос. Перечитайте состояние; прежние данные сохранены.");
      return;
    }
    if (!next.pending && pending.action === "answer" && (!isWaiting(next.run) || next.run?.question?.id !== pending.body.questionId || next.run?.progress?.stage === "interrupted" || next.run?.progress?.stage === "recovering")) {
      setError("Вопрос изменился или временно недоступен. Обновите статус; прежний ответ не отправлен.");
      return;
    }
    next.pending = pending;
    if (!update(next)) return;
    if (pending.action === "answer")
      track(
        "answer_sent",
        { runId: next.run?.id, requestId: pending.id },
        pending.id,
      );
    if (pending.action === "rework")
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
      requireCurrentDraft();
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
          requireCurrentDraft();
          const uploadedId = await api.upload(
            serverId,
            file.id,
            file.name,
            blob,
            file.sha256,
            requireCurrentDraft,
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
        requireCurrentDraft();
        result = await api.run(
          `projects/${encodeURIComponent(serverId)}/runs`,
          { ...pending.body, fileIds: next.files.map((f) => f.uploadedId) },
          pending.id,
        );
      } else if (pending.action === "rework") {
        if (!next.serverId || !canRework(next.run) || next.run?.id !== pending.runId)
          throw new Error("Для доработки нужен подтверждённый завершённый запуск");
        result = await api.run(`projects/${encodeURIComponent(next.serverId)}/runs`, pending.body, pending.id);
      } else if (pending.action === "approve") {
        const artifact = projectArtifacts(next).find(a => a.id === pending.targetId);
        if (!next.serverId || !artifact || !pending.runId) throw new Error("Нет сохранённой версии для одобрения");
        const receipt = await api.approve(
          `projects/${encodeURIComponent(next.serverId)}/runs/${encodeURIComponent(pending.runId)}/artifacts/${encodeURIComponent(artifact.id)}/approve`,
          pending.body, pending.id, artifact,
        );
        const current = projectsRef.current.find(x => x.id === next.id) || next;
        if (!update({ ...current, pending: undefined, approvals: { ...current.approvals, [artifact.id]: receipt } }))
          throw new Error("Подтверждение одобрения не сохранено в браузере");
        track("artifact_accepted", { runId: pending.runId, artifactId: artifact.id }, artifact.id);
        setNotice("Сервер подтвердил одобрение конкретной версии. Закрытый запуск и содержимое документа не изменены.");
        return;
      } else {
        if (!next.serverId || !next.run)
          throw new Error("Нет подтверждённого запуска");
        if (next.run.id !== pending.runId) throw new Error("Запрос относится к другому запуску");
        const prefix = `projects/${encodeURIComponent(next.serverId)}/runs/${encodeURIComponent(next.run.id)}`;
        result = await api.run(`${prefix}/answers`, pending.body, pending.id);
        if (result.id !== next.run.id)
          throw new Error("Ответ относится к другому запуску");
      }
      const current = projectsRef.current.find((x) => x.id === next.id) || next;
      const withServer = { ...current, serverId: next.serverId, files: next.files };
      next = pending.action === "rework"
        ? mergeRework(withServer, result, pending.runId!, String(pending.body.sourceArtifactId))
        : mergeRun(withServer, result);
      next.pending = undefined;
      next.draftMessage = "";
      next.answerDraft = undefined;
      if (!update(next)) throw new Error("Ответ не сохранён в браузере");
      if (pending.action === "rework") setArtifactId("");
      observeRun(next.id, result);
      if (pending.action === "answer")
        track(
          "answer_acknowledged",
          { runId: result.id, requestId: pending.id },
          pending.id,
        );
      if (pending.action === "rework")
        track(
          "message_acknowledged",
          { runId: result.id, requestId: pending.id },
          pending.id,
        );
      setPoll(true);
      setNotice(
        "Ответ сервиса получен. Сохранение каждого артефакта подтверждается отдельно.",
      );
    } catch (e) {
      if (study.active()?.phase !== "baseline" && study.active()?.projectId === p.id && study.active()?.mode === "wait") study.mode("active");
      if (pending.action === "answer" && e instanceof ApiError && e.code === "unknown_fork" && e.status && e.status >= 400 && e.status < 500) {
        // A definite rejection, unlike timeout, makes this answer safe NOT to replay.
        const current = projectsRef.current.find(x => x.id === next.id) || next;
        update({ ...current, pending: { ...pending, rejected: true } });
        try {
          requireCurrentDraft();
          const fresh = await api.run(`projects/${encodeURIComponent(next.serverId!)}/runs/${encodeURIComponent(pending.runId!)}`);
          const latestProject = projectsRef.current.find(x => x.id === next.id) || current;
          if (update({ ...mergeRun(latestProject, fresh), pending: undefined, answerDraft: undefined })) observeRun(next.id, fresh);
          setError("Прежний вопрос отклонён как устаревший. Загружено текущее состояние; заполните актуальные вопросы заново. Старый ответ не отправлен.");
        } catch {
          setError("Вопрос устарел (unknown_fork). Не удалось перечитать состояние. Нажмите «Обновить статус»; старый ответ не будет повторён.");
        }
        track("operation_failed", { requestId: pending.id, status: "unknown_fork" });
        return;
      }
      if (
        study.active()?.phase !== "baseline" &&
        study.active()?.projectId === p.id &&
        study.active()?.mode === "wait"
      )
        study.mode("active");
      const current = projectsRef.current.find(x => x.id === next.id) || next;
      const recovered = recoverRejectedOperation(current, pending.id, e);
      const rejected = recovered !== current && update(recovered);
      track("operation_failed", {
        requestId: pending.id,
        status: rejected ? "rejected" : "unconfirmed",
      });
      if (rejected) {
        setError(`Сервис отклонил запрос (${(e as ApiError).status}). Исправьте вводные или ответ и отправьте заново. Данные сохранены. ID запроса: ${pending.id}`);
        return;
      }
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
      if (current) {
        const merged = mergeRun(current, nextRun);
        if (current.pending?.rejected) { merged.pending = undefined; merged.answerDraft = undefined; }
        if (update(merged)) observeRun(current.id, nextRun);
      }
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
          `projects/${encodeURIComponent(p.serverId)}/runs/${encodeURIComponent(artifactRunId(p, a.id)!)}/artifacts/${encodeURIComponent(a.id)}/zip`,
        ),
        url = URL.createObjectURL(blob),
        link = document.createElement("a");
      link.href = url;
      link.download = `coreteams-v${a.version}.zip`;
      link.click();
      track("zip_download_handed", { runId: artifactRunId(p, a.id), artifactId: a.id });
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
      !shouldPollRun(run) ||
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
  const artifacts = p ? projectArtifacts(p) : [];
  const stageArtifacts = artifacts.filter((a) => a.stageId === selectedStage?.id);
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
  const answerDraft = p?.answerDraft?.questionId === run?.question?.id
    ? p?.answerDraft?.answers || (p?.answerDraft?.text && run?.question?.questions.length === 1
      ? { [run.question.questions[0].text]: { selected: [], custom: p.answerDraft.text } } : {}) : {};
  const questionReady = isWaiting(run) && run?.progress?.stage !== "interrupted" && run?.progress?.stage !== "recovering";
  useEffect(() => {
    if (!sample && view === "workspace" && sideTab === "artifacts" && a) {
      track(
        "result_shown",
        { runId: p ? artifactRunId(p, a.id) : undefined, artifactId: a.id, stageId: a.stageId },
        a.id,
      );
      track("artifact_viewed", {
        runId: p ? artifactRunId(p, a.id) : undefined,
        artifactId: a.id,
        stageId: a.stageId,
      });
    }
  }, [view, p?.id, sample, sideTab, a?.id]);
  const onboarding = view === "onboard" && p && !sample;
  const inProject = Boolean(p) && !sample && (view === "onboard" || view === "workspace");
  const prepLocked = Boolean(p?.run || p?.pending);
  const answeredCount = p ? p.answers.findIndex((x) => !x.trim()) : 0;
  // Questions open one at a time; an already answered later question stays visible.
  const lastAnswered = p
    ? p.answers.reduce((last, x, i) => (x.trim() ? i : last), -1)
    : -1;
  const visibleQuestions = !p
    ? 0
    : answeredCount === -1
      ? QUESTIONS.length
      : Math.max(answeredCount, lastAnswered) + 1;
  const statusLine = sample
    ? "Учебный пример · CTF не запускался"
    : run
      ? labels[run.status]
      : p?.step === 4
        ? "Работа ещё не запущена"
        : "Черновик в этом браузере";
  const nextStep = !p
    ? ""
    : p.step === 0
      ? "Назовите проект и опишите задачу"
      : p.step === 1
        ? answeredCount === -1
          ? "Посмотреть и выбрать команду"
          : "Ответьте на вопрос фасилитатора"
        : p.step === 2
          ? "Проверьте состав команды"
          : "Согласуйте этапы и результаты";
  function goToStep(i: number) {
    if (!p || block) return;
    if (i === 4) {
      setView("workspace");
      setStageId(run?.stageId || p.stages[0].id);
      setSideTab("artifacts");
      return;
    }
    if (view === "workspace") track("workspace_left", { runId: run?.id });
    patch({ step: i });
    setView("onboard");
  }
  function team() {
    return (
      <>
        <div className="team-list">
          <article className="team-role-row fixed">
            <span className="team-role-number" aria-hidden>
              ✳
            </span>
            <div>
              <strong>Фасилитатор</strong>
              <p>Связывает работу ролей и обсуждает с вами вопросы и результаты.</p>
            </div>
          </article>
          {ROLES.filter((r) => p?.team.includes(r.id)).map((r, i) => (
            <article className="team-role-row" key={r.id}>
              <span className="team-role-number" aria-hidden>
                {String(i + 1).padStart(2, "0")}
              </span>
              <div>
                <strong>{r.name}</strong>
                <p>{r.purpose}</p>
                <small>Результат: {r.output}</small>
              </div>
            </article>
          ))}
        </div>
        {p?.teamNote ? (
          <p className="note">Ваше уточнение: {p.teamNote}</p>
        ) : null}
        <p className="muted">
          {sample
            ? "Состав учебного примера. CTF не запускался."
            : "Выбранные AI-роли — часть согласуемого плана. Их доступность и назначение должен подтвердить backend."}
        </p>
        {!sample ? (
          <div className="role-evidence">
            <h3>Исполнение по событиям сервиса</h3>
            {p && projectRuns(p).some((r) => r.roleEvents?.length) ? (
              <ul>
                {projectRuns(p).flatMap((r) =>
                  (r.roleEvents || []).map((e) => (
                    <li key={`${r.id}:${e.id}`}>
                      {ROLES.find((role) => role.id === e.roleId)?.name || e.roleId}:{" "}
                      {e.type === "role.started"
                        ? "работа начата"
                        : e.type === "role.completed"
                          ? "работа завершена"
                          : "сообщена ошибка"}
                      <small> · {new Date(e.at).toLocaleString("ru")}</small>
                    </li>
                  )),
                )}
              </ul>
            ) : (
              <p className="muted">
                Подтверждённых событий ролей пока нет. Выбор команды и названия
                ролей в запросе не доказывают их исполнение.
              </p>
            )}
          </div>
        ) : null}
      </>
    );
  }
  function fileList() {
    return (
      <ul className="files">
        {p?.files.map((f) => (
          <li key={f.id}>
            <button className="file-link" onClick={() => inspectFile(f.id)}>
              <span className="file-icon" aria-hidden>
                ▤
              </span>
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
  function stageNav() {
    if (!p) return null;
    return (
      <nav className="nav-list" aria-label="Этапы работы">
        {p.stages.map((s, i) => {
          const result = latest(artifacts, s.id);
          const ids = new Set(
            artifacts.filter((a) => a.stageId === s.id).map((a) => a.documentId),
          );
          const phaseApproved =
            ids.size > 0 &&
            [...ids].every((id) => {
              const version = artifacts
                .filter((a) => a.stageId === s.id && a.documentId === id)
                .sort((a, b) => b.version - a.version)[0];
              return version && (sample ? version.approved : approved(p, version));
            });
          const active = selectedStage?.id === s.id;
          return (
            <button
              key={s.id}
              className={`nav-button stage-nav${active ? " active" : ""}${phaseApproved ? " done" : ""}`}
              onClick={() => {
                setStageId(s.id);
                setDocumentId("");
                setArtifactId("");
                setSideTab("artifacts");
              }}
              aria-current={active ? "step" : undefined}
            >
              <span className="nav-num">
                {phaseApproved ? "✓" : i + 1}
              </span>
              <span className="nav-text">
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
    );
  }
  return (
    <>
      <a className="skip" href="#main">
        Перейти к содержимому
      </a>
      <div className={`shell${view === "workspace" ? " workspace-app" : ""}`}>
        <aside className="sidebar" aria-label="Навигация по проекту">
          <button className="brand" onClick={home} disabled={busy}>
            <BrandMark />
            <span>coreteams</span>
          </button>
          <button
            className={`projects-link${view === "home" ? " active" : ""}`}
            onClick={home}
            disabled={busy}
          >
            Мои проекты <span>{projects.length}</span>
          </button>
          {inProject && p ? (
            <div className="sidebar-group">
              <p className="sidebar-caption">Подготовка проекта</p>
              <nav className="nav-list" aria-label="Подготовка проекта">
                {NAV_STEPS.map((name, i) => {
                  const current =
                    i === 4 ? view === "workspace" : view === "onboard" && p.step === i;
                  const enabled =
                    i === 4 ? p.step === 4 : !prepLocked && i <= p.step;
                  return (
                    <button
                      key={name}
                      className={`nav-button${current ? " active" : ""}${i < p.step && !current ? " done" : ""}`}
                      disabled={block || !enabled}
                      aria-current={current ? "step" : undefined}
                      onClick={() => goToStep(i)}
                    >
                      <span className="nav-num">
                        {i < p.step && !current ? "✓" : i + 1}
                      </span>
                      <span className="nav-text">{name}</span>
                    </button>
                  );
                })}
              </nav>
            </div>
          ) : null}
          {view === "workspace" && p ? (
            <div className="sidebar-group">
              <p className="sidebar-caption">Этапы работы</p>
              {stageNav()}
            </div>
          ) : null}
          {view === "workspace" && p ? (
            <div className="sidebar-extra">
              {run ? (
                <details className="run-history">
                  <summary>История запусков · {projectRuns(p).length}</summary>
                  <ol>
                    {projectRuns(p).map((attempt, i) => (
                      <li key={attempt.id}>
                        <strong>
                          Запуск {i + 1}
                          {attempt.id === run.id ? " · текущий" : ""}
                        </strong>
                        <small>{labels[attempt.status]}</small>
                        <code>{attempt.id}</code>
                        {attempt.sourceArtifactId ? (
                          <small>Доработка версии: {attempt.sourceArtifactId}</small>
                        ) : null}
                      </li>
                    ))}
                  </ol>
                  <p className="muted">
                    Диалог и версии всех запусков остаются в этом проекте.
                    Техническое завершение не означает ваше одобрение.
                  </p>
                </details>
              ) : null}
              <p className="sidebar-caption">Материалы</p>
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
          ) : null}
          <div className="sidebar-bottom">
            <div className="draft-note">
              <strong>
                {sample
                  ? "Учебные данные"
                  : p && view !== "home" && view !== "tour"
                    ? `${p.files.length} файлов · ${bytes(p.files.reduce((n, f) => n + f.size, 0))}`
                    : "Черновики на этом устройстве"}
              </strong>
              {sample
                ? "Не пользовательский результат."
                : "Черновики и файлы хранятся в этом браузере. Сохранение на сервере подтверждается отдельно."}
            </div>
            <div className="account">
              <span title={user.email}>{user.email}</span>
              <button className="text-btn" onClick={onLogout} disabled={busy}>
                Выйти
              </button>
            </div>
          </div>
        </aside>
        <div className="main-shell">
          <header className="topbar">
            <div className="topbar-left">
              <span className="project-label">
                {view === "home"
                  ? "Мои проекты"
                  : view === "tour"
                    ? "Как работает CoreTeams"
                    : p?.title || "Новый проект"}
              </span>
              {view === "onboard" || view === "workspace" ? (
                <>
                  <span className="topbar-dot" aria-hidden />
                  <span className="topbar-sub">{statusLine}</span>
                </>
              ) : null}
            </div>
            <nav className="topbar-actions" aria-label="Основная навигация">
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
              <a className="text-btn" href="/chat/">
                Свободный чат
              </a>
              {user.role === "admin" ? (
                <a className="text-btn" href="#admin">
                  Админка
                </a>
              ) : null}
              {sample || !api.available ? (
                <span className="service-status">
                  {sample ? "Учебный пример" : "CTF не подключён"}
                </span>
              ) : null}
            </nav>
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
                ? { artifactId: a.id, runId: artifactRunId(p!, a.id)! }
                : undefined
            }
          />
          {issue || error || notice ? (
            <div className="banners">
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
            </div>
          ) : null}
          <div className={`work-grid${onboarding ? " with-aside" : ""}`}>
            <main className="main-content" id="main">
              {view === "home" ? (
                <div className="home-page">
                  <section aria-labelledby="home-title">
                    <p className="eyebrow">CORETEAMS · ВАША ЗАДАЧА, НУЖНАЯ КОМАНДА</p>
                    <h1 className="page-title" id="home-title">
                      Когда одной экспертизы мало — <em>соберите команду.</em>
                    </h1>
                    <p className="identity">{IDENTITY}</p>
                    <p className="lead">
                      Принесите вводные. Обсудите вопросы с фасилитатором.
                      Выберите команду и план. Проверяйте результаты по этапам и
                      возвращайтесь к версиям.
                    </p>
                    <div className="actions">
                      <button className="primary" disabled={block} onClick={begin}>
                        Начать свою задачу
                      </button>
                      <button
                        className="secondary"
                        onClick={() => {
                          setTourStep(0);
                          setView("tour");
                        }}
                      >
                        Посмотреть пример пути <span aria-hidden>↗</span>
                      </button>
                    </div>
                    <p className="notice-line">
                      {api.available
                        ? "Подготовьте проект и запустите AI-команду. Пример можно изучить без запуска."
                        : "Сейчас можно подготовить проект и изучить пример. Реальное исполнение появится после интеграции CTF."}
                    </p>
                  </section>
                  <section className="section-block capabilities" aria-labelledby="capabilities-title">
                    <p className="section-kicker">С ЧЕГО МОЖНО НАЧАТЬ</p>
                    <h2 className="section-title" id="capabilities-title">
                      Какие задачи решает команда
                    </h2>
                    <div className="card-grid">
                      {CAPABILITIES.map((c, i) => (
                        <article className="card" key={c.title}>
                          <span className="card-symbol" aria-hidden>
                            {["◇", "⌘", "✳"][i]}
                          </span>
                          <span className="card-kicker">{c.kicker}</span>
                          <h3>{c.title}</h3>
                          <p>{c.text}</p>
                        </article>
                      ))}
                    </div>
                  </section>
                  <section className="section-block projects-section" aria-labelledby="projects-title">
                    <div className="section-heading">
                      <h2 className="section-title" id="projects-title">
                        Мои проекты · {projects.length}
                      </h2>
                      <button className="secondary small" disabled={block} onClick={begin}>
                        + Новый проект
                      </button>
                    </div>
                    {projects.length ? (
                      <div className="project-list">
                        {projects.map((x) => (
                          <button
                            key={x.id}
                            className="project-row"
                            onClick={() => openProject(x)}
                          >
                            <span className="project-details">
                              <span className="project-status">
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
                            </span>
                            <span className="project-open" aria-hidden>
                              Открыть →
                            </span>
                          </button>
                        ))}
                      </div>
                    ) : (
                      <p className="empty">
                        Начните с примера пути, затем создайте свой проект.
                        Черновики и файлы пока хранятся на этом устройстве.
                      </p>
                    )}
                  </section>
                  <LegacyProjects />
                </div>
              ) : null}
              {view === "tour" ? (
                <section className="tour-page" aria-labelledby="tour-title">
                  <p className="eyebrow">
                    СНАЧАЛА ПРИМЕР · {tourStep + 1} ИЗ {TOUR.length}
                  </p>
                  <h1 className="page-title" id="tour-title">
                    Как работает CoreTeams
                  </h1>
                  <p className="lead">
                    Разберём путь на идее кофейни. Здесь нет реальных
                    пользовательских данных или результатов CTF.
                  </p>
                  <nav className="tour-steps" aria-label="Шаги примера">
                    {TOUR.map((s, i) => (
                      <button
                        key={s.title}
                        onClick={() => setTourStep(i)}
                        className={tourStep === i ? "active" : ""}
                        aria-current={tourStep === i ? "step" : undefined}
                      >
                        <span className="nav-num">{String(i + 1).padStart(2, "0")}</span>
                        <span>
                          {["Задача", "Вопросы", "Команда", "План", "Результаты"][i]}
                        </span>
                      </button>
                    ))}
                  </nav>
                  <div className="tour-content surface">
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
                <section className="onboarding" aria-labelledby="onboard-title">
                  <p className="eyebrow">
                    ГОТОВИМ ВАШ ПРОЕКТ · ШАГ {p.step + 1} ИЗ 4
                  </p>
                  <h1 className="page-title" id="onboard-title">
                    {
                      [
                        <>Что вы хотите <em>сделать?</em></>,
                        <>Уточним задачу <em>вместе.</em></>,
                        <>Выберите команду <em>вокруг задачи.</em></>,
                        <>Согласуйте путь <em>к результату.</em></>,
                      ][p.step]
                    }
                  </h1>
                  <p className="lead">
                    {
                      [
                        "Достаточно сути. Факты, ссылки и файлы помогут команде не подменять вашу задачу предположениями.",
                        "Фасилитатор задаёт вопросы по одному. Ответ «пока не знаю» лучше выдуманного факта.",
                        "Посмотрите вклад каждой AI-роли: вы можете изменить состав и указать недостающую экспертизу.",
                        "Проверьте этапы и ожидаемые результаты. Работа запускается отдельно после вашего подтверждения.",
                      ][p.step]
                    }
                  </p>
                  {p.step > 0 ? (
                    <div className="workspace-summary">
                      <small>ВАША ЗАДАЧА · {p.title}</small>
                      <strong>{p.goal}</strong>
                    </div>
                  ) : null}
                  {p.step === 0 ? (
                    <div className="brief-box surface">
                      <p className="section-kicker">01 / СУТЬ ЗАДАЧИ</p>
                      <label className="field">
                        <span className="field-label">Название проекта</span>
                        <input
                          className="input"
                          name="project-title"
                          autoComplete="off"
                          value={p.title}
                          onChange={(e) => patch({ title: e.target.value })}
                          disabled={block}
                          placeholder="Например, проверить идею нового сервиса"
                        />
                      </label>
                      <label className="field">
                        <span className="field-label">Какую задачу нужно решить?</span>
                        <textarea
                          className="textarea"
                          name="project-goal"
                          autoComplete="off"
                          rows={4}
                          value={p.goal}
                          onChange={(e) => patch({ goal: e.target.value })}
                          disabled={block}
                          placeholder="Что хочется понять или получить?"
                        />
                      </label>
                      <label className="field">
                        <span className="field-label">Направление</span>
                        <select
                          className="input"
                          name="scenario"
                          value={p.scenario}
                          onChange={(e) => patch({ scenario: e.target.value })}
                          disabled={block}
                        >
                          <option value="other">Другая задача / пока не знаю</option>
                          <option value="business">Бизнес и новая идея</option>
                          <option value="course">Обучение и курс</option>
                          <option value="book">Книга и материалы</option>
                        </select>
                      </label>
                      <label className="field">
                        <span className="field-label">Контекст, факты и ссылки</span>
                        <textarea
                          className="textarea"
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
                        <p className="section-kicker">02 / ВВОДНЫЕ ФАЙЛЫ</p>
                        <label className="upload-label">
                          <PaperclipIcon />
                          Добавить файлы
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
                      <p className="helper">
                        Храним полное содержимое файлов локально, не только
                        названия. Передача сервису начнётся после явного запуска.
                        Не добавляйте секреты и персональные данные.
                      </p>
                      <div className="form-actions">
                        <button
                          className="primary"
                          disabled={block || !p.title.trim() || !p.goal.trim()}
                          onClick={() => patch({ step: 1 })}
                        >
                          К вопросам фасилитатора
                        </button>
                        <span className="notice-line">
                          Сроки, ограничения и детали уточним в диалоге.
                        </span>
                      </div>
                    </div>
                  ) : null}
                  {p.step === 1 ? (
                    <div className="facilitator-chat surface" aria-label="Вопросы фасилитатора">
                      <div className="chat-header">
                        <div className="question-top">
                          <span className="facilitator-icon" aria-hidden>
                            ✳
                          </span>
                          <div>
                            <strong>Фасилитатор</strong>
                            <small>
                              {answeredCount === -1
                                ? "Основные вопросы пройдены. Можно уточнить ответы."
                                : api.available
                                  ? "Вопросы для подготовки: ответы уйдут команде вместе с задачей."
                                  : "Вопросы для подготовки. Во время запуска фасилитатор сможет задать дополнительные."}
                            </small>
                          </div>
                        </div>
                      </div>
                      <div className="chat-thread" role="log" aria-label="Вопросы и ответы">
                        {QUESTIONS.slice(0, visibleQuestions).map((q, i) => (
                          <div className="chat-pair" key={q}>
                            <div
                              className={`chat-message facilitator${i === answeredCount ? " current" : ""}`}
                            >
                              <small>Фасилитатор · вопрос {i + 1}</small>
                              <p>{q}</p>
                            </div>
                            <label className="chat-message user answer">
                              <small>Ваш ответ</small>
                              <textarea
                                className="chat-answer"
                                name={`preparation-${i}`}
                                aria-label={q}
                                autoComplete="off"
                                rows={2}
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
                              {!p.answers[i].trim() ? (
                                <button
                                  type="button"
                                  className="chip"
                                  disabled={block}
                                  onClick={() =>
                                    patch({
                                      answers: p.answers.map((s, n) =>
                                        n === i ? "Пока не знаю" : s,
                                      ),
                                    })
                                  }
                                >
                                  Пока не знаю
                                </button>
                              ) : null}
                            </label>
                          </div>
                        ))}
                      </div>
                      <div className="chat-footer">
                        <small>
                          Ответы сохраняются в этом браузере по мере ввода.
                        </small>
                        <button
                          className="primary"
                          disabled={block || p.answers.some((a) => !a.trim())}
                          onClick={() => patch({ step: 2 })}
                        >
                          Посмотреть и выбрать команду
                        </button>
                      </div>
                    </div>
                  ) : null}
                  {p.step === 2 ? (
                    <>
                      <div className="team-pane surface">
                        <div className="team-pane-head">
                          <div>
                            <p className="section-kicker">01 / КОМАНДА</p>
                            <h2 className="section-title">Кто участвует</h2>
                          </div>
                        </div>
                        <div className="team-list">
                          <article className="team-role-row fixed">
                            <span className="team-role-number" aria-hidden>
                              ✳
                            </span>
                            <div>
                              <strong>Фасилитатор · связывает работу</strong>
                              <p>
                                Уточняет задачу, передаёт контекст ролям и
                                возвращается к вам за решением.
                              </p>
                            </div>
                            <span className="team-added-label">Всегда в команде</span>
                          </article>
                          {ROLES.map((r) => (
                            <label
                              className={`team-role-row selectable${p.team.includes(r.id) ? " chosen" : ""}`}
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
                              <div>
                                <strong>{r.name}</strong>
                                <p>{r.purpose}</p>
                                <small>Результат: {r.output}</small>
                              </div>
                            </label>
                          ))}
                        </div>
                        <label className="field">
                          <span className="field-label">
                            Что изменить в составе или какую экспертизу добавить?
                          </span>
                          <textarea
                            className="textarea"
                            name="team-note"
                            autoComplete="off"
                            rows={3}
                            value={p.teamNote}
                            onChange={(e) => patch({ teamNote: e.target.value })}
                            disabled={block}
                            placeholder="Например, нужен взгляд специалиста по закупкам…"
                          />
                        </label>
                        <p className="helper">
                          Сейчас это черновик выбора, не запущенные агенты. CTF
                          должен подтвердить доступность ролей. Нельзя создать
                          рабочего специалиста одним названием.
                        </p>
                      </div>
                      <div className="confirm-bar">
                        <button
                          className="secondary"
                          disabled={block}
                          onClick={() => patch({ step: 1 })}
                        >
                          ← Вернуться к вопросам
                        </button>
                        <span>Выбранные роли станут частью плана.</span>
                        <button
                          className="primary"
                          disabled={block || !p.team.length}
                          onClick={() => patch({ step: 3 })}
                        >
                          Команда подходит — к плану
                        </button>
                      </div>
                    </>
                  ) : null}
                  {p.step === 3 ? (
                    <>
                      <div className="team-pane surface">
                        <div className="team-pane-head">
                          <div>
                            <p className="section-kicker">02 / ПЛАН</p>
                            <h2 className="section-title">Как придём к результату</h2>
                          </div>
                          <button
                            className="secondary small"
                            disabled={block || p.stages.length >= MAX_PLAN_STAGES}
                            onClick={() =>
                              p.stages.length < MAX_PLAN_STAGES &&
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
                        </div>
                        <div className="plan-editor">
                          {p.stages.map((s, i) => (
                            <fieldset className="plan-edit-step" key={s.id}>
                              <legend>
                                <b>{String(i + 1).padStart(2, "0")}</b> Этап {i + 1}
                              </legend>
                              <label className="field">
                                <span className="field-label">Название этапа {i + 1}</span>
                                <input
                                  className="input"
                                  name={`stage-title-${i}`}
                                  autoComplete="off"
                                  value={s.title}
                                  disabled={block}
                                  onChange={(e) =>
                                    patch({
                                      stages: p.stages.map((x) =>
                                        x.id === s.id ? { ...x, title: e.target.value } : x,
                                      ),
                                    })
                                  }
                                />
                              </label>
                              <label className="field">
                                <span className="field-label">Результат этапа {i + 1}</span>
                                <textarea
                                  className="textarea"
                                  name={`stage-goal-${i}`}
                                  autoComplete="off"
                                  rows={2}
                                  value={s.goal}
                                  disabled={block}
                                  onChange={(e) =>
                                    patch({
                                      stages: p.stages.map((x) =>
                                        x.id === s.id ? { ...x, goal: e.target.value } : x,
                                      ),
                                    })
                                  }
                                />
                              </label>
                              {p.stages.length > 1 ? (
                                <button
                                  className="text-btn danger"
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
                            </fieldset>
                          ))}
                        </div>
                        <p className="helper">
                          В плане — от 1 до {MAX_PLAN_STAGES} этапов (ограничение
                          сервиса). Фактическое потребление покажем из CTF, когда
                          появятся подтверждённые данные.
                        </p>
                      </div>
                      <div className="confirm-bar">
                        <button
                          className="secondary"
                          disabled={block}
                          onClick={() => patch({ step: 2 })}
                        >
                          ← Вернуться к команде
                        </button>
                        <span>Это план. Работа запустится отдельно в рабочем пространстве.</span>
                        <button
                          className="primary"
                          disabled={
                            block ||
                            p.stages.length < 1 ||
                            p.stages.length > MAX_PLAN_STAGES ||
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
                      </div>
                    </>
                  ) : null}
                  {p.step === 1 ? (
                    <button
                      className="text-btn back-link"
                      disabled={block}
                      onClick={() => patch({ step: 0 })}
                    >
                      ← Вернуться к задаче
                    </button>
                  ) : null}
                </section>
              ) : null}
              {view === "workspace" && p ? (
                <section className="workspace" aria-labelledby="workspace-title">
                  {sample ? (
                    <div className="work-honesty sample-banner" role="note">
                      <div>
                        <strong>Учебный пример · CTF не запускался</strong>
                        <span>
                          Изучите этапы и версии. Данные и роли здесь
                          подготовлены заранее.
                        </span>
                      </div>
                      <button className="primary small" onClick={finishTour} disabled={block}>
                        Начать свой проект
                      </button>
                    </div>
                  ) : null}
                  <div className="workspace-heading">
                    <div>
                      <p className="eyebrow">РАБОЧЕЕ ПРОСТРАНСТВО</p>
                      <h1 className="page-title" id="workspace-title">
                        {p.title}
                      </h1>
                      <p className="lead">
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
                            onClick={() => {
                              try {
                                void perform("start", {
                                  ...projectPayload(p),
                                  stageId: p.stages[0].id,
                                  files: p.files,
                                });
                              } catch (e) {
                                setError(e instanceof Error ? e.message : "Проверьте план");
                              }
                            }}
                          >
                            {p.pending
                              ? "Повторить прежний запрос"
                              : "Запустить работу команды"}
                          </button>
                        </>
                      ) : null}
                      {!sample && run ? (
                        <button className="secondary" disabled={block} onClick={refresh}>
                          Обновить статус
                        </button>
                      ) : null}
                    </div>
                  </div>
                  {p.pending && !sample ? (
                    <div className="banner pending">
                      <span>
                        {p.pending.legacy || p.pending.action === "message"
                          ? "Сохранён запрос прежнего формата. Он не повторяется новым протоколом. Нужна сверка его исхода с сервером; данные не удалены."
                          : p.pending.rejected
                            ? "Прежний вопрос отклонён. Обновите статус; старый ответ не повторяется."
                            : "Исход доставки запроса не подтверждён. Это не терминальный outcome_unknown. Сначала обновите статус; повтор вручную использует прежние данные и тот же ID."}
                      </span>
                      <button
                        className="text-btn"
                        disabled={
                          block ||
                          p.pending.legacy ||
                          p.pending.rejected ||
                          p.pending.action === "message"
                        }
                        onClick={() =>
                          perform(p.pending!.action, p.pending!.body, p.pending!.targetId)
                        }
                      >
                        Повторить запрос
                      </button>
                    </div>
                  ) : null}
                  <div className="result-dashboard">
                    <section className="facilitator-chat surface conversation">
                      <div className="chat-header">
                        <div className="question-top">
                          <span className="facilitator-icon" aria-hidden>
                            ✳
                          </span>
                          <div>
                            <h2>Диалог с фасилитатором</h2>
                            <small>Работа над задачей</small>
                          </div>
                        </div>
                        <span className="status-pill">
                          {sample ? "Пример" : run ? labels[run.status] : "Не запущено"}
                        </span>
                      </div>
                      <div className="chat-thread" role="log" aria-label="История диалога">
                        {!run ? (
                          <div className="chat-message facilitator current">
                            <small>Ваш следующий шаг</small>
                            <p>
                              Контекст, материалы, команда и план собраны.{" "}
                              {api.available ? "Нажмите" : "После подключения CTF нажмите"}{" "}
                              «Запустить работу команды». Здесь появятся реальные
                              вопросы, объяснения и результаты.
                            </p>
                            <p className="muted">
                              До запуска ничего не передаём сервису. Сейчас нет
                              выполненных артефактов или измеренного потребления.
                            </p>
                          </div>
                        ) : (
                          projectMessages(p).map((m) => (
                            <article
                              key={m.id}
                              className={`chat-message ${m.role === "user" ? "user" : m.role === "facilitator" ? "facilitator" : "system"}`}
                            >
                              <small>
                                {m.role === "user"
                                  ? "Вы"
                                  : m.role === "facilitator"
                                    ? "Фасилитатор"
                                    : "Сервис"}
                                {sample ? " · учебный пример" : ""}
                              </small>
                              <p>{m.text}</p>
                            </article>
                          ))
                        )}
                        {isWaiting(run) && run?.question ? (
                          <FacilitatorQuestions
                            group={run.question}
                            draft={answerDraft}
                            disabled={block || sample || Boolean(p.pending) || !questionReady}
                            onChange={(answers) =>
                              patch({ answerDraft: { questionId: run.question!.id, answers } })
                            }
                            onSubmit={(answers) =>
                              void perform("answer", { questionId: run.question!.id, answers })
                            }
                          />
                        ) : null}
                        {run?.progress ? (
                          <p className="note" role="status">
                            {run.progress.stage === "interrupted"
                              ? "Связь с исполнением прервана. Ожидаем состояние сервера."
                              : run.progress.stage === "recovering"
                                ? "Сервис восстанавливает выполнение. Повторный запуск не создаём."
                                : "Сервис сообщает: выполнение продолжается."}
                            {run.progress.attempt
                              ? ` Попытка восстановления: ${run.progress.attempt}.`
                              : ""}
                            {run.progress.context_restored === true
                              ? " Восстановление контекста подтверждено сервером."
                              : ""}
                          </p>
                        ) : null}
                        {run?.status === "outcome_unknown" ? (
                          <p className="error-note" role="status">
                            Сервер сообщил неизвестный исход. Успех и сохранение не
                            подтверждены этим статусом. Проверьте версии и состояние
                            с командой; автоматического повтора или нового запуска нет.
                          </p>
                        ) : null}
                        {run?.status === "failed" ? (
                          <p className="error-note">
                            Запуск завершился ошибкой. ID: {run.id}. Новый запуск не
                            создаётся автоматически.
                          </p>
                        ) : null}
                      </div>
                      <form
                        className="chat-composer"
                        onSubmit={(e) => {
                          e.preventDefault();
                          try {
                            if (p.draftMessage.trim() && canRework(run) && a?.saved)
                              void perform("rework", {
                                ...projectPayload(p),
                                text: p.draftMessage.trim(),
                                stageId: selectedStage?.id,
                                parentRunId: run!.id,
                                sourceArtifactId: a.id,
                                documentId: a.documentId,
                                sourceVersion: a.version,
                                fileIds: p.files.map((f) => f.uploadedId),
                              });
                          } catch (e) {
                            setError(e instanceof Error ? e.message : "Проверьте план");
                          }
                        }}
                      >
                        <label className="field">
                          <span className="field-label">Вопрос или замечание к результату</span>
                          <textarea
                            className="textarea"
                            name="revision-message"
                            autoComplete="off"
                            rows={3}
                            disabled={
                              sample || block || Boolean(p.pending) || !canRework(run) || !a?.saved
                            }
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
                                ? canRework(run)
                                  ? "Доработка создаст новый запуск в этом же проекте. Выбранные документ, версия и история сохранят связь."
                                  : "Дождитесь завершения запуска. На вопросы отвечайте в форме выше; доработка не переоткрывает завершённый Run."
                                : "Черновик замечания сохранён. Для отправки нужен запуск CTF."}
                          </small>
                          <button
                            className="primary"
                            disabled={
                              sample ||
                              block ||
                              !canRework(run) ||
                              !a?.saved ||
                              Boolean(p.pending) ||
                              !p.draftMessage.trim()
                            }
                          >
                            Доработать в новом запуске
                          </button>
                        </div>
                      </form>
                    </section>
                    <aside className="results-pane surface">
                      <nav className="pane-tabs" aria-label="Панель проекта">
                        {(["artifacts", "team", "usage"] as const).map((t) => (
                          <button
                            key={t}
                            aria-pressed={sideTab === t}
                            className={sideTab === t ? "active" : ""}
                            onClick={() => setSideTab(t)}
                          >
                            {t === "artifacts" ? "Артефакты" : t === "team" ? "Команда" : "Замеры"}
                          </button>
                        ))}
                      </nav>
                      <div className="results-body">
                        {sideTab === "team" ? team() : null}
                        {sideTab === "usage" ? (
                          <>
                            <p className="section-kicker">ФАКТИЧЕСКОЕ ПОТРЕБЛЕНИЕ</p>
                            <h2 className="section-title">Замеры CTF</h2>
                            <p className="muted">
                              Данные текущего запуска, не сумма по проекту. Запусков в
                              истории: {projectRuns(p).length}.
                            </p>
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
                            <p className="section-kicker">РЕЗУЛЬТАТ ЭТАПА</p>
                            <h2 className="section-title">{selectedStage?.title}</h2>
                            <p className="muted">{selectedStage?.goal}</p>
                            {a ? (
                              <>
                                <div className="artifact-selects">
                                  <label className="field">
                                    <span className="field-label">Артефакт этапа</span>
                                    <select
                                      className="input"
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
                                  <label className="field">
                                    <span className="field-label">Версия артефакта</span>
                                    <select
                                      className="input"
                                      name="artifact-version"
                                      aria-label="Версия артефакта"
                                      value={a.id}
                                      onChange={(e) => setArtifactId(e.target.value)}
                                    >
                                      {versions.map((v) => (
                                        <option key={v.id} value={v.id}>
                                          v{v.version} · {v.title}
                                          {(sample ? v.approved : approved(p, v)) ? " · одобрена" : ""}
                                        </option>
                                      ))}
                                    </select>
                                  </label>
                                </div>
                                <div className="artifact">
                                  <div className="artifact-head">
                                    <span className="artifact-icon" aria-hidden>
                                      v{a.version}
                                    </span>
                                    <div>
                                      <h3>{a.title}</h3>
                                      <small>
                                        {sample
                                          ? "Учебная версия, не результат CTF"
                                          : a.saved
                                            ? `Сервер подтвердил запись${a.savedAt ? ` · ${new Date(a.savedAt).toLocaleString("ru")}` : ""}`
                                            : "Постоянное сохранение не подтверждено"}
                                      </small>
                                      {!sample ? (
                                        <small>
                                          {approved(p, a)
                                            ? "Одобрено вами · подтверждено сервером для этой версии"
                                            : "Пользовательское одобрение этой версии не подтверждено"}
                                        </small>
                                      ) : null}
                                    </div>
                                  </div>
                                  {a.content || !a.file ? (
                                    <Document content={a.content} />
                                  ) : (
                                    <p className="note">
                                      Файл «{a.file.name}» не текстовый или слишком
                                      большой для просмотра. Скачайте его, чтобы открыть.
                                    </p>
                                  )}
                                </div>
                                <div className="artifact-actions">
                                  {!sample ? (
                                    <button
                                      className="primary"
                                      disabled={
                                        block ||
                                        Boolean(p.pending) ||
                                        approved(p, a) ||
                                        a.id !== versions[0]?.id ||
                                        !a.saved
                                      }
                                      onClick={() =>
                                        perform("approve", { artifactId: a.id, version: a.version }, a.id)
                                      }
                                    >
                                      {approved(p, a) ? "Версия согласована" : "Согласовать результат"}
                                    </button>
                                  ) : null}
                                  {a.file && !sample ? (
                                    <a
                                      className="secondary"
                                      href={a.file.downloadUrl}
                                      download={a.file.name}
                                      onClick={() =>
                                        track("file_download_handed", {
                                          runId: artifactRunId(p, a.id),
                                          artifactId: a.id,
                                        })
                                      }
                                    >
                                      Скачать файл
                                      {a.file.sizeBytes !== null ? ` · ${bytes(a.file.sizeBytes)}` : ""}
                                    </a>
                                  ) : null}
                                  <button
                                    className="secondary"
                                    hidden={!a.content && Boolean(a.file)}
                                    onClick={() => {
                                      downloadText(a);
                                      track("text_download_handed", {
                                        runId: artifactRunId(p, a.id),
                                        artifactId: a.id,
                                      });
                                    }}
                                  >
                                    Скачать текст версии
                                  </button>
                                  {!sample ? (
                                    <button
                                      className="secondary"
                                      disabled={block || !a.saved || !p.serverId || Boolean(p.pending)}
                                      onClick={downloadZip}
                                    >
                                      Скачать ZIP версии
                                    </button>
                                  ) : null}
                                </div>
                                {!sample ? (
                                  <p className="helper">
                                    Для доработки напишите замечание в диалоге. Новая
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
            {onboarding && p ? (
              <aside className="aside" aria-label="Состояние работы">
                <h3>Сейчас</h3>
                <div className="aside-item">
                  <small>Этап</small>
                  <strong>{STEP_NAMES[p.step]}</strong>
                  <div className="progress-track" aria-hidden>
                    <div className="progress-fill" style={{ width: `${((p.step + 1) / 5) * 100}%` }} />
                  </div>
                </div>
                <div className="aside-item">
                  <small>Следующий шаг</small>
                  <strong>{nextStep}</strong>
                </div>
                <div className="aside-item">
                  <small>{GUIDE[p.step].title}</small>
                  <p>{GUIDE[p.step].text}</p>
                </div>
                <div className="aside-item">
                  <small>Сервис</small>
                  <strong>
                    {api.available
                      ? "CTF подключён — запуск после согласования плана"
                      : "CTF пока не подключён. Подготовка проекта работает локально."}
                  </strong>
                </div>
              </aside>
            ) : null}
          </div>
        </div>
      </div>
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
              <button autoFocus className="secondary small" onClick={() => setPreview(null)}>
                Закрыть просмотр
              </button>
            </div>
            <p className="muted">
              Без выполнения содержимого. Для текстовых файлов — первые 16 КБ,
              оригинал хранится полностью.
            </p>
            <pre>{preview.text}</pre>
          </section>
        </div>
      ) : null}
    </>
  );
}

const NAV_STEPS = [...STEP_NAMES, "Рабочее пространство"];
const GUIDE = [
  {
    title: "Не нужно начинать с идеального ТЗ",
    text: "Расскажите о ситуации и добавьте имеющиеся материалы. Команда получает содержание файлов только при явном запуске.",
  },
  {
    title: "Неизвестное можно назвать неизвестным",
    text: "Ответ «пока не знаю» лучше выдуманного факта. Во время работы фасилитатор вернётся за уточнением.",
  },
  {
    title: "Вы участвуете в выборе",
    text: "Вы видите, зачем нужна роль и что она должна передать дальше. Роли ИИ не заменяют проверку реальными специалистами.",
  },
  {
    title: "Каждый этап оставляет результат",
    text: "Артефакты и версии появятся в рабочем пространстве. Там можно задать вопрос, попросить доработку или согласовать результат.",
  },
];
const CAPABILITIES = [
  {
    kicker: "01 / РЕШЕНИЕ",
    title: "Разобраться в незнакомой области",
    text: "Собрать основания, сравнить варианты, обозначить неизвестное.",
  },
  {
    kicker: "02 / МАТЕРИАЛЫ",
    title: "Подготовить документ или план",
    text: "Приложить вводные и согласовать ожидаемый результат.",
  },
  {
    kicker: "03 / ПРОВЕРКА",
    title: "Посмотреть на задачу с разных сторон",
    text: "Обсудить состав AI-команды и проверить допущения.",
  },
];

function PaperclipIcon() {
  return (
    <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="m20.5 11.5-8.8 8.8a5 5 0 0 1-7.1-7.1l9.2-9.2a3.5 3.5 0 0 1 5 5l-9.2 9.2a2 2 0 0 1-2.8-2.8l8.5-8.5" />
    </svg>
  );
}
