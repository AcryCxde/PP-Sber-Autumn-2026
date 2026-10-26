"use strict";

// Real local server + real Git, deliberately WITHOUT PostgreSQL. Opt-in suite.
const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const net = require("node:net");
const { spawn } = require("node:child_process");

const root = path.join(__dirname, "..");
const token = "synthetic-storage-contract-token";
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let child;
let base;
let dataDir;

async function unusedPort() {
  const reservation = net.createServer();
  await new Promise((resolve, reject) => {
    reservation.once("error", reject);
    reservation.listen(0, "127.0.0.1", resolve);
  });
  const port = reservation.address().port;
  await new Promise((resolve) => reservation.close(resolve));
  return port;
}

test.before(async () => {
  const port = await unusedPort();
  base = `http://127.0.0.1:${port}`;
  dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "coreteams-http-contract-"));
  let errors = "";
  child = spawn(process.execPath, ["server.js"], {
    cwd: root,
    env: {
      ...process.env, PORT: String(port), HOST: "127.0.0.1", DATA_DIR: dataDir,
      ARTIFACT_REPO_PATH: path.join(dataDir, "git"), DATABASE_URL: "", STORAGE_TOKEN: token,
    },
    stdio: ["ignore", "ignore", "pipe"],
  });
  child.stderr.on("data", (chunk) => { errors = `${errors}${chunk}`.slice(-2000); });
  for (let attempt = 0; attempt < 60; attempt += 1) {
    if (child.exitCode !== null) throw new Error(`Test server exited: ${errors}`);
    try {
      const response = await fetch(`${base}/api/projects`, { signal: AbortSignal.timeout(500) });
      if (response.ok) return;
    } catch { /* bounded startup wait */ }
    await delay(100);
  }
  throw new Error(`Test server startup timeout: ${errors}`);
});

test.after(async () => {
  if (child && child.exitCode === null) {
    const exited = new Promise((resolve) => child.once("exit", resolve));
    child.kill();
    await exited;
  }
  if (dataDir) fs.rmSync(dataDir, { recursive: true, force: true });
});

function payload(overrides = {}) {
  const content = "# Проверка результата\n";
  return {
    projectId: "demo-main", runId: crypto.randomUUID(), operationId: crypto.randomUUID(),
    title: "result.md", content,
    contentSha256: crypto.createHash("sha256").update(content).digest("hex"), ...overrides,
  };
}

async function postArtifact(body, authorization = `Bearer ${token}`) {
  const headers = { "content-type": "application/json" };
  if (authorization !== null) headers.authorization = authorization;
  return fetch(`${base}/internal/artifacts`, { method: "POST", headers, body: JSON.stringify(body) });
}

function dataSnapshot(directory = dataDir, relative = "") {
  const snapshot = {};
  for (const entry of fs.readdirSync(path.join(directory, relative), { withFileTypes: true })) {
    const name = path.join(relative, entry.name);
    if (entry.isDirectory()) Object.assign(snapshot, dataSnapshot(directory, name));
    else snapshot[name.replaceAll(path.sep, "/")] = crypto.createHash("sha256")
      .update(fs.readFileSync(path.join(directory, name))).digest("hex");
  }
  return snapshot;
}

test("internal save rejects missing/wrong service auth without creating operations", async () => {
  for (const authorization of [null, "Bearer wrong-token"]) {
    const body = payload();
    assert.equal((await postArtifact(body, authorization)).status, 401);
    const state = JSON.parse(fs.readFileSync(path.join(dataDir, "state.json"), "utf8"));
    assert.equal(state.operations[body.operationId], undefined);
  }
});

test("download isolation checks artifact project and demo owner", async () => {
  const response = await postArtifact(payload());
  assert.equal(response.status, 201);
  const saved = await response.json();
  const own = await fetch(`${base}/api/projects/demo-main/artifacts/${saved.artifactId}`);
  assert.equal(own.status, 200);
  assert.equal(await own.text(), "# Проверка результата\n");
  assert.equal((await fetch(`${base}/api/projects/demo-main/artifacts/${saved.artifactId}`, {
    headers: { "x-demo-user": "demo-other" },
  })).status, 404);
  assert.equal((await fetch(`${base}/api/projects/demo-isolated/artifacts/${saved.artifactId}`, {
    headers: { "x-demo-user": "demo-other" },
  })).status, 404);
});

test("operation ID cannot be replayed against another run", async () => {
  const body = payload();
  assert.equal((await postArtifact(body)).status, 201);
  assert.equal((await postArtifact({ ...body, runId: crypto.randomUUID() })).status, 409);
});

test("a request project must match the existing run, even on a confirmed replay", async () => {
  const body = payload();
  const savedResponse = await postArtifact(body);
  assert.equal(savedResponse.status, 201);
  const saved = await savedResponse.json();

  const replayResponse = await postArtifact(body);
  assert.equal(replayResponse.status, 200);
  const replay = await replayResponse.json();
  assert.equal(replay.artifactId, saved.artifactId);
  assert.equal(replay.commitSha, saved.commitSha);

  const before = dataSnapshot();
  const response = await postArtifact({ ...body, projectId: "demo-isolated" });
  assert.equal(response.status, 409);
  assert.deepEqual(await response.json(), { error: "Запуск не принадлежит указанному проекту." });
  assert.deepEqual(dataSnapshot(), before);
});

for (const [label, status, eventType, simulation] of [
  ["unknown", "outcome_unknown", "run.outcome_unknown", "simulateUnknown"],
  ["failed", "failed", "failed", "simulateFailure"],
]) {
  test(`late artifact save must not reopen a terminal ${label} Run`, async () => {
    const confirmedBody = payload();
    const confirmedResponse = await postArtifact(confirmedBody);
    assert.equal(confirmedResponse.status, 201);
    const confirmed = await confirmedResponse.json();

    const created = await fetch(`${base}/api/projects/demo-main/runs`, {
      method: "POST",
      headers: { "content-type": "application/json", "idempotency-key": crypto.randomUUID() },
      body: JSON.stringify({ task: `Synthetic terminal ${label}`, [simulation]: true }),
    });
    assert.equal(created.status, 201);
    const run = await created.json();
    const eventsUrl = `${base}/api/projects/demo-main/runs/${run.id}/events`;
    let before = [];
    for (let attempt = 0; attempt < 50; attempt += 1) {
      before = await fetch(eventsUrl).then((response) => response.json());
      if (before.at(-1)?.type === eventType) break;
      await delay(100);
    }
    assert.equal(before.at(-1)?.type, eventType, "Terminal fixture did not reach the target state");

    const filesBefore = dataSnapshot();
    const response = await postArtifact(payload({ runId: run.id }));
    assert.equal(response.status, 409);
    assert.deepEqual(await response.json(), { error: "Запись в завершённый запуск запрещена." });
    const after = await fetch(eventsUrl).then((result) => result.json());
    const runs = await fetch(`${base}/api/projects/demo-main/runs`).then((result) => result.json());
    assert.deepEqual({
      status: runs.find((item) => item.id === run.id).status,
      events: after,
      files: dataSnapshot(),
    }, { status, events: before, files: filesBefore }, "Late save changed an already terminal Run");

    const replayResponse = await postArtifact(confirmedBody);
    assert.equal(replayResponse.status, 200);
    const replay = await replayResponse.json();
    assert.equal(replay.artifactId, confirmed.artifactId);
    assert.equal(replay.commitSha, confirmed.commitSha);
  });
}
