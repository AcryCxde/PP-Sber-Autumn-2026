// Local, opt-in study data. Never pass this record to the product API.
import { outcomeLabel } from "./study-labels.ts";
import {
  BASELINE_ACTIONS,
  BASELINE_TOOLS,
  BASELINE_BARRIERS,
  BASELINE_OUTCOMES,
  RESULT_KINDS,
  readBaselineSteps,
  type BaselineStep,
  type ResultKind,
} from "./baseline.ts";
export const JOURNEY_KEY = "coreteams-journey-study:v1";
export const CONSENT_VERSION = "journey-local-v1";
export type Phase = "baseline" | "prototype" | "interface";
export type Mode = "active" | "wait" | "pause" | "unobserved";
export const EVENT_NAMES = [
  "session_started",
  "session_finished",
  "session_recovered",
  "mode_changed",
  "project_created",
  "project_reopened",
  "guide_viewed",
  "preparation_step",
  "file_added",
  "file_removed",
  "team_changed",
  "plan_changed",
  "workspace_entered",
  "sample_viewed",
  "request_started",
  "request_received",
  "request_failed",
  "operation_failed",
  "usage_received",
  "run_received",
  "run_status",
  "clarification_shown",
  "answer_sent",
  "answer_acknowledged",
  "result_shown",
  "artifact_saved",
  "artifact_viewed",
  "artifact_accepted",
  "message_submitted",
  "message_acknowledged",
  "text_download_handed",
  "zip_download_handed",
  "workspace_left",
  "help_observed",
  "error_observed",
  "rework_observed",
  "baseline_result_saved",
  "baseline_step_started",
  "baseline_step_finished",
  "task_result_selected",
] as const;
export type EventName = (typeof EVENT_NAMES)[number];
export type Fields = {
  projectId?: string;
  runId?: string;
  requestId?: string;
  artifactId?: string;
  stageId?: string;
  questionId?: string;
  status?: string;
  step?: number;
  count?: number;
  durationMs?: number;
  inputTokens?: number;
  outputTokens?: number;
  requests?: number;
  computeSeconds?: number;
  cost?: number;
  currency?: string;
};
export type StudyEvent = Fields & {
  id: string;
  name: EventName;
  at: string;
  dedupe?: string;
};
export type Segment = { mode: Mode; from: string; to: string };
export type Ratings = {
  usefulness?: number;
  applicability?: number;
  clarity?: number;
  verifiability?: number;
};
export type Session = {
  id: string;
  participantCode: string;
  taskCode: string;
  phase: Phase;
  buildVersion: string;
  environment: "local" | "deployment" | "fixture";
  consent: { version: string; at: string };
  startedAt: string;
  cursor: string;
  mode: Mode;
  open: boolean;
  projectId?: string;
  endedAt?: string;
  outcome?: "completed" | "incomplete" | "abandoned";
  ratings: Ratings;
  segments: Segment[];
  events: StudyEvent[];
  baselineSteps?: BaselineStep[];
  resultKind?: ResultKind;
};
type StoragePort = Pick<Storage, "getItem" | "setItem">;
export type StudyState = { sessions: Session[]; issue: string };
const iso = (ms: number) => new Date(ms).toISOString();
const token = (v: unknown): v is string =>
  typeof v === "string" && /^[a-zA-Z0-9_.:-]{1,128}$/.test(v);
const modes: Mode[] = ["active", "wait", "pause", "unobserved"];
const phases: Phase[] = ["baseline", "prototype", "interface"];
const validDate = (v: unknown): v is string =>
  typeof v === "string" && Number.isFinite(Date.parse(v));
function cleanFields(input: Fields): Fields {
  const out: Fields = {};
  for (const key of [
    "projectId",
    "runId",
    "requestId",
    "artifactId",
    "stageId",
    "questionId",
    "status",
    "currency",
  ] as const)
    if (token(input[key])) out[key] = input[key];
  for (const key of [
    "step",
    "count",
    "durationMs",
    "inputTokens",
    "outputTokens",
    "requests",
    "computeSeconds",
    "cost",
  ] as const)
    if (
      typeof input[key] === "number" &&
      Number.isFinite(input[key]) &&
      input[key]! >= 0
    )
      out[key] = input[key];
  return out;
}
function cleanRatings(input: Ratings): Ratings {
  const out: Ratings = {};
  for (const key of [
    "usefulness",
    "applicability",
    "clarity",
    "verifiability",
  ] as const)
    if (Number.isInteger(input[key]) && input[key]! >= 1 && input[key]! <= 5)
      out[key] = input[key];
  return out;
}
// Explicit schema validation, including allowlisted fields on reload/export.
function read(raw: string | null): Session[] {
  if (raw === null) return [];
  const value = JSON.parse(raw);
  if (value.version !== 1 || !Array.isArray(value.sessions))
    throw Error("schema");
  const sessions: Session[] = value.sessions.map((s: any) => {
    if (
      !token(s.id) ||
      !/^[A-Z][0-9]{2,6}$/.test(s.participantCode) ||
      !/^[A-Z][0-9]{2,6}$/.test(s.taskCode) ||
      !phases.includes(s.phase) ||
      !token(s.buildVersion) ||
      !["local", "deployment", "fixture"].includes(s.environment) ||
      s.consent?.version !== CONSENT_VERSION ||
      !validDate(s.consent.at) ||
      !validDate(s.startedAt) ||
      !validDate(s.cursor) ||
      !modes.includes(s.mode) ||
      typeof s.open !== "boolean" ||
      !s.ratings ||
      !Array.isArray(s.segments) ||
      !Array.isArray(s.events) ||
      (s.projectId !== undefined && !token(s.projectId)) ||
      (s.endedAt !== undefined && !validDate(s.endedAt)) ||
      (!s.open &&
        (!s.endedAt ||
          !["completed", "incomplete", "abandoned"].includes(s.outcome)))
    )
      throw Error("session");
    const segments = s.segments.map((x: any) => {
      if (
        !modes.includes(x.mode) ||
        !validDate(x.from) ||
        !validDate(x.to) ||
        Date.parse(x.to) < Date.parse(x.from)
      )
        throw Error("interval");
      return { mode: x.mode, from: x.from, to: x.to };
    });
    const events = s.events.map((e: any) => {
      if (
        !token(e.id) ||
        !EVENT_NAMES.includes(e.name) ||
        !validDate(e.at) ||
        (e.dedupe !== undefined && !token(e.dedupe))
      )
        throw Error("event");
      return {
        id: e.id,
        name: e.name,
        at: e.at,
        ...cleanFields(e),
        ...(e.dedupe ? { dedupe: e.dedupe } : {}),
      };
    });
    if (
      s.resultKind !== undefined &&
      !Object.hasOwn(RESULT_KINDS, s.resultKind)
    )
      throw Error("result kind");
    const baselineSteps = readBaselineSteps(s.baselineSteps);
    if (baselineSteps.length && s.phase !== "baseline")
      throw Error("baseline phase");
    return {
      id: s.id,
      participantCode: s.participantCode,
      taskCode: s.taskCode,
      phase: s.phase,
      buildVersion: s.buildVersion,
      environment: s.environment,
      consent: { version: CONSENT_VERSION, at: s.consent.at },
      startedAt: s.startedAt,
      cursor: s.cursor,
      mode: s.mode,
      open: s.open,
      projectId: s.projectId,
      endedAt: s.endedAt,
      outcome: s.outcome,
      ratings: cleanRatings(s.ratings),
      segments,
      events,
      baselineSteps,
      resultKind: s.resultKind,
    };
  });
  if (
    new Set(sessions.map((s) => s.id)).size !== sessions.length ||
    sessions.filter((s) => s.open).length > 1
  )
    throw Error("duplicates");
  return sessions;
}
function tick(s: Session, at: string) {
  if (Date.parse(at) < Date.parse(s.cursor))
    throw Error(
      "Часы устройства переведены назад. Завершите замер после проверки времени.",
    );
  if (at === s.cursor) return;
  const previous = s.segments.at(-1);
  if (previous?.mode === s.mode && previous.to === s.cursor) previous.to = at;
  else s.segments.push({ mode: s.mode, from: s.cursor, to: at });
  s.cursor = at;
}
export class JourneyRecorder {
  private state: StudyState = { sessions: [], issue: "" };
  private raw: string | null = null;
  private listeners = new Set<() => void>();
  constructor(
    private storage: StoragePort,
    private now = () => Date.now(),
    private uuid = () => crypto.randomUUID(),
  ) {
    try {
      this.raw = storage.getItem(JOURNEY_KEY);
      this.state = { sessions: read(this.raw), issue: "" };
      const open = this.active();
      if (open)
        this.change((s) => {
          // Time since last checkpoint is unknown, NOT observed work or server wait.
          s.mode = "unobserved";
          tick(s, iso(this.now()));
          s.mode = "pause";
          this.append(s, "session_recovered", {}, undefined);
        });
    } catch {
      this.state = {
        sessions: [],
        issue:
          "Журнал замеров недоступен или повреждён. Исходные данные не перезаписаны. Выгрузите резервную копию хранилища и проверьте браузер.",
      };
    }
  }
  getSnapshot = () => this.state;
  subscribe = (fn: () => void) => {
    this.listeners.add(fn);
    return () => {
      this.listeners.delete(fn);
    };
  };
  active() {
    return this.state.sessions.find((s) => s.open);
  }
  private publish(next: StudyState) {
    this.state = next;
    this.listeners.forEach((fn) => fn());
  }
  private commit(sessions: Session[]) {
    if (this.state.issue) return false;
    try {
      if (this.storage.getItem(JOURNEY_KEY) !== this.raw)
        throw Error("Журнал изменился в другой вкладке. Обновите страницу.");
      const raw = JSON.stringify({ version: 1, sessions });
      this.storage.setItem(JOURNEY_KEY, raw);
      this.raw = raw;
      this.publish({ sessions, issue: "" });
      return true;
    } catch (e) {
      this.publish({
        ...this.state,
        issue: `Сбор остановлен: ${e instanceof Error ? e.message : "не удалось сохранить замер"}. Данные могут быть неполными.`,
      });
      return false;
    }
  }
  private change(fn: (s: Session) => void) {
    if (this.state.issue || !this.active()) return false;
    try {
      const sessions = structuredClone(this.state.sessions),
        s = sessions.find((s) => s.open)!;
      fn(s);
      return this.commit(sessions);
    } catch (e) {
      this.publish({ ...this.state, issue: String(e) });
      return false;
    }
  }
  private append(s: Session, name: EventName, fields: Fields, dedupe?: string) {
    s.events.push({
      id: this.uuid(),
      name,
      at: iso(this.now()),
      ...cleanFields(fields),
      ...(dedupe ? { dedupe } : {}),
    });
  }
  start(input: {
    participantCode: string;
    taskCode: string;
    phase: Phase;
    consent: boolean;
    buildVersion: string;
    environment: Session["environment"];
  }) {
    if (
      this.active() ||
      this.state.issue ||
      !input.consent ||
      !phases.includes(input.phase) ||
      !/^[A-Z][0-9]{2,6}$/.test(input.participantCode) ||
      !/^[A-Z][0-9]{2,6}$/.test(input.taskCode) ||
      !token(input.buildVersion) ||
      !["local", "deployment", "fixture"].includes(input.environment)
    )
      return false;
    const at = iso(this.now());
    const s: Session = {
      id: this.uuid(),
      participantCode: input.participantCode,
      taskCode: input.taskCode,
      phase: input.phase,
      buildVersion: input.buildVersion,
      environment: input.environment,
      consent: { version: CONSENT_VERSION, at },
      startedAt: at,
      cursor: at,
      mode: "active",
      open: true,
      ratings: {},
      segments: [],
      events: [],
    };
    this.append(s, "session_started", {});
    return this.commit([...this.state.sessions, s]);
  }
  checkpoint() {
    return this.change((s) => tick(s, iso(this.now())));
  }
  mode(mode: Mode) {
    if (!modes.includes(mode) || this.active()?.mode === mode) return false;
    return this.change((s) => {
      tick(s, iso(this.now()));
      s.mode = mode;
      this.append(s, "mode_changed", { status: mode });
    });
  }
  record(
    name: EventName,
    fields: Fields = {},
    dedupe?: string,
    manual = false,
  ) {
    const active = this.active();
    if (
      !active ||
      !EVENT_NAMES.includes(name) ||
      (!manual && active.phase === "baseline")
    )
      return false;
    if (
      dedupe &&
      (!token(dedupe) ||
        active.events.some((e) => e.dedupe === dedupe && e.name === name))
    )
      return false;
    if (
      fields.projectId &&
      (!token(fields.projectId) ||
        (active.projectId && active.projectId !== fields.projectId))
    )
      return false;
    return this.change((s) => {
      tick(s, iso(this.now()));
      if (fields.projectId && !s.projectId) s.projectId = fields.projectId;
      this.append(s, name, fields, dedupe);
    });
  }
  beginBaselineStep(
    action: BaselineStep["action"],
    tool: BaselineStep["tool"],
  ) {
    const active = this.active();
    if (
      active?.phase !== "baseline" ||
      active.baselineSteps?.some((s) => !s.endedAt) ||
      active.resultKind ||
      !Object.hasOwn(BASELINE_ACTIONS, action) ||
      !Object.hasOwn(BASELINE_TOOLS, tool)
    )
      return false;
    return this.change((s) => {
      tick(s, iso(this.now()));
      s.mode = "active";
      const step = { id: this.uuid(), action, tool, startedAt: s.cursor };
      (s.baselineSteps ||= []).push(step);
      this.append(s, "baseline_step_started", { step: s.baselineSteps.length });
    });
  }
  endBaselineStep(
    barrier: keyof typeof BASELINE_BARRIERS,
    outcome: keyof typeof BASELINE_OUTCOMES,
  ) {
    if (
      this.active()?.phase !== "baseline" ||
      !this.active()?.baselineSteps?.some((s) => !s.endedAt) ||
      !Object.hasOwn(BASELINE_BARRIERS, barrier) ||
      !Object.hasOwn(BASELINE_OUTCOMES, outcome)
    )
      return false;
    return this.change((s) => {
      tick(s, iso(this.now()));
      Object.assign(
        s.baselineSteps!.find((x) => !x.endedAt)!,
        { endedAt: s.cursor, barrier, outcome },
      );
      this.append(s, "baseline_step_finished", {
        step: s.baselineSteps!.length,
      });
    });
  }
  saveBaselineResult(kind: ResultKind) {
    const active = this.active();
    if (
      active?.phase !== "baseline" ||
      !Object.hasOwn(RESULT_KINDS, kind) ||
      active.resultKind ||
      !active.baselineSteps?.length ||
      active.baselineSteps.some((s) => !s.endedAt)
    )
      return false;
    return this.change((s) => {
      tick(s, iso(this.now()));
      s.resultKind = kind;
      this.append(s, "baseline_result_saved", {}, "baseline-save");
    });
  }
  finish(outcome: Session["outcome"], ratings: Ratings) {
    if (!["completed", "incomplete", "abandoned"].includes(outcome || ""))
      return false;
    return this.change((s) => {
      tick(s, iso(this.now()));
      const unfinished = s.baselineSteps?.find((x) => !x.endedAt);
      if (unfinished)
        Object.assign(unfinished, {
          endedAt: s.cursor,
          barrier: "unknown",
          outcome: "unfinished",
        });
      s.open = false;
      s.endedAt = s.cursor;
      s.outcome = outcome;
      s.ratings = cleanRatings(ratings);
      this.append(s, "session_finished", { status: outcome });
    });
  }
  remove(id: string) {
    return this.commit(this.state.sessions.filter((s) => s.id !== id));
  }
}
export function summary(s: Session) {
  const totals = { active: 0, wait: 0, pause: 0, unobserved: 0 };
  for (const segment of s.segments)
    totals[segment.mode] +=
      (Date.parse(segment.to) - Date.parse(segment.from)) / 1000;
  const selected = s.events.find((e) => e.name === "task_result_selected");
  const firstSaved =
    s.phase === "baseline"
      ? s.events.find((e) => e.name === "baseline_result_saved")
      : selected
        ? s.events.find(
            (e) =>
              e.name === "artifact_saved" &&
              e.artifactId === selected.artifactId,
          )
        : undefined;
  const unknownBeforeSave =
    firstSaved &&
    s.segments.some(
      (x) =>
        x.mode === "unobserved" &&
        Date.parse(x.from) < Date.parse(firstSaved.at),
    );
  const usage: Fields = {};
  const perRun = new Map<string, Fields>();
  for (const e of s.events.filter(e => e.name === "run_received" && e.runId)) perRun.set(e.runId!, {});
  for (const e of s.events.filter((e) => e.name === "usage_received")) {
    const key = e.runId || "legacy-unscoped";
    const previous = perRun.get(key) || {};
    for (const key of [
      "inputTokens",
      "outputTokens",
      "requests",
      "computeSeconds",
      "cost",
      "currency",
    ] as const)
      if (e[key] !== undefined) Object.assign(previous, { [key]: e[key] });
    perRun.set(key, previous);
  }
  const values = [...perRun.values()];
  for (const key of ["inputTokens", "outputTokens", "requests", "computeSeconds"] as const)
    if (values.length && values.every(v => v[key] !== undefined)) usage[key] = values.reduce((sum, v) => sum + v[key]!, 0);
  if (values.length && values.every(v => v.cost !== undefined && v.currency && v.currency === values[0].currency)) {
    usage.cost = values.reduce((sum, v) => sum + v.cost!, 0);
    usage.currency = values[0].currency;
  }
  return {
    session_id: s.id,
    participant_code: s.participantCode,
    task_code: s.taskCode,
    phase: s.phase,
    baseline_steps_n:
      s.phase === "baseline" ? s.baselineSteps?.length || 0 : null,
    baseline_barriers_n:
      s.phase === "baseline"
        ? s.baselineSteps?.filter(
            (x) => x.barrier && !["none", "unknown"].includes(x.barrier),
          ).length || 0
        : null,
    result_kind: s.resultKind || null,
    build_version: s.buildVersion,
    environment: s.environment,
    project_id: s.projectId || null,
    run_id: s.events.find((e) => e.name === "run_received")?.runId || null,
    runs_n: new Set(s.events.filter(e => e.name === "run_received" && e.runId).map(e => e.runId)).size,
    started_at: s.startedAt,
    ended_at: s.endedAt || null,
    outcome: s.outcome || null,
    outcome_label: outcomeLabel(s),
    elapsed_s:
      (Date.parse(s.endedAt || s.cursor) - Date.parse(s.startedAt)) / 1000,
    active_s: totals.active,
    wait_s: totals.wait,
    pause_s: totals.pause,
    unobserved_s: totals.unobserved,
    first_saved_at: firstSaved?.at || null,
    selected_result_artifact_id: selected?.artifactId || null,
    elapsed_to_saved_s: firstSaved
      ? (Date.parse(firstSaved.at) - Date.parse(s.startedAt)) / 1000
      : null,
    time_complete_to_saved: firstSaved ? !unknownBeforeSave : null,
    errors_n: s.events.filter((e) => e.name === "error_observed").length,
    help_requests_n: s.events.filter((e) => e.name === "help_observed").length,
    rework_n: s.events.filter((e) => e.name === "rework_observed").length,
    downloads_handed_n: s.events.filter((e) =>
      ["text_download_handed", "zip_download_handed"].includes(e.name),
    ).length,
    transport_failures_n: s.events.filter((e) => e.name === "request_failed")
      .length,
    failed_runs_n: new Set(
      s.events
        .filter((e) => e.name === "run_status" && e.status === "failed")
        .map((e) => e.runId),
    ).size,
    failed_operations_n: new Set(
      s.events
        .filter((e) => e.name === "operation_failed")
        .map((e) => e.requestId),
    ).size,
    input_tokens: usage.inputTokens ?? null,
    output_tokens: usage.outputTokens ?? null,
    service_requests: usage.requests ?? null,
    compute_seconds: usage.computeSeconds ?? null,
    cost: usage.cost ?? null,
    currency: usage.currency ?? null,
    ...s.ratings,
  };
}
export function compare(
  baseline: Session | undefined,
  prototype: Session | undefined,
  comparable: boolean,
) {
  const blocked: string[] = [];
  if (!baseline || !prototype)
    return { blocked: ["Выберите две сессии."], delta: null };
  if (baseline.phase !== "baseline" || prototype.phase !== "prototype")
    blocked.push("Нужны baseline и реальный прогон, не проверка интерфейса.");
  if (
    baseline.participantCode !== prototype.participantCode ||
    baseline.taskCode !== prototype.taskCode
  )
    blocked.push("Коды участника и задачи должны совпадать.");
  if (!comparable)
    blocked.push(
      "Подтвердите сопоставимость задачи и условий; учтите эффект обучения.",
    );
  if (
    baseline.open ||
    prototype.open ||
    baseline.outcome !== "completed" ||
    prototype.outcome !== "completed"
  )
    blocked.push("Оба прогона должны быть завершены с результатом.");
  if (prototype.environment === "fixture")
    blocked.push("Тестовая заглушка не является пользовательским результатом.");
  if (!prototype.events.some((e) => e.name === "run_received"))
    blocked.push("Нет подтверждённого ответа запуска сервиса.");
  const a = summary(baseline),
    b = summary(prototype);
  if (a.elapsed_to_saved_s === null || b.elapsed_to_saved_s === null)
    blocked.push("В обоих прогонах нужна отметка сохранённого результата.");
  if (a.unobserved_s > 0 || b.unobserved_s > 0)
    blocked.push("Есть ненаблюдаемые интервалы; сравнение времени неполно.");
  return {
    blocked,
    delta: blocked.length
      ? null
      : {
          elapsed_to_saved_s: b.elapsed_to_saved_s! - a.elapsed_to_saved_s!,
          active_s: b.active_s - a.active_s,
          wait_s: b.wait_s - a.wait_s,
          pause_s: b.pause_s - a.pause_s,
          errors_n: b.errors_n - a.errors_n,
          help_requests_n: b.help_requests_n - a.help_requests_n,
          rework_n: b.rework_n - a.rework_n,
          usefulness:
            a.usefulness !== undefined && b.usefulness !== undefined
              ? b.usefulness - a.usefulness
              : null,
          applicability:
            a.applicability !== undefined && b.applicability !== undefined
              ? b.applicability - a.applicability
              : null,
          clarity:
            a.clarity !== undefined && b.clarity !== undefined
              ? b.clarity - a.clarity
              : null,
          verifiability:
            a.verifiability !== undefined && b.verifiability !== undefined
              ? b.verifiability - a.verifiability
              : null,
        },
  };
}
export function exportStudy(
  sessions: Session[],
  pair?: ReturnType<typeof compare>,
) {
  // Re-validate and project allowlists so even malformed imported storage cannot leak text.
  const safe = read(JSON.stringify({ version: 1, sessions }));
  return {
    schema: "coreteams-journey-v1",
    scope: "local-consented-study",
    exportedAt: new Date().toISOString(),
    limitations:
      "Client-observed timings; manual activity modes. No server analytics receipt. Repeated-task learning prevents causal attribution. Download handoff is not proof of a disk save. Counts are recorded observations, not proof none were missed.",
    sessions: safe,
    summaries: safe.map(summary),
    baseline_steps: safe.flatMap((s) =>
      (s.baselineSteps || []).map((step, index) => {
        const end = step.endedAt || s.cursor;
        const time = { active_s: 0, wait_s: 0, pause_s: 0, unobserved_s: 0 };
        for (const segment of s.segments) {
          const ms = Math.max(
            0,
            Math.min(Date.parse(end), Date.parse(segment.to)) -
              Math.max(Date.parse(step.startedAt), Date.parse(segment.from)),
          );
          time[`${segment.mode}_s` as keyof typeof time] += ms / 1000;
        }
        return {
          session_id: s.id,
          participant_code: s.participantCode,
          task_code: s.taskCode,
          step_n: index + 1,
          action: step.action,
          tool: step.tool,
          started_at: step.startedAt,
          ended_at: step.endedAt || null,
          barrier: step.barrier || null,
          outcome: step.outcome || null,
          elapsed_s: (Date.parse(end) - Date.parse(step.startedAt)) / 1000,
          ...time,
        };
      }),
    ),
    comparison: pair || null,
  };
}
export function csv(sessions: Session[]) {
  const rows = exportStudy(sessions).summaries;
  return csvRows(rows);
}
export function csvBaselineSteps(sessions: Session[]) {
  return csvRows(exportStudy(sessions).baseline_steps);
}
function csvRows(rows: Record<string, unknown>[]) {
  const keys = [...new Set(rows.flatMap((r) => Object.keys(r)))];
  const cell = (x: unknown) =>
    `"${(x === null || x === undefined ? "" : String(x)).replace(/"/g, '""')}"`;
  return (
    "\uFEFF" +
    [
      keys.map(cell).join(","),
      ...rows.map((r) =>
        keys.map((k) => cell((r as Record<string, unknown>)[k])).join(","),
      ),
    ].join("\r\n")
  );
}
