import { test } from "node:test";
import assert from "node:assert/strict";
import { ApiError, ProjectApi, parseRun, parseSave } from "../src/api.ts";
import { loadProjects } from "../src/storage.ts";
import { sessionMetrics } from "../src/metrics.ts";
const response = (v: unknown) =>
  new Response(JSON.stringify(v), {
    headers: { "Content-Type": "application/json" },
  });
test("default unavailable and credential URLs send no request", async () => {
  for (const base of [
    "",
    "https://user:password@test.invalid",
    "file:///tmp/test",
  ]) {
    let called = false;
    const api = new ProjectApi(base, true, async () => {
      called = true;
      return response({});
    });
    await assert.rejects(
      api.create({}, "stable-id", "p"),
      (e: ApiError) => e.code === "unavailable",
    );
    assert.equal(called, false);
  }
});
test("idempotency and correlation IDs preserved across timeout/network retries", async () => {
  const ids: string[] = [];
  let attempts = 0;
  const api = new ProjectApi(
    "https://test.invalid/api/",
    true,
    async (url, options) => {
      assert.equal(url, "https://test.invalid/api/projects");
      const headers = options!.headers as Record<string, string>;
      ids.push(headers["Idempotency-Key"]);
      assert.equal(headers["X-Correlation-ID"], "stable");
      if (++attempts === 1) throw new TypeError("network");
      return response({ id: "server-p" });
    },
  );
  await assert.rejects(
    api.create({ title: "test" }, "stable", "p"),
    (e: ApiError) => e.code === "network",
  );
  assert.equal(await api.create({ title: "test" }, "stable", "p"), "server-p");
  assert.deepEqual(ids, ["stable", "stable"]);
});
test("timeout aborts the transport and exposes unknown outcome", async () => {
  const api = new ProjectApi(
    "https://test.invalid",
    true,
    async (_u, o) =>
      new Promise((_resolve, reject) =>
        o!.signal!.addEventListener("abort", () =>
          reject(new DOMException("aborted", "AbortError")),
        ),
      ),
    5,
  );
  await assert.rejects(
    api.create({}, "timeout-id", "p"),
    (e: ApiError) => e.code === "timeout" && e.requestId === "timeout-id",
  );
});
test("response_received waits for the full response body, not just headers", async () => {
  const body = new ReadableStream({
    start(controller) {
      setTimeout(() => {
        controller.enqueue(new TextEncoder().encode('{"id":"server-p"}'));
        controller.close();
      }, 30);
    },
  });
  const api = new ProjectApi(
    "https://test.invalid",
    true,
    async () => new Response(body),
  );
  const pending = api.create({}, "slow-body", "p");
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal(
    sessionMetrics().filter(
      (m) => m.name === "response_received" && m.requestId === "slow-body",
    ).length,
    0,
  );
  assert.equal(await pending, "server-p");
  assert.ok(
    sessionMetrics().find(
      (m) => m.name === "response_received" && m.requestId === "slow-body",
    )!.durationMs! >= 20,
  );
});
test("HTTP failures and invalid server ack never become saved", async () => {
  assert.throws(() =>
    parseSave({
      saved: false,
      artifactId: "a",
      version: "1",
      savedAt: "2026-10-15",
    }),
  );
  assert.throws(() =>
    parseSave({
      saved: true,
      artifactId: "a",
      version: "1",
      savedAt: "invalid",
    }),
  );
  const api = new ProjectApi(
    "https://test.invalid",
    true,
    async () => new Response("secret internals", { status: 403 }),
  );
  await assert.rejects(
    api.save("p", {}, "id", "local"),
    (e: ApiError) => e.code === "http_403" && !e.message.includes("secret"),
  );
});
test("question/result are server discriminated states", () => {
  assert.throws(() => parseRun({ id: "r", status: "clarification" }));
  assert.throws(() => parseRun({ id: "r", status: "completed" }));
  assert.equal(
    parseRun({
      id: "r",
      status: "running",
      question: { id: "q", text: "invented" },
    }).question,
    undefined,
  );
  assert.equal(
    parseRun({
      id: "r",
      status: "clarification",
      question: { id: "q", text: "Server asks" },
    }).question?.text,
    "Server asks",
  );
});
test("ZIP refuses JSON/error payloads and accepts ZIP signature", async () => {
  const invalid = new ProjectApi("https://test.invalid", true, async () =>
    response({ error: "not a zip" }),
  );
  await assert.rejects(
    invalid.zip("p", "a", "local"),
    (e: ApiError) => e.code === "invalid_response",
  );
  const valid = new ProjectApi(
    "https://test.invalid",
    true,
    async () =>
      new Response(new Uint8Array([80, 75, 5, 6, ...Array(18).fill(0)]), {
        headers: { "Content-Type": "application/zip" },
      }),
  );
  assert.equal((await valid.zip("p", "a", "local")).size, 22);
});
test("corrupt drafts stay untouched, valid drafts reload, metrics contain no content", () => {
  assert.throws(() => loadProjects({ getItem: () => "{corrupt" }));
  const draft = {
    id: "p",
    title: "Task",
    context: "context",
    updatedAt: "2026-10-15",
    pending: { action: "create", requestId: "stable", body: { title: "Task" } },
  };
  assert.equal(
    loadProjects({ getItem: () => JSON.stringify([draft]) })[0].pending
      ?.requestId,
    "stable",
  );
  assert.ok(
    sessionMetrics().every(
      (m) => !("content" in m) && !("title" in m) && !("answer" in m),
    ),
  );
});
