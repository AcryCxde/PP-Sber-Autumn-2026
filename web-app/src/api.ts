import type { Run, SaveAck } from "./types.ts";
import { emitMetric } from "./metrics.ts";
export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
    public requestId: string,
  ) {
    super(message);
  }
}
const record = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);
const str = (v: unknown): v is string => typeof v === "string" && v.length > 0;
export function parseRun(v: unknown): Run {
  if (
    !record(v) ||
    !str(v.id) ||
    !["running", "clarification", "completed", "failed"].includes(
      String(v.status),
    )
  )
    throw new Error("schema");
  if (
    v.status === "clarification" &&
    (!record(v.question) || !str(v.question.id) || !str(v.question.text))
  )
    throw new Error("schema");
  if (
    v.status === "completed" &&
    (!record(v.result) ||
      !str(v.result.id) ||
      !str(v.result.title) ||
      typeof v.result.content !== "string")
  )
    throw new Error("schema");
  return {
    id: v.id,
    status: v.status as Run["status"],
    ...(v.status === "clarification"
      ? { question: v.question as Run["question"] }
      : {}),
    ...(v.status === "completed" ? { result: v.result as Run["result"] } : {}),
  };
}
export function parseSave(v: unknown): SaveAck {
  if (
    !record(v) ||
    v.saved !== true ||
    !str(v.artifactId) ||
    !str(v.version) ||
    !str(v.savedAt) ||
    !Number.isFinite(Date.parse(v.savedAt))
  )
    throw new Error("schema");
  return {
    saved: true,
    artifactId: v.artifactId,
    version: v.version,
    savedAt: v.savedAt,
  };
}
export class ProjectApi {
  constructor(
    private base: string,
    private enabled: boolean,
    private fetcher: typeof fetch = globalThis.fetch.bind(globalThis),
    private timeoutMs = 20000,
  ) {}
  get available() {
    try {
      const u = new URL(this.base);
      return (
        this.enabled &&
        ["http:", "https:"].includes(u.protocol) &&
        !u.username &&
        !u.password &&
        !u.search &&
        !u.hash
      );
    } catch {
      return false;
    }
  }
  async request<T>(
    path: string,
    parse: (v: unknown) => T,
    {
      body,
      requestId = crypto.randomUUID(),
      projectId,
      runId,
      zip = false,
    }: {
      body?: Record<string, unknown>;
      requestId?: string;
      projectId?: string;
      runId?: string;
      zip?: boolean;
    } = {},
  ): Promise<T> {
    if (!this.available)
      throw new ApiError(
        "unavailable",
        "Сервис не подключён. Черновик остаётся в этом браузере.",
        requestId,
      );
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    const started = performance.now();
    try {
      emitMetric("request_sent", { requestId, projectId, runId });
      const response = await this.fetcher(
        `${this.base.replace(/\/$/, "")}/${path}`,
        {
          method: body ? "POST" : "GET",
          body: body ? JSON.stringify(body) : undefined,
          headers: {
            "X-Correlation-ID": requestId,
            ...(body
              ? {
                  "Content-Type": "application/json",
                  "Idempotency-Key": requestId,
                }
              : {}),
          },
          credentials: "same-origin",
          signal: controller.signal,
        },
      );
      const value = zip ? await response.blob() : await response.text();
      emitMetric("response_received", {
        requestId,
        projectId,
        runId,
        durationMs: Math.round(performance.now() - started),
      });
      if (!response.ok)
        throw new ApiError(
          `http_${response.status}`,
          response.status === 401 || response.status === 403
            ? "Нет доступа к проекту. Проверьте сессию."
            : `Сервис вернул ошибку ${response.status}. Повторите запрос с тем же идентификатором.`,
          requestId,
        );
      if (zip) {
        const blob = value as Blob;
        const bytes = new Uint8Array(await blob.slice(0, 4).arrayBuffer());
        if (
          !/application\/(zip|octet-stream)/i.test(blob.type) ||
          bytes[0] !== 0x50 ||
          bytes[1] !== 0x4b ||
          ![
            [3, 4],
            [5, 6],
            [7, 8],
          ].some(([a, b]) => bytes[2] === a && bytes[3] === b)
        )
          throw new Error("schema");
        return parse(blob);
      }
      return parse(JSON.parse(value as string));
    } catch (e) {
      if (e instanceof ApiError) throw e;
      if (controller.signal.aborted)
        throw new ApiError(
          "timeout",
          "Время ожидания истекло. Исход операции неизвестен; повтор использует прежний идентификатор.",
          requestId,
        );
      if (e instanceof TypeError)
        throw new ApiError(
          "network",
          "Нет соединения с сервисом. Исход операции неизвестен; повтор использует прежний идентификатор.",
          requestId,
        );
      throw new ApiError(
        "invalid_response",
        "Ответ сервиса не соответствует контракту. Сохранение не подтверждено.",
        requestId,
      );
    } finally {
      clearTimeout(timer);
    }
  }
  create(body: Record<string, unknown>, requestId: string, projectId: string) {
    return this.request(
      "projects",
      (v) => {
        if (!record(v) || !str(v.id)) throw new Error("schema");
        return v.id;
      },
      { body, requestId, projectId },
    );
  }
  start(
    id: string,
    body: Record<string, unknown>,
    requestId: string,
    projectId: string,
  ) {
    return this.request(`projects/${encodeURIComponent(id)}/runs`, parseRun, {
      body,
      requestId,
      projectId,
    });
  }
  run(id: string, runId: string, projectId: string) {
    return this.request(
      `projects/${encodeURIComponent(id)}/runs/${encodeURIComponent(runId)}`,
      parseRun,
      { projectId, runId },
    );
  }
  answer(
    id: string,
    runId: string,
    body: Record<string, unknown>,
    requestId: string,
    projectId: string,
  ) {
    return this.request(
      `projects/${encodeURIComponent(id)}/runs/${encodeURIComponent(runId)}/answers`,
      parseRun,
      { body, requestId, projectId, runId },
    );
  }
  save(
    id: string,
    body: Record<string, unknown>,
    requestId: string,
    projectId: string,
  ) {
    return this.request(
      `projects/${encodeURIComponent(id)}/artifacts`,
      parseSave,
      { body, requestId, projectId, runId: str(body.runId) ? body.runId : undefined },
    );
  }
  zip(id: string, artifactId: string, projectId: string) {
    return this.request(
      `projects/${encodeURIComponent(id)}/artifacts/${encodeURIComponent(artifactId)}/zip`,
      (v) => v as Blob,
      { projectId, zip: true },
    );
  }
}
