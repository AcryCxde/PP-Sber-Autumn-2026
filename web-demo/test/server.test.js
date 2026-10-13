"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const path = require("node:path");

const root = path.join(__dirname, "..");
const base = "http://127.0.0.1:4181";
let child;

async function waitForServer() {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    try { const response = await fetch(`${base}/api/projects`); if (response.ok) return; } catch { /* waiting */ }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("Demo server did not start");
}
test.before(async () => {
  child = spawn(process.execPath, ["server.js"], { cwd: root, env: { ...process.env, PORT: "4181" }, stdio: "ignore" });
  await waitForServer();
});
test.after(() => child?.kill());

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
