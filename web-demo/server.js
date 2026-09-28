"use strict";

const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");

const root = __dirname;
const dataDir = path.join(root, "data");
const stateFile = path.join(dataDir, "state.json");
const publicDir = path.join(root, "public");
const subscribers = new Map();

const now = () => new Date().toISOString();
const id = (prefix) => `${prefix}_${crypto.randomUUID()}`;

function initialState() {
  return {
    projects: [
      { id: "demo-main", name: "Пересмотр учебной программы", owner: "demo-user", budget: 10, spent: 0 },
      { id: "demo-isolated", name: "Изолированный тестовый проект", owner: "demo-other", budget: 10, spent: 0 }
    ],
    runs: {}, events: {}, artifacts: {}, idempotency: {}
  };
}
function load() {
  fs.mkdirSync(dataDir, { recursive: true });
  if (!fs.existsSync(stateFile)) fs.writeFileSync(stateFile, JSON.stringify(initialState(), null, 2));
  return JSON.parse(fs.readFileSync(stateFile, "utf8"));
}
function save(state) { fs.writeFileSync(stateFile, JSON.stringify(state, null, 2)); }
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
function emit(state, run, type, payload) {
  const event = { eventId: id("evt"), projectId: run.projectId, runId: run.id, sequence: (state.events[run.id] || []).length + 1, createdAt: now(), type, payload };
  state.events[run.id] = [...(state.events[run.id] || []), event];
  save(state);
  for (const res of subscribers.get(run.id) || []) res.write(`id: ${event.eventId}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
  return event;
}
function readBody(req) {
  return new Promise((resolve, reject) => {
    let body = "";
    req.on("data", (chunk) => { body += chunk; if (body.length > 50_000) reject(new Error("Слишком большой запрос")); });
    req.on("end", () => { try { resolve(body ? JSON.parse(body) : {}); } catch { reject(new Error("Некорректный JSON")); } });
  });
}
function safeRun(run) { return { id: run.id, projectId: run.projectId, status: run.status, task: run.task, createdAt: run.createdAt, updatedAt: run.updatedAt }; }
function completeRun(runId, outcome = "completed") {
  const state = load(); const run = state.runs[runId]; if (!run || run.status !== "running") return;
  if (outcome === "failed") { run.status = "failed"; run.updatedAt = now(); emit(state, run, "failed", { message: "Работу не удалось завершить. Данные проекта сохранены." }); return; }
  if (outcome === "unknown") { run.status = "unknown"; run.updatedAt = now(); emit(state, run, "unknown", { message: "Связь с исполнителем потеряна. Итог не подтверждён." }); return; }
  const artifact = { id: id("artifact"), projectId: run.projectId, runId, createdAt: now(), name: "итоговый-разбор.md", content: `# Итоговый разбор\n\nЗадача: ${run.task}\n\n## Статус\n\nЭто демонстрационный артефакт, сохранённый сервером после завершения Run.\n\n## Открытые вопросы\n\n- Проверить выводы с владельцем проекта.\n` };
  state.artifacts[artifact.id] = artifact; run.status = "completed"; run.artifactId = artifact.id; run.updatedAt = now();
  emit(state, run, "artifact_saved", { artifactId: artifact.id, name: artifact.name }); emit(state, run, "completed", { message: "Итоговый артефакт сохранён." });
}
function serveStatic(res, file) {
  const content = fs.readFileSync(path.join(publicDir, file));
  res.writeHead(200, { "content-type": file.endsWith(".css") ? "text/css; charset=utf-8" : "text/html; charset=utf-8" }); res.end(content);
}
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://localhost"); const segments = url.pathname.split("/").filter(Boolean); const state = load();
  if (req.method === "GET" && url.pathname === "/") return serveStatic(res, "index.html");
  if (req.method === "GET" && url.pathname === "/app.css") return serveStatic(res, "app.css");
  if (req.method === "GET" && url.pathname === "/app.js") {
    const content = fs.readFileSync(path.join(publicDir, "app.js"));
    res.writeHead(200, { "content-type": "text/javascript; charset=utf-8" });
    return res.end(content);
  }
  if (req.method === "GET" && url.pathname === "/api/projects") return json(res, 200, state.projects.filter((p) => p.owner === visibleProject(req)).map(({ id: projectId, name, budget, spent }) => ({ id: projectId, name, budget, spent })));
  if (segments[0] === "api" && segments[1] === "projects" && segments[3] === "runs") {
    const projectId = segments[2]; const project = authorize(state, req, projectId); if (!project) return json(res, 404, { error: "Проект недоступен." });
    if (req.method === "GET" && segments.length === 4) return json(res, 200, Object.values(state.runs).filter((run) => run.projectId === projectId).map(safeRun));
    if (req.method === "POST" && segments.length === 4) {
      try {
        const body = await readBody(req); const key = req.headers["idempotency-key"]; if (!key) return json(res, 400, { error: "Нужен ключ идемпотентности." });
        if (state.idempotency[key]) return json(res, 200, safeRun(state.runs[state.idempotency[key]]));
        const task = String(body.task || "").trim(); if (!task) return json(res, 400, { error: "Опишите задачу." });
        if (project.budget - project.spent < 1) { const run = { id: id("run"), projectId, status: "limit_reached", task, createdAt: now(), updatedAt: now() }; state.runs[run.id] = run; state.idempotency[key] = run.id; emit(state, run, "limit_reached", { message: "Тестовый лимит исчерпан. Работа не запускалась." }); return json(res, 201, safeRun(run)); }
        project.spent += 1; const run = { id: id("run"), projectId, status: "queued", task, createdAt: now(), updatedAt: now() }; state.runs[run.id] = run; state.idempotency[key] = run.id; emit(state, run, "queued", { message: "Работа поставлена в очередь." });
        setTimeout(() => { const fresh = load(); const current = fresh.runs[run.id]; if (!current) return; current.status = "running"; current.updatedAt = now(); emit(fresh, current, "run_started", { message: "Работа началась." }); emit(fresh, current, "progress", { message: "Готовим безопасный итоговый материал." }); const outcome = body.simulateUnknown ? "unknown" : body.simulateFailure ? "failed" : "completed"; setTimeout(() => completeRun(run.id, outcome), 900); }, 300);
        return json(res, 201, safeRun(run));
      } catch (error) { return json(res, 400, { error: error.message }); }
    }
    const runId = segments[4]; const run = state.runs[runId]; if (!run || run.projectId !== projectId) return json(res, 404, { error: "Запуск недоступен." });
    if (req.method === "GET" && segments[5] === "events") return json(res, 200, state.events[runId] || []);
    if (req.method === "GET" && segments[5] === "stream") { res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "keep-alive" }); res.write(": connected\n\n"); const list = subscribers.get(runId) || new Set(); list.add(res); subscribers.set(runId, list); req.on("close", () => list.delete(res)); return; }
    if (req.method === "GET" && segments[5] === "artifact") { const artifact = state.artifacts[run.artifactId]; if (!artifact) return json(res, 404, { error: "Подтверждённого артефакта нет." }); res.writeHead(200, { "content-type": "text/markdown; charset=utf-8", "content-disposition": `attachment; filename="${artifact.name}"` }); return res.end(artifact.content); }
  }
  json(res, 404, { error: "Не найдено." });
});
const port = Number(process.env.PORT || 4173);
server.listen(port, "127.0.0.1", () => console.log(`CoreTeams demo: http://127.0.0.1:${port}`));
