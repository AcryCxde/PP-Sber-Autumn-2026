import test from "node:test";
import assert from "node:assert/strict";
import {
  parseRun,
  mergeRun,
  newProject,
  readProjects,
  exampleProject,
  latest,
  projectPayload,
} from "../src/domain.ts";
const base = () => ({
  id: "r1",
  revision: 1,
  status: "running",
  stageId: "understand",
  messages: [],
  artifacts: [],
});
test("run requires real question and validates numeric usage", () => {
  assert.throws(() => parseRun({ ...base(), status: "clarification" }));
  assert.throws(() => parseRun({ ...base(), usage: { inputTokens: -1 } }));
  assert.throws(() => parseRun({ ...base(), usage: { cost: 3 } }));
  assert.deepEqual(
    parseRun({
      ...base(),
      usage: { inputTokens: 0, outputTokens: 1, cost: 0, currency: "USD" },
    }).usage,
    { inputTokens: 0, outputTokens: 1, cost: 0, currency: "USD" },
  );
  assert.equal(parseRun(base()).usage, undefined);
});
test("saved means explicit timestamp, duplicate artifact IDs are rejected", () => {
  const a = {
    id: "a1",
    documentId: "d1",
    stageId: "understand",
    version: 1,
    title: "Result",
    content: "Plain text",
    saved: true,
  };
  assert.throws(() => parseRun({ ...base(), artifacts: [a] }));
  assert.throws(() =>
    parseRun({
      ...base(),
      artifacts: [
        { ...a, saved: false },
        { ...a, saved: false },
      ],
    }),
  );
});
test("schema preserves every version and rejects stale revisions", () => {
  const p = newProject();
  p.run = parseRun({ ...base(), revision: 3 });
  assert.equal(mergeRun(p, parseRun(base())), p);
  const ex = exampleProject();
  assert.equal(ex.run!.artifacts.length, 4);
  assert.equal(latest(ex.run!.artifacts, "options")?.version, 2);
});
test("unknown stages rejected; preparation includes team and context", () => {
  const p = newProject();
  assert.throws(() => mergeRun(p, parseRun({ ...base(), stageId: "unknown" })));
  assert.deepEqual(projectPayload(p).team, p.team);
  assert.equal(projectPayload(p).context, p.context);
});
test("draft round trip and no destructive fallback on corruption", () => {
  const p = newProject();
  p.title = "Task";
  const raw = JSON.stringify({ version: 2, projects: [p] });
  assert.equal(readProjects(raw)[0].id, p.id);
  assert.throws(() => readProjects("broken"));
  assert.throws(() =>
    readProjects(JSON.stringify({ version: 1, projects: [p] })),
  );
  assert.throws(() =>
    readProjects(
      JSON.stringify({ version: 2, projects: [{ ...p, stages: [] }] }),
    ),
  );
});
test("no artificial project quota; sample reports no CTF usage", () => {
  const projects = Array.from({ length: 5 }, () => newProject());
  assert.equal(
    readProjects(JSON.stringify({ version: 2, projects })).length,
    5,
  );
  assert.equal(exampleProject().run?.usage, undefined);
});
test("historical versions survive partial snapshots; changing an existing version fails", () => {
  const p = exampleProject(),
    old = p.run!;
  const partial = { ...old, revision: 3, artifacts: [old.artifacts[3]] };
  assert.equal(mergeRun(p, partial).run!.artifacts.length, 4);
  assert.throws(() =>
    mergeRun(p, {
      ...old,
      revision: 3,
      artifacts: [
        { ...old.artifacts[0], content: "Changed immutable content" },
      ],
    }),
  );
  assert.throws(() => parseRun({ ...base(), usage: { requests: 1.5 } }));
});
test("facilitator answer draft survives reload", () => {
  const p = newProject();
  p.answerDraft = { questionId: "q1", text: "Safe draft" };
  assert.deepEqual(
    readProjects(JSON.stringify({ version: 2, projects: [p] }))[0].answerDraft,
    p.answerDraft,
  );
});
