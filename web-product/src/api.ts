import { parseRun, type Run } from "./domain.ts";
export class CtfApi {
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
      if (!response.ok)
        throw new Error(
          response.status === 401 || response.status === 403
            ? "Нет доступа к проекту. Проверьте сессию."
            : `Сервис вернул ошибку ${response.status}. Исход запроса не подтверждён.`,
        );
      return JSON.parse(text);
    } catch (e) {
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
  async zip(route: string): Promise<Blob> {
    if (!this.available) throw new Error("CTF не подключён");
    const controller = new AbortController(),
      timer = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await this.fetcher(
        `${this.base.replace(/\/$/, "")}/${route}`,
        { credentials: "same-origin", signal: controller.signal },
      );
      if (!response.ok)
        throw new Error(`Не удалось скачать ZIP: ${response.status}`);
      const blob = await response.blob(),
        head = new Uint8Array(await blob.slice(0, 4).arrayBuffer());
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
    } finally {
      clearTimeout(timer);
    }
  }
}
