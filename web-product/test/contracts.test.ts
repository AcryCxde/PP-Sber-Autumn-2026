import test from "node:test";
import assert from "node:assert/strict";
import { CtfApi, ApiError } from "../src/api.ts";
import {
  parseRun, parseQuestion, buildAnswers, mergeRun, mergeRework, newProject,
  projectArtifacts, projectRuns, artifactRunId, parseApproval, approved, readProjects,
  canRework,
} from "../src/domain.ts";
const base = () => ({ id: "run-1", revision: 1, status: "running", stageId: "understand", messages: [], artifacts: [] });
const artifact = () => ({ id: "a1", documentId: "d1", stageId: "understand", version: 1,
  title: "Result", content: "Immutable", saved: true, savedAt: "2026-10-15T00:00:00Z" });
const group = () => ({ id: "q1", questions: [
  { text: "Exact question?", header: null, options: [{ label: "A", description: null }, { label: "B", description: "Detail" }], multi_select: true, allow_custom: true },
  { text: "Second question?", header: "Choose one", options: [{ label: "Yes", description: null }], multi_select: false, allow_custom: false },
] });
test("16/18: full structured questions, exact keys and runner comma-space multiselect", () => {
  const q = parseQuestion(group());
  assert.deepEqual(buildAnswers(q, {
    "Exact question?": { selected: ["A", "B"], custom: "other" },
    "Second question?": { selected: ["Yes"], custom: "" },
  }), { "Exact question?": "A, B, other", "Second question?": "Yes" });
  assert.equal(parseRun({ ...base(), status: "waiting_for_input", question: q }).question!.questions.length, 2);
});
test("18: incomplete, duplicate and forbidden custom answers fail closed", () => {
  assert.throws(() => buildAnswers(group(), {}));
  for (const value of [{ selected: ["Yes", "Yes"], custom: "" }, { selected: ["Unknown"], custom: "" }, { selected: [], custom: "not allowed" }])
    assert.throws(() => buildAnswers(group(), { "Exact question?": { selected: ["A"], custom: "" }, "Second question?": value }));
  assert.throws(() => parseQuestion({ ...group(), questions: [group().questions[0], group().questions[0]] }));
});
test("17: explicit unknown outcome and progress are not inferred from transport", () => {
  assert.equal(parseRun({ ...base(), status: "outcome_unknown" }).status, "outcome_unknown");
  assert.equal(parseRun({ ...base(), progress: { stage: "recovering", attempt: 2, context_restored: false } }).progress!.attempt, 2);
  assert.throws(() => parseRun({ ...base(), status: "completed", progress: { stage: "recovering" } }));
  assert.throws(() => parseRun({ ...base(), progress: { stage: "recovering", attempt: 0 } }));
  assert.equal(parseRun({ ...base(), status: "waiting_for_input", progress: { stage: "interrupted" } }).question, undefined);
});
test("17: no events, usage updates or reopening after terminal", () => {
  const p = { ...newProject(), run: parseRun({ ...base(), status: "completed", artifacts: [artifact()] }) };
  assert.equal(mergeRun(p, p.run).run!.status, "completed");
  for (const change of [{ revision: 2 }, { status: "running" }, { usage: { requests: 1 } }, { artifacts: [{ ...artifact(), approved: true }] },
    { messages: [{ id: "m1", role: "system", text: "Late event", at: "2026-10-15T00:00:00Z" }] }])
    assert.throws(() => mergeRun(p, parseRun({ ...p.run, ...change })));
});
test("20: rework is a linked NEW run in SAME project; old documents/versions survive", () => {
  const p = { ...newProject(), run: parseRun({ ...base(), status: "completed", artifacts: [artifact()] }) };
  const next = mergeRework(p, parseRun({ ...base(), id: "run-2", parentRunId: "run-1", sourceArtifactId: "a1" }), "run-1", "a1");
  assert.equal(next.id, p.id);
  assert.equal(next.context, p.context);
  assert.deepEqual(projectRuns(next).map(r => r.id), ["run-1", "run-2"]);
  assert.equal(artifactRunId(next, "a1"), "run-1");
  const withVersion = mergeRun(next, parseRun({ ...next.run, revision: 2, artifacts: [{ ...artifact(), id: "a2", version: 2 }] }));
  assert.deepEqual(projectArtifacts(withVersion).map(a => a.version), [1, 2]);
  assert.equal(p.run.artifacts[0].content, "Immutable");
  assert.throws(() => mergeRework(p, parseRun({ ...base(), id: "run-2" }), "run-1", "a1"));
  assert.throws(() => mergeRework(p, p.run, "run-1", "a1"));
});
test("20: polling new run cannot mutate or reuse historical versions", () => {
  const p = { ...newProject(), run: parseRun({ ...base(), status: "completed", artifacts: [artifact()] }) };
  const next = mergeRework(p, parseRun({ ...base(), id: "run-2", parentRunId: "run-1", sourceArtifactId: "a1" }), "run-1", "a1");
  assert.throws(() => mergeRun(next, parseRun({ ...next.run, revision: 2, artifacts: [{ ...artifact(), content: "Changed" }] })));
  assert.throws(() => mergeRun(next, parseRun({ ...next.run, revision: 2, artifacts: [{ ...artifact(), id: "a2" }] })));
});
test("21: saved and approval independent; legacy approved bool is NOT server receipt", () => {
  const a = { ...artifact(), approved: true }, p = newProject();
  assert.equal(approved(p, a), false);
  const receipt = parseApproval({ receiptId: "receipt1", artifactId: a.id, version: 1, approved: true, approvedAt: a.savedAt }, a);
  assert.equal(approved({ ...p, approvals: { [a.id]: receipt } }, a), true);
  assert.throws(() => parseApproval({ ...receipt, version: 2 }, a));
  assert.throws(() => parseApproval(receipt, { ...a, saved: false }));
  assert.throws(() => parseApproval({ ...receipt, receiptId: "" }, a));
});
test("v2 migration preserves old text, marks in-flight mutation legacy; v3 keeps typed draft/history", () => {
  const p = { ...newProject(), run: { ...base(), status: "clarification", question: { id: "q1", text: "Old?" } },
    answerDraft: { questionId: "q1", text: "Preserve" }, pending: { action: "answer", id: "op1", body: { answer: "Preserve" } } };
  const migrated = readProjects(JSON.stringify({ version: 2, projects: [p] }))[0];
  assert.equal(migrated.run!.question!.questions[0].text, "Old?");
  assert.equal(migrated.answerDraft!.text, "Preserve");
  assert.equal(migrated.pending!.legacy, true);
  assert.deepEqual(migrated.pending!.body, p.pending.body);
  assert.equal(readProjects(JSON.stringify({ version: 3, projects: [migrated] }))[0].pending!.id, "op1");
  assert.throws(() => readProjects(JSON.stringify({ version: "3", projects: [] })));
});
test("17: unknown usage remains unknown; actual zero stays zero", () => {
  assert.deepEqual(parseRun({ ...base(), usage: { inputTokens: null, outputTokens: 0, currency: null } }).usage, { outputTokens: 0 });
});
test("18: API recognizes only structured rejection; never echoes untrusted error text", async () => {
  const api = new CtfApi("http://fixture.invalid/api", true, async () => new Response(JSON.stringify({ code: "unknown_fork", message: "DO NOT DISPLAY" }), { status: 409 }));
  await assert.rejects(api.run("answers", {}, "op1"), e => e instanceof ApiError && e.code === "unknown_fork" && !e.message.includes("DO NOT DISPLAY"));
});
test("20: manual delivery retry reuses operation key and exact original bytes", async () => {
  const calls: RequestInit[] = [];
  const api = new CtfApi("http://fixture.invalid/api", true, async (_url, init) => {
    calls.push(init!);
    if (calls.length === 1) throw new TypeError("Connection interrupted");
    return Response.json(base());
  });
  const body = { parentRunId: "run-0", sourceArtifactId: "a0", text: "Change", stageId: "understand" };
  await assert.rejects(api.run("projects/p/runs", body, "stable-op"));
  assert.equal(calls.length, 1); // no automatic POST retry
  await api.run("projects/p/runs", body, "stable-op");
  assert.equal(calls[0].body, calls[1].body);
  assert.deepEqual(calls[0].headers, calls[1].headers);
});
test("21: approval endpoint parses receipt, not a mutated terminal Run", async () => {
  const api = new CtfApi("http://fixture.invalid/api", true, async () => Response.json({ ...base(), status: "completed" }));
  await assert.rejects(api.approve("approve", { artifactId: "a1", version: 1 }, "op", artifact()));
});
test("19: corrupted input bytes stop BEFORE upload/start", async () => {
  let called = false;
  const api = new CtfApi("http://fixture.invalid/api", true, async () => { called = true; return Response.json({}); });
  await assert.rejects(api.upload("p1", "f1", "input.txt", new Blob(["changed bytes"]), "0".repeat(64)), /SHA-256/);
  assert.equal(called, false);
});
test("19: role activity requires validated events, not a selected team or prompt", () => {
  assert.equal(parseRun(base()).roleEvents, undefined);
  const event = { id: "event1", roleId: "analyst", type: "role.started", at: "2026-10-16T00:00:00Z" };
  const p = { ...newProject(), run: parseRun({ ...base(), roleEvents: [event] }) };
  assert.throws(() => mergeRun(p, parseRun({ ...base(), revision: 2, roleEvents: [{ ...event, type: "role.completed" }] })));
  assert.equal(mergeRun(p, parseRun({ ...base(), revision: 2 })).run!.roleEvents!.length, 1);
  assert.throws(() => parseRun({ ...base(), roleEvents: [{ ...event, at: "not a date" }] }));
  assert.equal(canRework(parseRun({ ...base(), status: "review" })), false);
});
