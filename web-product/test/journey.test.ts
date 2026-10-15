import test from "node:test";
import assert from "node:assert/strict";
import {
  JourneyRecorder,
  JOURNEY_KEY,
  compare,
  csv,
  exportStudy,
  summary,
} from "../src/journey.ts";
function fixture() {
  const values = new Map<string, string>();
  let at = Date.parse("2026-10-15T08:00:00+03:00"),
    serial = 0;
  const storage = {
    getItem: (key: string) => values.get(key) || null,
    setItem: (key: string, value: string) => {
      values.set(key, value);
    },
  };
  const create = () =>
    new JourneyRecorder(
      storage,
      () => at,
      () => `id-${++serial}`,
    );
  const advance = (seconds: number) => (at += seconds * 1000);
  return { values, storage, create, advance };
}
const input = (phase: "baseline" | "prototype" | "interface" = "baseline") => ({
  participantCode: "U001",
  taskCode: "T001",
  phase,
  consent: true,
  buildVersion: "test-1",
  environment: "local" as const,
});
test("opt-in, fixed pseudonyms; baseline excludes product events and arbitrary text", () => {
  const f = fixture(),
    r = f.create();
  assert.equal(r.record("project_created"), false);
  assert.equal(f.storage.getItem(JOURNEY_KEY), null);
  assert.equal(r.start({ ...input(), consent: false }), false);
  assert.equal(
    r.start({ ...input(), participantCode: "email@example.com" }),
    false,
  );
  assert(r.start(input()));
  assert.equal(r.record("request_started", { projectId: "p1" }), false);
  r.record("help_observed", { text: "SECRET CONTENT" } as any, undefined, true);
  assert(
    !JSON.stringify(exportStudy(r.getSnapshot().sessions)).includes("SECRET"),
  );
  assert.equal(summary(r.active()!).help_requests_n, 1);
});
test("separate active/wait/pause and first saved boundaries; missing is null not zero", () => {
  const f = fixture(),
    r = f.create();
  r.start(input());
  assert.equal(summary(r.active()!).elapsed_to_saved_s, null);
  f.advance(10);
  r.mode("wait");
  f.advance(4);
  r.mode("pause");
  f.advance(3);
  r.mode("active");
  f.advance(2);
  r.record("baseline_result_saved", {}, "saved", true);
  r.record("baseline_result_saved", {}, "saved", true);
  r.finish("completed", { usefulness: 5, clarity: 0 });
  const s = summary(r.getSnapshot().sessions[0]);
  assert.equal(s.active_s, 12);
  assert.equal(s.wait_s, 4);
  assert.equal(s.pause_s, 3);
  assert.equal(s.elapsed_to_saved_s, 19);
  assert.equal(s.clarity, undefined);
  assert.equal(
    r
      .getSnapshot()
      .sessions[0].events.filter((e) => e.name === "baseline_result_saved")
      .length,
    1,
  );
});
test("reload gap is unknown; resumes paused; duplicates stay deduplicated", () => {
  const f = fixture(),
    r = f.create();
  r.start(input("prototype"));
  r.record("artifact_saved", { projectId: "p1", artifactId: "a1" }, "a1");
  f.advance(2);
  r.checkpoint();
  f.advance(7);
  const recovered = f.create();
  assert.equal(recovered.active()!.mode, "pause");
  assert.equal(summary(recovered.active()!).active_s, 2);
  assert.equal(summary(recovered.active()!).unobserved_s, 7);
  assert.equal(
    recovered.record(
      "artifact_saved",
      { projectId: "p1", artifactId: "a1" },
      "a1",
    ),
    false,
  );
});
test("study cannot silently mix projects; corrupt and concurrent data are not overwritten", () => {
  const f = fixture(),
    r = f.create();
  r.start(input("prototype"));
  r.record("project_created", { projectId: "p1" });
  assert.equal(r.record("artifact_saved", { projectId: "p2" }), false);
  f.values.set(JOURNEY_KEY, "broken");
  assert.equal(r.checkpoint(), false);
  assert(r.getSnapshot().issue);
  const next = f.create();
  assert(next.getSnapshot().issue);
  assert.equal(f.storage.getItem(JOURNEY_KEY), "broken");
});
test("explicit same-person/task comparison; no interface/fixture/missing-result wins", () => {
  const f = fixture(),
    r = f.create();
  r.start(input());
  f.advance(20);
  r.record("baseline_result_saved", {}, undefined, true);
  r.finish("completed", { usefulness: 3 });
  r.start(input("prototype"));
  f.advance(10);
  r.record("run_received", { projectId: "p1", runId: "run1" }, "run1");
  r.record("artifact_saved", { projectId: "p1", artifactId: "a1" }, "a1");
  r.record(
    "task_result_selected",
    { projectId: "p1", artifactId: "a1" },
    "task-result",
  );
  r.finish("completed", { usefulness: 4 });
  const [a, b] = r.getSnapshot().sessions;
  assert.equal(compare(a, b, false).delta, null);
  assert.equal(compare(a, b, true).delta!.elapsed_to_saved_s, -10);
  assert.equal(compare(a, b, true).delta!.usefulness, 1);
  assert.equal(compare(a, { ...b, taskCode: "T002" }, true).delta, null);
  assert.equal(compare(a, { ...b, phase: "interface" }, true).delta, null);
  assert.equal(compare(a, { ...b, environment: "fixture" }, true).delta, null);
  assert.equal(compare(a, { ...b, events: [] }, true).delta, null);
});
test("no invented time on clock rollback or unknown gaps; withdrawal deletes only study", () => {
  const f = fixture(),
    r = f.create();
  r.start(input());
  f.advance(-1);
  assert.equal(r.checkpoint(), false);
  assert(r.getSnapshot().issue);
  const g = fixture(),
    study = g.create();
  study.start(input());
  const id = study.active()!.id;
  assert(study.remove(id));
  assert.equal(study.getSnapshot().sessions.length, 0);
});
test("export strips unknown fields; CSV has blank unavailable values and observed counters", () => {
  const f = fixture(),
    r = f.create();
  r.start(input("interface"));
  r.record("request_started", {
    requestId: "req1",
    durationMs: -1,
    prompt: "SECRET PROMPT",
  } as any);
  const sessions = r
    .getSnapshot()
    .sessions.map((s) => ({ ...s, task_text: "SECRET TASK" }));
  const data = JSON.stringify(exportStudy(sessions));
  assert(!data.includes("SECRET"));
  assert(!data.includes("durationMs"));
  assert(csv(sessions).includes('"elapsed_to_saved_s"'));
  assert.equal(summary(sessions[0]).elapsed_to_saved_s, null);
});

test("actual usage preserves unknown versus zero and partial snapshot fields", () => {
  const f = fixture(),
    r = f.create();
  r.start(input("prototype"));
  assert.equal(summary(r.active()!).input_tokens, null);
  r.record("usage_received", {
    inputTokens: 0,
    outputTokens: 4,
    cost: 0,
    currency: "USD",
  });
  r.record("usage_received", { requests: 2 });
  const values = summary(r.active()!);
  assert.equal(values.input_tokens, 0);
  assert.equal(values.output_tokens, 4);
  assert.equal(values.cost, 0);
  assert.equal(values.compute_seconds, null);
});
test("storage exhaustion stops recording without changing the product or last saved data", () => {
  const f = fixture(),
    r = f.create();
  r.start(input());
  const raw = f.storage.getItem(JOURNEY_KEY);
  f.storage.setItem = () => {
    throw Error("quota");
  };
  assert.equal(r.record("help_observed", {}, undefined, true), false);
  assert(r.getSnapshot().issue);
  assert.equal(f.storage.getItem(JOURNEY_KEY), raw);
});
