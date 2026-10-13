"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

// Test pure scenario logic without a browser, timers, network or user storage.
function loadApp() {
  let source = fs.readFileSync(path.join(__dirname, "../public/v9/app.js"), "utf8");
  source = source.replace(/  readRoute\(\);\s+render\(false\);\s+startTimer\(\);\s+\}\)\(\);\s*$/, "globalThis.subject = { stagesFor, questionsFor, teamFor, buildArtifact, teamCards, answerText, decisionOptions, markdown }; })();");
  const node = { addEventListener() {} };
  const sandbox = {
    document: {querySelector: () => node, addEventListener() {}},
    window: {addEventListener() {}},
    localStorage: {getItem: () => null},
  };
  vm.runInNewContext(source, sandbox);
  return sandbox.subject;
}
const app = loadApp();
const project = (scenario) => ({scenario, name: "Проверка", problem: "Нужен понятный план", context: "Без найма команды", answers: [["Предложение команды"], [], []], custom: ["", "", ""], answer: ["Сначала сравнить варианты"], answerCustom: "", artifacts: [[], [], []], files: [], messages: [{role: "user", text: "Учесть сезонность"}], stage: 0});

test("old v9 projects retain the course scenario", () => {
  assert.equal(app.stagesFor(project(undefined))[1].title, "Обновлённая программа");
  assert.match(app.questionsFor(project(undefined))[1].title, /курсе/);
});
test("all scenarios have three stages, matching questions and nonempty role responsibilities", () => {
  for (const type of ["course", "book", "business", "other"]) {
    const p = project(type);
    assert.equal(app.stagesFor(p).length, 3);
    assert.equal(app.questionsFor(p).length, 3);
    for (const role of app.teamFor(p)) assert.ok(role.every(Boolean));
    assert.match(app.teamCards(p), /не подключённый агент/);
  }
});
test("marketing role follows a specific answer and is not duplicated", () => {
  const p = project("business");
  assert.equal(app.teamFor(p).length, 3);
  p.answers[0] = ["Проверить спрос", "Продумать продвижение"];
  assert.equal(app.teamFor(p).length, 4);
  assert.equal(app.teamFor(p).filter(r => r[0] === "Маркетолог").length, 1);
  p.answers[0] = ["Проверить спрос"];
  assert.equal(app.teamFor(p).length, 3);
});
test("non-course artifacts do not leak course content and preserve user decisions", () => {
  for (const type of ["book", "business", "other"]) {
    const p = project(type);
    for (let stage = 0; stage < 3; stage++) {
      p.stage = stage;
      const doc = app.buildArtifact(p);
      const text = JSON.stringify(doc);
      assert.doesNotMatch(text, /ученик|теори|модул|программ.*курс/);
      assert.match(text, /Без найма команды/);
      assert.match(text, /Сначала сравнить варианты/);
      assert.match(text, /Учесть сезонность/);
      assert.match(text, /шаблон/);
      assert.equal(doc.title, app.stagesFor(p)[stage].title);
      assert.match(app.markdown(p, [doc]), /Учесть сезонность/);
    }
  }
});
test("course artifact and theory decision remain available", () => {
  const p = project("course");
  p.answer = ["Нет, вся теория должна остаться"];
  assert.match(JSON.stringify(app.buildArtifact(p)), /Сохранить всю теорию/);
  assert.match(app.decisionOptions(p).join(" "), /теория/);
  assert.doesNotMatch(app.decisionOptions(project("book")).join(" "), /теория/);
});
test("revision creates a version without destroying earlier artifacts", () => {
  const p = project("business");
  p.artifacts[0].push(app.buildArtifact(p));
  p.revisionRequest = "Добавить риски";
  const second = app.buildArtifact(p);
  assert.equal(second.version, 2);
  assert.equal(p.artifacts[0].length, 1);
  assert.match(JSON.stringify(second), /Добавить риски/);
});
