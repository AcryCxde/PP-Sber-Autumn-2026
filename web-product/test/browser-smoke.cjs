const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const DEFAULT_URL = process.env.DEFAULT_URL || "http://127.0.0.1:4192";
const FIXTURE_URL = process.env.FIXTURE_URL || "http://127.0.0.1:4191";
const ROOT = path.resolve(__dirname, "..");
const OUT = path.join(ROOT, "test-results");
fs.mkdirSync(OUT, { recursive: true });
const TEXT =
  "Non-secret baseline file contents: compare options before committing.";
const QUESTIONS = [
  "Какое решение вы хотите принять или что получить?",
  "Для кого нужен результат и что уже известно?",
  "Какие ограничения и критерии успеха важны?",
];
async function prepare(page, title, withGuide = true) {
  await page.goto(withGuide ? DEFAULT_URL : FIXTURE_URL);
  await page
    .getByRole("button", { name: "Начать свою задачу", exact: true })
    .click();
  if (withGuide) {
    await page
      .getByRole("heading", { name: "Как работает CoreTeams", exact: true })
      .waitFor();
    for (let i = 0; i < 4; i++)
      await page.getByRole("button", { name: "Дальше", exact: true }).click();
    await page
      .getByRole("button", { name: "Открыть рабочее пространство примера" })
      .click();
    await page
      .getByRole("heading", { name: "Диалог с фасилитатором" })
      .waitFor();
    await page.getByLabel("Версия артефакта").selectOption("e-a2");
    await page
      .getByRole("heading", { name: "Первый вариант", exact: true })
      .waitFor();
    await page.getByLabel("Версия артефакта").selectOption("e-a3");
    await page.getByRole("heading", { name: "Проверка до аренды" }).waitFor();
    await page
      .getByLabel("Артефакт этапа", { exact: true })
      .selectOption("interview");
    await page
      .getByRole("heading", { name: "Пример вопросов", exact: true })
      .waitFor();
    await page
      .getByLabel("Артефакт этапа", { exact: true })
      .selectOption("plan");
    await page.getByRole("button", { name: "Замеры", exact: true }).click();
    assert.equal(
      await page.getByText("Не измерено", { exact: true }).count(),
      5,
    );
    await page.getByRole("button", { name: "Артефакты", exact: true }).click();
    await page.screenshot({
      path: path.join(
        OUT,
        `example-${title.includes("Mobile") ? "mobile" : "desktop"}.png`,
      ),
      fullPage: true,
    });
    await page
      .getByRole("button", { name: "Начать свой проект", exact: true })
      .click();
  }
  await page.getByLabel("Название проекта", { exact: true }).fill(title);
  await page
    .getByLabel("Какую задачу нужно решить?")
    .fill("Compare alternatives using safe external sources");
  await page
    .getByLabel("Контекст, факты и ссылки")
    .fill("Safe context. Do not invent a result.");
  await page.getByLabel("Добавить вводные файлы").setInputFiles({
    name: "baseline.txt",
    mimeType: "text/plain",
    buffer: Buffer.from(TEXT),
  });
  await page.getByRole("button").filter({ hasText: "baseline.txt" }).waitFor();
  await page.reload();
  await page.getByRole("button").filter({ hasText: title }).click();
  await page.getByRole("button").filter({ hasText: "baseline.txt" }).click();
  await page.getByRole("dialog").waitFor();
  assert((await page.getByRole("dialog").innerText()).includes(TEXT));
  await page.keyboard.press("Escape");
  await page.getByRole("dialog").waitFor({ state: "hidden" });
  await page
    .getByRole("button", { name: "К вопросам фасилитатора", exact: true })
    .click();
  for (let i = 0; i < 3; i++)
    await page
      .getByLabel(QUESTIONS[i], { exact: true })
      .fill("Confirmed preparation answer " + i);
  await page
    .getByRole("button", { name: "Посмотреть и выбрать команду" })
    .click();
  await page.getByRole("checkbox", { name: /Предметный специалист/ }).check();
  await page.getByRole("checkbox", { name: /Редактор/ }).uncheck();
  await page
    .getByLabel("Что изменить в составе или какую экспертизу добавить?")
    .fill("Check procurement assumptions.");
  await page
    .getByRole("button", { name: "Команда подходит — к плану" })
    .click();
  await page
    .getByLabel("Название этапа 1", { exact: true })
    .fill("Исходные данные");
  await page
    .getByRole("button", { name: "План подходит — в рабочее пространство" })
    .click();
  await page.getByRole("heading", { name: "Диалог с фасилитатором" }).waitFor();
  await page.getByRole("button", { name: "Команда", exact: true }).click();
  await page.getByText("Предметный специалист", { exact: true }).waitFor();
  assert.equal(await page.getByText("Редактор", { exact: true }).count(), 0);
  await page.getByRole("button", { name: "Артефакты", exact: true }).click();
  if (
    !(await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ))
  ) {
    await page.screenshot({
      path: path.join(OUT, "overflow-diagnostic.png"),
      fullPage: true,
    });
    console.log(
      "Overflow:",
      await page.evaluate(() =>
        Array.from(document.querySelectorAll("body *"))
          .filter((el) => el.getBoundingClientRect().right > innerWidth + 1)
          .slice(0, 10)
          .map((el) => ({
            tag: el.tagName,
            class: el.className,
            width: el.getBoundingClientRect().width,
            right: el.getBoundingClientRect().right,
          })),
      ),
    );
  }
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
    true,
  );
}
(async () => {
  const browser = await chromium.launch({
    channel: "msedge",
    headless: process.env.PW_HEADLESS === "true",
  });
  try {
    for (const [name, width, height] of [
      ["Desktop", 1440, 1000],
      ["Mobile", 390, 844],
    ]) {
      const context = await browser.newContext({ viewport: { width, height } }),
        page = await context.newPage(),
        errors = [];
      let apiCount = 0;
      page.on("pageerror", (e) => errors.push(e.message));
      page.on("request", (r) => {
        if (r.url().includes("/api/")) apiCount++;
      });
      await prepare(page, name + " safe task");
      assert.equal(
        await page.evaluate(() =>
          localStorage.getItem("coreteams-journey-study:v1"),
        ),
        null,
      );
      await page
        .getByRole("button", { name: "Замер пути", exact: true })
        .click();
      await page.getByLabel("Код участника", { exact: true }).fill("U001");
      await page.getByLabel("Код задачи", { exact: true }).fill("T001");
      assert(
        await page
          .getByRole("button", { name: "Начать замер", exact: true })
          .isDisabled(),
      );
      await page.getByLabel("Участник согласился", { exact: false }).check();
      await page
        .getByRole("button", { name: "Начать замер", exact: true })
        .click();
      await page.getByRole("button", { name: "Жду", exact: true }).click();
      await page.getByRole("button", { name: "Пауза", exact: true }).click();
      await page.getByRole("button", { name: "Работаю", exact: true }).click();
      await page
        .getByRole("button", { name: "Отметить помощь", exact: true })
        .click();
      await page
        .getByLabel("Действие привычного пути", { exact: true })
        .selectOption("find");
      await page
        .getByLabel("Инструмент привычного пути", { exact: true })
        .selectOption("search");
      await page
        .getByRole("button", {
          name: "Начать шаг привычного пути",
          exact: true,
        })
        .click();
      assert(
        await page
          .getByRole("button", {
            name: "Результат baseline сохранён",
            exact: true,
          })
          .isDisabled(),
      );
      await page
        .getByLabel("Затруднение на шаге", { exact: true })
        .selectOption("sources");
      await page
        .getByLabel("Итог шага", { exact: true })
        .selectOption("sources");
      await page
        .getByRole("button", {
          name: "Завершить шаг привычного пути",
          exact: true,
        })
        .click();
      await page
        .getByLabel("Формат результата привычного способа", { exact: true })
        .selectOption("document");
      await page
        .getByRole("button", {
          name: "Результат baseline сохранён",
          exact: true,
        })
        .click();
      await page.getByLabel("Полезность", { exact: true }).selectOption("3");
      await page
        .getByLabel("Итог задачи", { exact: true })
        .selectOption("completed");
      await page
        .getByRole("button", { name: "Завершить замер", exact: true })
        .click();
      const baseline = await page.evaluate(
        () =>
          JSON.parse(localStorage.getItem("coreteams-journey-study:v1"))
            .sessions[0],
      );
      assert.equal(baseline.outcome, "completed");
      assert.equal(baseline.ratings.usefulness, 3);
      assert.equal(baseline.baselineSteps.length, 1);
      assert.equal(baseline.baselineSteps[0].barrier, "sources");
      assert.equal(baseline.resultKind, "document");
      await page.getByText(/^Сохранённые сессии и сравнение/).click();
      const stepsDownload = page.waitForEvent("download");
      await page
        .getByRole("button", {
          name: "Выгрузить шаги привычного пути (CSV)",
          exact: true,
        })
        .click();
      const stepsFile = await stepsDownload;
      await stepsFile.saveAs(path.join(OUT, `baseline-${name}-steps.csv`));
      const stepsText = fs.readFileSync(
        path.join(OUT, `baseline-${name}-steps.csv`),
        "utf8",
      );
      assert(stepsText.includes('"action"'));
      assert(stepsText.includes('"find"'));
      assert(stepsText.includes('"sources"'));
      await page.screenshot({
        path: path.join(OUT, `baseline-${name}-journal.png`),
        fullPage: true,
      });
      assert.equal(
        baseline.events.filter((e) => e.name === "help_observed").length,
        1,
      );
      assert(!JSON.stringify(baseline).includes(TEXT));
      assert(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      );
      await page
        .getByRole("button", { name: "Замер пути", exact: true })
        .click();
      await page
        .getByRole("button", { name: "Запустить работу команды", exact: true })
        .click();
      await page
        .getByRole("alert")
        .filter({ hasText: "CTF пока не подключён" })
        .waitFor();
      assert.equal(apiCount, 0);
      await page.screenshot({
        path: path.join(OUT, name.toLowerCase() + "-workspace.png"),
        fullPage: true,
      });
      assert.deepEqual(errors, []);
      await context.close();
      console.log(
        "PASS " +
          name +
          ": guide/example, real file bytes restored after reload, question/team/plan/workspace, no API while unavailable, no horizontal overflow",
      );
    }
    const context = await browser.newContext({
      viewport: { width: 1440, height: 1000 },
    });
    await context.addInitScript(() =>
      localStorage.setItem("coreteams-product-workspace:v2:learned", "true"),
    );
    const page = await context.newPage();
    let uploaded = false;
    let createBody;
    let revision = 0;
    const runKeys = [];
    let runCount = 0,
      answered = false;
    const message = (id, role, text) => ({
      id,
      role,
      text,
      at: "2026-10-15T00:00:00Z",
    });
    const artifacts = [
      {
        id: "a-v1",
        documentId: "d1",
        stageId: "understand",
        version: 1,
        title: "Fixture artifact",
        content: "# Fixture version 1\nSynthetic test, not a CTF result.",
        saved: true,
        savedAt: "2026-10-15T00:00:00Z",
      },
      {
        id: "a-v2",
        documentId: "d1",
        stageId: "understand",
        version: 2,
        title: "Fixture artifact",
        content:
          "# Fixture version 2\nChanged synthetic test, not a CTF result.",
        saved: true,
        savedAt: "2026-10-15T00:01:00Z",
      },
    ];
    const response = (status, extra = {}) => ({
      id: "run-v2",
      revision: ++revision,
      status,
      stageId: "understand",
      messages: [
        message(
          "m1",
          "facilitator",
          "Synthetic fixture facilitator, not real CTF.",
        ),
      ],
      artifacts: status === "clarification" ? [] : artifacts,
      usage:
        status === "clarification"
          ? undefined
          : {
              inputTokens: 120,
              outputTokens: 45,
              requests: 2,
              computeSeconds: 4,
              cost: 0.001,
              currency: "USD",
            },
      ...extra,
    });
    await page.route("**/api/**", async (route) => {
      const req = route.request(),
        suffix = new URL(req.url()).pathname.slice(4);
      const json = (x) =>
        route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(x),
        });
      if (suffix === "/projects") {
        createBody = req.postDataJSON();
        assert(createBody.team.includes("specialist"));
        assert(!createBody.team.includes("editor"));
        return json({ id: "project-v2" });
      }
      if (suffix === "/projects/project-v2/files") {
        const raw = req.postDataBuffer().toString("utf8");
        assert(raw.includes(TEXT));
        const sha = raw.match(/name="sha256"\r\n\r\n([a-f0-9]{64})/)[1];
        uploaded = true;
        return json({ id: "uploaded-v2", sha256: sha });
      }
      if (suffix === "/projects/project-v2/runs") {
        assert(uploaded);
        runKeys.push(req.headers()["idempotency-key"]);
        assert.deepEqual(req.postDataJSON().fileIds, ["uploaded-v2"]);
        assert.equal(req.postDataJSON().stages[0].title, "Исходные данные");
        if (++runCount === 1) return route.abort("failed");
        return json(response("running", { artifacts: [] }));
      }
      if (suffix.endsWith("/answers")) {
        answered = true;
        assert.equal(req.postDataJSON().questionId, "q-real");
        return json(response("review"));
      }
      if (suffix.endsWith("/messages")) {
        assert.equal(req.postDataJSON().text, "Fixture revision request");
        return json(
          response("review", {
            messages: [
              message(
                "m1",
                "facilitator",
                "Synthetic fixture facilitator, not real CTF.",
              ),
              message("m2", "user", "Fixture revision request"),
            ],
          }),
        );
      }
      if (suffix.endsWith("/approve")) {
        assert.equal(req.postDataJSON().version, 2);
        return json(
          response("completed", {
            artifacts: artifacts.map((a) => ({
              ...a,
              approved: a.id === "a-v2",
            })),
          }),
        );
      }
      if (suffix === "/projects/project-v2/runs/run-v2")
        return json(
          answered
            ? response("review")
            : response("clarification", {
                question: {
                  id: "q-real",
                  text: "Fixture question: which assumption matters?",
                },
              }),
        );
      if (suffix.endsWith("/zip"))
        return route.fulfill({
          status: 200,
          contentType: "application/zip",
          body: Buffer.from([80, 75, 5, 6, ...Array(18).fill(0)]),
        });
      throw Error("Unexpected request " + suffix);
    });
    await page.goto(FIXTURE_URL);
    await page.getByRole("button", { name: "Замер пути", exact: true }).click();
    await page.getByLabel("Код участника", { exact: true }).fill("U002");
    await page.getByLabel("Код задачи", { exact: true }).fill("T002");
    await page
      .getByLabel("Способ решения", { exact: true })
      .selectOption("prototype");
    await page.getByLabel("Участник согласился", { exact: false }).check();
    await page
      .getByRole("button", { name: "Начать замер", exact: true })
      .click();
    await prepare(page, "Fixture product workspace", false);
    await page
      .getByRole("button", { name: "Запустить работу команды", exact: true })
      .click();
    await page.getByRole("alert").waitFor();
    await page.reload();
    await page
      .getByRole("button")
      .filter({ hasText: "Fixture product workspace" })
      .click();
    await page
      .getByRole("button", { name: "Повторить запрос", exact: true })
      .click();
    await page
      .getByText("Fixture question: which assumption matters?", { exact: true })
      .waitFor();
    assert.equal(new Set(runKeys).size, 1);
    assert.equal(runCount, 2);
    await page
      .getByLabel("Ответ фасилитатору", { exact: true })
      .fill("Fixture answer");
    await page.reload();
    await page
      .getByRole("button")
      .filter({ hasText: "Fixture product workspace" })
      .click();
    assert.equal(
      await page.getByLabel("Ответ фасилитатору", { exact: true }).inputValue(),
      "Fixture answer",
    );
    await page.getByRole("button", { name: "Ответить и продолжить" }).click();
    await page
      .getByRole("heading", { name: "Fixture version 2", exact: true })
      .waitFor();
    await page.getByLabel("Версия артефакта").selectOption("a-v1");
    await page
      .getByRole("heading", { name: "Fixture version 1", exact: true })
      .waitFor();
    assert(
      await page
        .getByRole("button", { name: "Согласовать результат" })
        .isDisabled(),
    );
    await page.getByLabel("Версия артефакта").selectOption("a-v2");
    await page
      .getByLabel("Вопрос или замечание к результату")
      .fill("Fixture revision request");
    await page.getByRole("button", { name: "Передать фасилитатору" }).click();
    await page.getByText("Fixture revision request", { exact: true }).waitFor();
    await page.getByRole("button", { name: "Замеры", exact: true }).click();
    await page.getByText("120", { exact: true }).waitFor();
    await page.getByRole("button", { name: "Артефакты", exact: true }).click();
    await page
      .getByRole("button", { name: "Согласовать результат", exact: true })
      .click();
    await page
      .getByRole("button", { name: "Версия согласована", exact: true })
      .waitFor();
    const download = page.waitForEvent("download");
    await page.getByRole("button", { name: "Скачать текст версии" }).click();
    assert((await download).suggestedFilename().includes("v2.md"));
    const zipDownload = page.waitForEvent("download");
    await page
      .getByRole("button", { name: "Скачать ZIP версии", exact: true })
      .click();
    assert((await zipDownload).suggestedFilename().endsWith("v2.zip"));
    await page.getByRole("button", { name: /^Замер пути/ }).click();
    await page
      .getByRole("button", {
        name: "Этот артефакт — результат задачи",
        exact: true,
      })
      .click();
    await page
      .getByLabel("Итог задачи", { exact: true })
      .selectOption("completed");
    await page.getByLabel("Полезность", { exact: true }).selectOption("4");
    await page
      .getByRole("button", { name: "Завершить замер", exact: true })
      .click();
    await page.getByText(/^Сохранённые сессии и сравнение/).click();
    const jsonDownload = page.waitForEvent("download");
    await page
      .getByRole("button", { name: "Выгрузить JSON замеров", exact: true })
      .click();
    const exported = await jsonDownload;
    await exported.saveAs(path.join(OUT, "study-fixture.json"));
    const study = JSON.parse(
      fs.readFileSync(path.join(OUT, "study-fixture.json"), "utf8"),
    );
    const session = study.sessions[0],
      names = session.events.map((e) => e.name);
    for (const event of [
      "project_created",
      "file_added",
      "team_changed",
      "preparation_step",
      "workspace_entered",
      "request_started",
      "request_failed",
      "run_received",
      "clarification_shown",
      "answer_acknowledged",
      "result_shown",
      "artifact_saved",
      "artifact_accepted",
      "message_acknowledged",
      "text_download_handed",
      "zip_download_handed",
      "session_recovered",
      "task_result_selected",
    ])
      assert(names.includes(event), "missing event " + event);
    assert.equal(session.environment, "fixture");
    assert.equal(
      session.events.filter(
        (e) => e.name === "artifact_saved" && e.artifactId === "a-v2",
      ).length,
      1,
    );
    assert.equal(study.summaries[0].selected_result_artifact_id, "a-v2");
    assert.equal(study.summaries[0].input_tokens, 120);
    assert.equal(study.summaries[0].output_tokens, 45);
    assert.equal(study.summaries[0].failed_operations_n, 1);
    assert(!JSON.stringify(createBody).includes("U002"));
    assert(study.summaries[0].elapsed_to_saved_s !== null);
    assert(study.summaries[0].unobserved_s > 0);
    for (const secret of [
      TEXT,
      "Fixture answer",
      "Fixture revision request",
      "Synthetic test",
    ])
      assert(!JSON.stringify(study).includes(secret), "private content leaked");
    const csvDownload = page.waitForEvent("download");
    await page
      .getByRole("button", { name: "Выгрузить CSV замеров", exact: true })
      .click();
    assert((await csvDownload).suggestedFilename().endsWith(".csv"));
    await page.screenshot({
      path: path.join(OUT, "fixture-versions.png"),
      fullPage: true,
    });
    await context.close();
    console.log(
      "PASS fixture: actual multipart file bytes/hash, selected team/plan, stable retry across reload, same-run question/answer, immutable versions, message, approval, consumption and download. NOT live CTF.",
    );
    const corrupt = await browser.newContext();
    await corrupt.addInitScript(() =>
      localStorage.setItem("coreteams-product-workspace:v2", "broken"),
    );
    const cp = await corrupt.newPage();
    await cp.goto(DEFAULT_URL);
    await cp.getByRole("alert").waitFor();
    assert.equal(
      await cp.evaluate(() =>
        localStorage.getItem("coreteams-product-workspace:v2"),
      ),
      "broken",
    );
    await corrupt.close();
    console.log("PASS corrupt drafts preserved");
    const damagedStudy = await browser.newContext();
    await damagedStudy.addInitScript(() =>
      localStorage.setItem("coreteams-journey-study:v1", "invalid-study"),
    );
    const dp = await damagedStudy.newPage();
    await dp.goto(DEFAULT_URL);
    await dp.getByRole("button", { name: /^Замер пути/ }).click();
    await dp
      .getByRole("alert")
      .filter({ hasText: "Исходные данные не перезаписаны" })
      .waitFor();
    assert.equal(
      await dp.evaluate(() =>
        localStorage.getItem("coreteams-journey-study:v1"),
      ),
      "invalid-study",
    );
    await dp.screenshot({
      path: path.join(OUT, "study-storage-warning.png"),
      fullPage: true,
    });
    await damagedStudy.close();
    console.log(
      "PASS opt-in study: desktop/mobile baseline, recovered fixture lifecycle, privacy-safe JSON/CSV, usage and corrupt journal preservation",
    );
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
