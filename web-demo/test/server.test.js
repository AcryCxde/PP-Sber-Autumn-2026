"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const root = path.join(__dirname, "..");
const base = "http://127.0.0.1:4181";
const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "coreteam-storage-test-"));
let child;

async function waitForServer() {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    try { const response = await fetch(`${base}/api/projects`); if (response.ok) return; } catch { /* waiting */ }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("Demo server did not start");
}
async function startServer() {
  child = spawn(process.execPath, ["server.js"], {
    cwd: root,
    env: { ...process.env, PORT: "4181", DATA_DIR: dataDir, STORAGE_TOKEN: "test-storage-token" },
    stdio: "ignore",
  });
  await waitForServer();
}
async function stopServer() {
  if (!child) return;
  child.kill();
  await new Promise((resolve) => child.once("exit", resolve));
  child = undefined;
}
test.before(startServer);
test.after(async () => { await stopServer(); fs.rmSync(dataDir, { recursive: true, force: true }); });

test("refuses another demo user's project", async () => {
  const response = await fetch(`${base}/api/projects/demo-isolated/runs`);
  assert.equal(response.status, 404);
});

test("unknown outcome is never completed", async () => {
  const response = await fetch(`${base}/api/projects/demo-main/runs`, {
    method: "POST",
    headers: { "content-type": "application/json", "idempotency-key": crypto.randomUUID() },
    body: JSON.stringify({ task: "Automated unknown path", simulateUnknown: true })
  });
  assert.equal(response.status, 201);
  const run = await response.json();
  await new Promise((resolve) => setTimeout(resolve, 1500));
  const events = await fetch(`${base}/api/projects/demo-main/runs/${run.id}/events`).then((result) => result.json());
  assert.equal(events.at(-1).type, "unknown");
  assert.equal(events.some((event) => event.type === "completed"), false);
});

test("runner artifact survives workspace cleanup and server restart", async () => {
  const runId = crypto.randomUUID();
  const operationId = crypto.randomUUID();
  const workspace = fs.mkdtempSync(path.join(os.tmpdir(), "runner-workspace-"));
  const resultPath = path.join(workspace, "result.md");
  const content = "# Confirmed result\n\nStored outside the runner container.\n";
  fs.writeFileSync(resultPath, content);
  const contentSha256 = crypto.createHash("sha256").update(content).digest("hex");

  const savedResponse = await fetch(`${base}/internal/artifacts`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: "Bearer test-storage-token" },
    body: JSON.stringify({ projectId: "demo-main", runId, operationId, title: "result.md", content, contentSha256 }),
  });
  assert.equal(savedResponse.status, 201);
  const saved = await savedResponse.json();
  assert.equal(saved.type, "artifact_saved");
  assert.match(saved.commitSha, /^[a-f0-9]{40}$/);

  const replayResponse = await fetch(`${base}/internal/artifacts`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: "Bearer test-storage-token" },
    body: JSON.stringify({ projectId: "demo-main", runId, operationId, title: "result.md", content, contentSha256 }),
  });
  assert.equal(replayResponse.status, 200);
  const replay = await replayResponse.json();
  assert.equal(replay.artifactId, saved.artifactId);
  assert.equal(replay.commitSha, saved.commitSha);

  const conflictingContent = "# Different result\n";
  const conflictingHash = crypto.createHash("sha256").update(conflictingContent).digest("hex");
  const conflictResponse = await fetch(`${base}/internal/artifacts`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: "Bearer test-storage-token" },
    body: JSON.stringify({
      projectId: "demo-main", runId, operationId, title: "result.md",
      content: conflictingContent, contentSha256: conflictingHash,
    }),
  });
  assert.equal(conflictResponse.status, 409);

  fs.rmSync(workspace, { recursive: true, force: true });
  await stopServer();
  await startServer();

  const download = await fetch(`${base}/api/projects/demo-main/artifacts/${saved.artifactId}`);
  assert.equal(download.status, 200);
  assert.equal(await download.text(), content);
  assert.equal(download.headers.get("x-artifact-commit"), saved.commitSha);

  const state = JSON.parse(fs.readFileSync(path.join(dataDir, "state.json"), "utf8"));
  assert.equal(Object.hasOwn(state.artifacts[saved.artifactId], "content"), false);
  assert.equal(state.operations[operationId].state, "confirmed");
});
