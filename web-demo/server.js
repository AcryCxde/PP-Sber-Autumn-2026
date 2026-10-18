"use strict";

const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { GitArtifactStorage } = require("./git-storage");
const { MetadataStore } = require("./metadata");

const root = __dirname;
const dataDir = path.resolve(process.env.DATA_DIR || path.join(root, "data"));
const stateFile = path.join(dataDir, "state.json");
const publicDir = path.join(root, "public");
const artifactStorage = new GitArtifactStorage(
  process.env.STORAGE_ROOT_PATH ||
    process.env.ARTIFACT_REPO_PATH ||
    path.join(dataDir, "artifact-repository"),
);
const metadata = new MetadataStore(process.env.DATABASE_URL);
const storageToken = process.env.STORAGE_TOKEN || "";
const subscribers = new Map();
const terminalRunStatuses = new Set([
  "completed", "failed", "cancelled", "limit_stopped", "limit_reached",
  "outcome_unknown", "unknown",
]);
let storageQueue = Promise.resolve();

const now = () => new Date().toISOString();
const id = () => crypto.randomUUID();

class OperationConflictError extends Error {}

function initialState() {
  return {
    projects: [
      { id: "demo-main", name: "Пересмотр учебной программы", owner: "demo-user", budget: 10, spent: 0 },
      { id: "demo-isolated", name: "Изолированный тестовый проект", owner: "demo-other", budget: 10, spent: 0 }
    ],
    runs: {}, events: {}, artifacts: {}, operations: {}, idempotency: {}
  };
}
function load() {
  fs.mkdirSync(dataDir, { recursive: true });
  if (!fs.existsSync(stateFile)) fs.writeFileSync(stateFile, JSON.stringify(initialState(), null, 2));
  const state = JSON.parse(fs.readFileSync(stateFile, "utf8"));
  state.operations ||= {};
  return state;
}
function save(state) {
  fs.mkdirSync(dataDir, { recursive: true });
  const temporary = `${stateFile}.tmp-${process.pid}`;
  fs.writeFileSync(temporary, JSON.stringify(state, null, 2));
  fs.renameSync(temporary, stateFile);
}
function json(res, status, body) {
  res.writeHead(status, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" });
  res.end(JSON.stringify(body));
}
function projectFor(state, projectId) { return state.projects.find((project) => project.id === projectId); }
function visibleProject(req) { return req.headers["x-demo-user"] || "demo-user"; }
function authorize(state, req, projectId) {
  const project = projectFor(state, projectId);
  return project && project.owner === visibleProject(req) ? project : null;
}
function emit(state, run, type, payload, { mirror = true } = {}) {
  const event = { eventId: id(), projectId: run.projectId, runId: run.id, sequence: (state.events[run.id] || []).length + 1, createdAt: now(), type, payload };
  state.events[run.id] = [...(state.events[run.id] || []), event];
  save(state);
  if (mirror) metadata.appendEvent(run.projectId, event).catch((error) => console.error("metadata event write failed", error));
  for (const res of subscribers.get(run.id) || []) res.write(`id: ${event.eventId}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
  return event;
}
function readBody(req) {
  return new Promise((resolve, reject) => {
    let body = "";
    req.on("data", (chunk) => { body += chunk; if (Buffer.byteLength(body, "utf8") > 10 * 1024 * 1024) reject(new Error("Слишком большой запрос")); });
    req.on("end", () => { try { resolve(body ? JSON.parse(body) : {}); } catch { reject(new Error("Некорректный JSON")); } });
  });
}
function internalAuthorized(req) {
  if (!storageToken) return false;
  const supplied = req.headers.authorization?.replace(/^Bearer\s+/i, "") || "";
  const expected = Buffer.from(storageToken);
  const actual = Buffer.from(supplied);
  return expected.length === actual.length && crypto.timingSafeEqual(expected, actual);
}
function safeRun(run) { return { id: run.id, projectId: run.projectId, status: run.status, task: run.task, createdAt: run.createdAt, updatedAt: run.updatedAt }; }
function deterministicUuid(value) {
  const bytes = crypto.createHash("sha256").update(value).digest().subarray(0, 16);
  bytes[6] = (bytes[6] & 0x0f) | 0x50;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = bytes.toString("hex");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
function withStorageLock(work) {
  const pending = storageQueue.then(work, work);
  storageQueue = pending.catch(() => undefined);
  return pending;
}
function preparedEvent(state, run, type, payload, offset) {
  return { eventId: id(), projectId: run.projectId, runId: run.id, sequence: (state.events[run.id] || []).length + offset, createdAt: now(), type, payload };
}
function publishPrepared(state, run, events) {
  state.events[run.id] = [...(state.events[run.id] || []), ...events];
  save(state);
  for (const event of events) {
    for (const res of subscribers.get(run.id) || []) res.write(`id: ${event.eventId}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
  }
}
async function persistArtifact(state, run, { content, title = "итоговый-разбор.md", operationId }) {
  return withStorageLock(async () => {
    const contentSha256 = crypto.createHash("sha256").update(content, "utf8").digest("hex");
    const stableOperationId = operationId || deterministicUuid(`${run.projectId}:${run.id}:${contentSha256}`);
    const prior = state.operations[stableOperationId];
    if (prior && (
      prior.projectId !== run.projectId ||
      prior.runId !== run.id ||
      prior.contentSha256 !== contentSha256
    )) {
      throw new OperationConflictError("Operation-Id уже связан с другим артефактом");
    }
    if (prior?.state === "confirmed" && state.artifacts[prior.artifactId]) return state.artifacts[prior.artifactId];

    const gitPath = artifactStorage.artifactPath(run.projectId, run.id);
    const operation = { operationId: stableOperationId, runId: run.id, projectId: run.projectId, state: "intent", gitPath, contentSha256, createdAt: prior?.createdAt || now(), updatedAt: now() };
    state.operations[stableOperationId] = operation;
    save(state);
    await metadata.createIntent(run.projectId, run, operation);

    const committed = artifactStorage.commitArtifact({ projectId: run.projectId, runId: run.id, operationId: stableOperationId, content, contentSha256 });
    const artifact = {
      id: prior?.artifactId || id(), projectId: run.projectId, runId: run.id,
      operationId: stableOperationId, createdAt: now(), name: title,
      mimeType: "text/markdown", ...committed,
      gitTreeUrl: `git:${committed.commitSha}:${committed.gitPath}`,
    };
    const events = [
      preparedEvent(state, run, "artifact_saved", { artifactId: artifact.id, name: artifact.name, commitSha: artifact.commitSha, contentSha256: artifact.contentSha256, downloadUrl: `/api/projects/${run.projectId}/artifacts/${artifact.id}` }, 1),
      preparedEvent(state, run, "completed", { message: "Итоговый артефакт сохранён.", artifactId: artifact.id }, 2),
    ];
    await metadata.confirmArtifact(run.projectId, run, operation, artifact, events);

    operation.state = "confirmed";
    operation.commitSha = artifact.commitSha;
    operation.artifactId = artifact.id;
    operation.updatedAt = now();
    state.artifacts[artifact.id] = artifact;
    run.status = "completed";
    run.artifactId = artifact.id;
    run.updatedAt = now();
    publishPrepared(state, run, events);
    return artifact;
  });
}
async function completeRun(runId, outcome = "completed") {
  const state = load(); const run = state.runs[runId]; if (!run || run.status !== "running") return;
  if (outcome === "failed") { run.status = "failed"; run.updatedAt = now(); await metadata.ensureRun(run.projectId, run); emit(state, run, "failed", { message: "Работу не удалось завершить. Данные проекта сохранены." }); return; }
  if (outcome === "unknown") { run.status = "outcome_unknown"; run.updatedAt = now(); await metadata.ensureRun(run.projectId, run); emit(state, run, "run.outcome_unknown", { message: "Связь с исполнителем потеряна. Итог не подтверждён." }); return; }
  const content = `# Итоговый разбор\n\nЗадача: ${run.task}\n\n## Статус\n\nЭто демонстрационный артефакт, сохранённый сервером после завершения Run.\n\n## Открытые вопросы\n\n- Проверить выводы с владельцем проекта.\n`;
  try {
    await persistArtifact(state, run, { content });
  } catch (error) {
    console.error("artifact persistence failed", error);
    run.status = "outcome_unknown";
    run.updatedAt = now();
    await metadata.ensureRun(run.projectId, run);
    emit(state, run, "run.outcome_unknown", { message: "Результат сформирован, но подтверждение внешнего хранилища не получено." });
  }
}
function serveStatic(res, file) {
  const content = fs.readFileSync(path.join(publicDir, file));
  const types = { ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".html": "text/html; charset=utf-8", ".png": "image/png", ".woff2": "font/woff2" };
  res.writeHead(200, { "content-type": types[path.extname(file)] || "application/octet-stream" }); res.end(content);
}
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://localhost"); const segments = url.pathname.split("/").filter(Boolean);
  if (req.method === "GET" && url.pathname === "/health/ready") {
    try {
      fs.accessSync(dataDir, fs.constants.R_OK | fs.constants.W_OK);
      fs.accessSync(artifactStorage.repository, fs.constants.R_OK | fs.constants.W_OK);
      await metadata.ready();
      return json(res, 200, { status: "ready", database: metadata.enabled ? "ready" : "disabled", storage: "ready" });
    } catch {
      return json(res, 503, { status: "not_ready" });
    }
  }
  const state = load();
  if (req.method === "GET" && ["/", "/v9", "/v9/"].includes(url.pathname)) return serveStatic(res, "v9/index.html");
  if (req.method === "GET" && url.pathname === "/v8") return serveStatic(res, "index.html");
  const v9Files = ["/v9/app.js", "/v9/style.css", "/assets/v9/team.png", "/assets/v9/guide.png", "/assets/v9/result.png", "/assets/v9/inter-cyrillic-400-normal.woff2", "/assets/v9/inter-cyrillic-600-normal.woff2", "/assets/v9/inter-latin-400-normal.woff2", "/assets/v9/inter-latin-600-normal.woff2"];
  if (req.method === "GET" && v9Files.includes(url.pathname)) return serveStatic(res, url.pathname.slice(1));
  if (req.method === "GET" && url.pathname === "/app.css") return serveStatic(res, "app.css");
  if (req.method === "GET" && url.pathname === "/extra.css") return serveStatic(res, "extra.css");
  if (req.method === "GET" && url.pathname === "/assets/book-spread.png") {
    const content = fs.readFileSync(path.join(publicDir, "assets", "book-spread.png"));
    res.writeHead(200, { "content-type": "image/png", "cache-control": "public, max-age=3600" });
    return res.end(content);
  }
  if (req.method === "GET" && url.pathname === "/assets/CoreTeams-v1-720p.mp4") {
    const videoPath = path.join(publicDir, "assets", "CoreTeams-v1-720p.mp4");
    const size = fs.statSync(videoPath).size;
    const headers = { "content-type": "video/mp4", "accept-ranges": "bytes", "cache-control": "public, max-age=3600" };
    if (req.headers.range) {
      const match = /^bytes=(\d*)-(\d*)$/.exec(req.headers.range);
      const start = match?.[1] ? Number(match[1]) : Math.max(0, size - Number(match?.[2] || 0));
      const end = match?.[1] && match?.[2] ? Math.min(Number(match[2]), size - 1) : size - 1;
      if (!match || start >= size || start > end) {
        res.writeHead(416, { "content-range": `bytes */${size}` });
        return res.end();
      }
      res.writeHead(206, { ...headers, "content-range": `bytes ${start}-${end}/${size}`, "content-length": end - start + 1 });
      return fs.createReadStream(videoPath, { start, end }).pipe(res);
    }
    res.writeHead(200, { ...headers, "content-length": size });
    return fs.createReadStream(videoPath).pipe(res);
  }
  if (req.method === "GET" && url.pathname === "/app.js") {
    const content = fs.readFileSync(path.join(publicDir, "app.js"));
    res.writeHead(200, { "content-type": "text/javascript; charset=utf-8" });
    return res.end(content);
  }
  if (req.method === "POST" && url.pathname === "/internal/artifacts") {
    if (!internalAuthorized(req)) return json(res, 401, { error: "Недействительный сервисный ключ." });
    try {
      const body = await readBody(req);
      const project = projectFor(state, body.projectId);
      if (!project) return json(res, 404, { error: "Проект недоступен или не существует." });
      if (typeof body.runId !== "string" || typeof body.operationId !== "string" || typeof body.content !== "string") {
        return json(res, 422, { error: "Некорректный контракт артефакта." });
      }
      const actualHash = crypto.createHash("sha256").update(body.content, "utf8").digest("hex");
      if (body.contentSha256 !== actualHash) return json(res, 422, { error: "Контрольная сумма не совпадает." });
      const existingRun = state.runs[body.runId];
      if (existingRun && existingRun.projectId !== body.projectId) {
        return json(res, 403, { error: "Запуск не принадлежит указанному проекту." });
      }
      const priorOperation = state.operations[body.operationId];
      const exactReplay = priorOperation?.state === "confirmed" &&
        priorOperation.projectId === body.projectId &&
        priorOperation.runId === body.runId &&
        priorOperation.contentSha256 === actualHash;
      if (existingRun && terminalRunStatuses.has(existingRun.status) && !exactReplay) {
        return json(res, 409, { error: "Запись в завершённый запуск запрещена." });
      }
      const run = existingRun || {
        id: body.runId, projectId: body.projectId, status: "running",
        task: String(body.title || "Runner artifact"), clientRequestId: `runner:${body.runId}`,
        createdAt: now(), updatedAt: now(),
      };
      state.runs[run.id] = run;
      state.events[run.id] ||= [];
      save(state);
      await metadata.ensureRun(run.projectId, run);
      const replay = state.operations[body.operationId]?.state === "confirmed";
      const artifact = await persistArtifact(state, run, {
        content: body.content,
        title: String(body.title || "result.md"),
        operationId: body.operationId,
      });
      return json(res, replay ? 200 : 201, {
        type: "artifact_saved", artifactId: artifact.id, projectId: artifact.projectId,
        runId: artifact.runId, operationId: artifact.operationId, gitPath: artifact.gitPath,
        commitSha: artifact.commitSha, contentSha256: artifact.contentSha256,
        sizeBytes: artifact.sizeBytes, createdAt: artifact.createdAt,
        downloadUrl: `/api/projects/${artifact.projectId}/artifacts/${artifact.id}`,
      });
    } catch (error) {
      if (error instanceof OperationConflictError) {
        return json(res, 409, { error: error.message });
      }
      console.error("internal artifact save failed", error);
      return json(res, 503, { error: "Не удалось подтвердить запись артефакта." });
    }
  }
  if (req.method === "GET" && url.pathname === "/api/projects") return json(res, 200, state.projects.filter((p) => p.owner === visibleProject(req)).map(({ id: projectId, name, budget, spent }) => ({ id: projectId, name, budget, spent })));
  if (segments[0] === "api" && segments[1] === "projects" && segments[3] === "artifacts" && segments.length === 5) {
    const projectId = segments[2]; const project = authorize(state, req, projectId); if (!project) return json(res, 404, { error: "Проект недоступен или не существует." });
    const artifact = state.artifacts[segments[4]];
    if (!artifact || artifact.projectId !== projectId) return json(res, 404, { error: "Подтверждённого артефакта нет." });
    try {
      const content = artifactStorage.readArtifact(artifact.commitSha, artifact.gitPath);
      res.writeHead(200, { "content-type": "text/markdown; charset=utf-8", "content-disposition": `attachment; filename="${artifact.name}"`, "x-artifact-commit": artifact.commitSha });
      return res.end(content);
    } catch {
      return json(res, 503, { error: "Сохранённая версия временно недоступна." });
    }
  }
  if (segments[0] === "api" && segments[1] === "projects" && segments[3] === "runs") {
    const projectId = segments[2]; const project = authorize(state, req, projectId); if (!project) return json(res, 404, { error: "Проект недоступен." });
    if (req.method === "GET" && segments.length === 4) return json(res, 200, Object.values(state.runs).filter((run) => run.projectId === projectId).map(safeRun));
    if (req.method === "POST" && segments.length === 4) {
      try {
        const body = await readBody(req); const key = req.headers["idempotency-key"]; if (!key) return json(res, 400, { error: "Нужен ключ идемпотентности." });
        const scopedKey = `${projectId}:${key}`;
        if (state.idempotency[scopedKey]) return json(res, 200, safeRun(state.runs[state.idempotency[scopedKey]]));
        const task = String(body.task || "").trim(); if (!task) return json(res, 400, { error: "Опишите задачу." });
        if (project.budget - project.spent < 1) { const run = { id: id(), projectId, status: "limit_reached", task, clientRequestId: key, createdAt: now(), updatedAt: now() }; state.runs[run.id] = run; state.idempotency[scopedKey] = run.id; await metadata.ensureRun(projectId, run); emit(state, run, "limit_reached", { message: "Тестовый лимит исчерпан. Работа не запускалась." }); return json(res, 201, safeRun(run)); }
        project.spent += 1; const run = { id: id(), projectId, status: "queued", task, clientRequestId: key, createdAt: now(), updatedAt: now() }; state.runs[run.id] = run; state.idempotency[scopedKey] = run.id; await metadata.ensureRun(projectId, run); emit(state, run, "queued", { message: "Работа поставлена в очередь." });
        setTimeout(() => { const fresh = load(); const current = fresh.runs[run.id]; if (!current) return; current.status = "running"; current.updatedAt = now(); metadata.ensureRun(projectId, current).then(() => { emit(fresh, current, "run_started", { message: "Работа началась." }); emit(fresh, current, "progress", { message: "Готовим безопасный итоговый материал." }); const outcome = body.simulateUnknown ? "unknown" : body.simulateFailure ? "failed" : "completed"; setTimeout(() => completeRun(run.id, outcome).catch((error) => console.error(error)), 900); }).catch((error) => console.error("metadata run update failed", error)); }, 300);
        return json(res, 201, safeRun(run));
      } catch (error) { return json(res, 400, { error: error.message }); }
    }
    const runId = segments[4]; const run = state.runs[runId]; if (!run || run.projectId !== projectId) return json(res, 404, { error: "Запуск недоступен." });
    if (req.method === "GET" && segments[5] === "events") return json(res, 200, state.events[runId] || []);
    if (req.method === "GET" && segments[5] === "stream") { res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "keep-alive" }); res.write(": connected\n\n"); const list = subscribers.get(runId) || new Set(); list.add(res); subscribers.set(runId, list); req.on("close", () => list.delete(res)); return; }
    if (req.method === "GET" && segments[5] === "artifact") {
      const artifact = state.artifacts[run.artifactId];
      if (!artifact) return json(res, 404, { error: "Подтверждённого артефакта нет." });
      try {
        const content = artifactStorage.readArtifact(artifact.commitSha, artifact.gitPath);
        res.writeHead(200, { "content-type": "text/markdown; charset=utf-8", "content-disposition": `attachment; filename="${artifact.name}"`, "x-artifact-commit": artifact.commitSha });
        return res.end(content);
      } catch {
        return json(res, 503, { error: "Сохранённая версия временно недоступна." });
      }
    }
  }
  json(res, 404, { error: "Не найдено." });
});
const port = Number(process.env.PORT || 4173);
const host = process.env.HOST || "127.0.0.1";
async function start() {
  load();
  artifactStorage.initialize();
  await metadata.initialize();
  server.listen(port, host, () => console.log(`CoreTeams demo: http://${host}:${port}`));
}
async function shutdown() {
  server.close();
  await metadata.close();
}
process.once("SIGTERM", () => shutdown().finally(() => process.exit(0)));
process.once("SIGINT", () => shutdown().finally(() => process.exit(0)));
start().catch((error) => { console.error("CoreTeams demo failed to start", error); process.exit(1); });
