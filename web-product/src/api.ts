import { parseRun, parseApproval, type Artifact, type Run } from "./domain.ts";
import type { EventName, Fields } from "./journey.ts";
export class ApiError extends Error {
  constructor(message: string, public code?: string, public status?: number) { super(message); }
}
export class CtfApi {
  onMetric?: (name: EventName, fields: Fields) => void;
  private metric(name: EventName, fields: Fields) {
    try {
      this.onMetric?.(name, fields);
    } catch {
      /* Research failure must not break product requests. */
    }
  }
  constructor(
    public base: string,
    public enabled: boolean,
    private fetcher: typeof fetch = globalThis.fetch.bind(globalThis),
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
  async request(
    route: string,
    body?: Record<string, unknown> | FormData,
    key?: string,
  ): Promise<unknown> {
    if (!this.available)
      throw new Error(
        "CTF пока не подключён. Контекст, файлы, команда и план остаются локальными черновиками.",
      );
    const controller = new AbortController(),
      timer = setTimeout(() => controller.abort(), 30000);
    const started = performance.now(),
      requestId = key || crypto.randomUUID();
    this.metric("request_started", { requestId });
    try {
      const response = await this.fetcher(
        `${this.base.replace(/\/$/, "")}/${route}`,
        {
          method: body ? "POST" : "GET",
          body:
            body instanceof FormData
              ? body
              : body
                ? JSON.stringify(body)
                : undefined,
          headers: {
            ...(key ? { "Idempotency-Key": key, "X-Correlation-ID": key } : {}),
            ...(body && !(body instanceof FormData)
              ? { "Content-Type": "application/json" }
              : {}),
          },
          credentials: "same-origin",
          signal: controller.signal,
        },
      );
      const text = await response.text();
      this.metric("request_received", {
        requestId,
        durationMs: performance.now() - started,
        status: `http_${response.status}`,
      });
      if (!response.ok) {
        let code: string | undefined;
        try {
          const value = JSON.parse(text);
          const candidate = value?.code ?? value?.error?.code;
          if (["unknown_fork", "answers_mismatch", "version_conflict", "idempotency_conflict"].includes(candidate)) code = candidate;
        } catch { /* Never display arbitrary server error bodies. */ }
        throw new ApiError(
          response.status === 401 || response.status === 403
            ? "Нет доступа к проекту. Проверьте сессию."
            : `Сервис вернул ошибку ${response.status}. Исход запроса не подтверждён.`,
          code, response.status,
        );
      }
      return JSON.parse(text);
    } catch (e) {
      this.metric("request_failed", {
        requestId,
        durationMs: performance.now() - started,
        status: "transport_or_response",
      });
      if (e instanceof Error && e.name === "AbortError")
        throw new Error(
          "Сервис не ответил вовремя. Повтор использует прежний ID и данные.",
        );
      throw e;
    } finally {
      clearTimeout(timer);
    }
  }
  async create(body: Record<string, unknown>, key: string): Promise<string> {
    const v = (await this.request("projects", body, key)) as any;
    if (!v || typeof v.id !== "string" || !v.id)
      throw new Error("Сервис не подтвердил ID проекта");
    return v.id;
  }
  async upload(
    projectId: string,
    fileId: string,
    name: string,
    blob: Blob,
    sha256: string,
  ): Promise<string> {
    const digest = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
    const actualHash = Array.from(new Uint8Array(digest)).map(n => n.toString(16).padStart(2, "0")).join("");
    if (actualHash !== sha256) throw new Error("Байты входного файла не совпадают с сохранённым SHA-256. Запуск остановлен.");
    const body = new FormData();
    body.append("file", blob, name);
    body.append("clientFileId", fileId);
    body.append("sha256", sha256);
    const v = (await this.request(
      `projects/${encodeURIComponent(projectId)}/files`,
      body,
      fileId,
    )) as any;
    if (!v || typeof v.id !== "string" || !v.id || v.sha256 !== sha256)
      throw new Error("Сервис не подтвердил содержимое файла");
    return v.id;
  }
  async run(
    route: string,
    body?: Record<string, unknown>,
    key?: string,
  ): Promise<Run> {
    return parseRun(await this.request(route, body, key));
  }
  async approve(route: string, body: Record<string, unknown>, key: string, artifact: Artifact) {
    return parseApproval(await this.request(route, body, key), artifact);
  }
  async zip(route: string): Promise<Blob> {
    if (!this.available) throw new Error("CTF не подключён");
    const controller = new AbortController(),
      timer = setTimeout(() => controller.abort(), 30000);
    const started = performance.now(),
      requestId = crypto.randomUUID();
    this.metric("request_started", { requestId });
    try {
      const response = await this.fetcher(
        `${this.base.replace(/\/$/, "")}/${route}`,
        { credentials: "same-origin", signal: controller.signal },
      );
      if (!response.ok)
        throw new Error(`Не удалось скачать ZIP: ${response.status}`);
      const blob = await response.blob(),
        head = new Uint8Array(await blob.slice(0, 4).arrayBuffer());
      this.metric("request_received", {
        requestId,
        durationMs: performance.now() - started,
        status: `http_${response.status}`,
      });
      if (
        !/application\/(zip|octet-stream)/i.test(blob.type) ||
        head[0] !== 80 ||
        head[1] !== 75 ||
        ![
          [3, 4],
          [5, 6],
          [7, 8],
        ].some(([a, b]) => head[2] === a && head[3] === b)
      )
        throw new Error("Сервис вернул не ZIP");
      return blob;
    } catch (e) {
      this.metric("request_failed", {
        requestId,
        durationMs: performance.now() - started,
        status: "zip_transport_or_format",
      });
      throw e;
    } finally {
      clearTimeout(timer);
    }
  }
}
