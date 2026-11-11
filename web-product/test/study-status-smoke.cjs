const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const URL = process.env.DEFAULT_URL || "http://127.0.0.1:4192";
(async () => {
  const { JourneyRecorder, JOURNEY_KEY } = await import("../src/journey.ts");
  const values = new Map();
  let now = Date.parse("2026-10-15T08:00:00+03:00"),
    sequence = 0;
  const storage = {
    getItem: (key) => values.get(key) || null,
    setItem: (key, value) => values.set(key, value),
  };
  const recorder = new JourneyRecorder(
    storage,
    () => now,
    () => `synthetic-${++sequence}`,
  );
  const start = (phase) =>
    recorder.start({
      participantCode: "U900",
      taskCode: "T900",
      phase,
      consent: true,
      buildVersion: "status-test",
      environment: "fixture",
    });
  start("baseline");
  recorder.beginBaselineStep("frame", "ai");
  now += 3000;
  recorder.endBaselineStep("unknown", "unfinished");
  recorder.finish("abandoned", {});
  start("baseline");
  recorder.beginBaselineStep("assemble", "documents");
  now += 5000;
  recorder.endBaselineStep("none", "result");
  recorder.saveBaselineResult("document");
  recorder.finish("completed", {});
  start("interface");
  recorder.finish("incomplete", {});
  const raw = storage.getItem(JOURNEY_KEY),
    out = path.resolve(__dirname, "../test-results");
  fs.mkdirSync(out, { recursive: true });
  const browser = await chromium.launch({
    channel: "msedge",
    headless: process.env.PW_HEADLESS === "true",
  });
  try {
    for (const width of [1440, 390]) {
      const context = await browser.newContext({
        viewport: { width, height: 1000 },
      });
      await context.addInitScript(
        ({ key, raw }) => localStorage.setItem(key, raw),
        { key: JOURNEY_KEY, raw },
      );
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (e) => errors.push(e.message));
      await page.goto(URL);
      await page
        .getByRole("button", { name: "Замер пути", exact: true })
        .click();
      await page.getByText(/^Сохранённые сессии и сравнение/).click();
      const table = page.getByRole("table"),
        row = table.getByRole("row").filter({ hasText: "Попытка прекращена" });
      assert((await row.innerText()).includes("Сохранение не отмечено"));
      assert((await row.innerText()).includes("3,0 с"));
      assert((await row.innerText()).includes("Пропусков не отмечено"));
      assert.equal((await table.innerText()).includes("abandoned"), false);
      assert((await table.innerText()).includes("Завершено с результатом"));
      assert((await table.innerText()).includes("Задача не завершена"));
      assert((await table.innerText()).includes("Не применяется"));
      await row.getByText("Почему такой итог?", { exact: true }).click();
      assert((await row.innerText()).includes("Отметка сохранения: нет"));
      assert.equal(
        await page.evaluate((key) => localStorage.getItem(key), JOURNEY_KEY),
        raw,
      );
      if (width === 1440)
        await table.screenshot({
          path: path.join(out, "study-statuses-synthetic.png"),
        });
      assert(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      );
      await page.getByLabel("Код участника", { exact: true }).fill("U999");
      await page.getByLabel("Код задачи", { exact: true }).fill("T999");
      await page.getByLabel("Участник согласился", { exact: false }).check();
      await page
        .getByRole("button", { name: "Начать замер", exact: true })
        .click();
      assert(
        await page
          .getByRole("button", { name: "Завершить замер", exact: true })
          .isDisabled(),
      );
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
            name: "Завершить шаг привычного пути",
            exact: true,
          })
          .isDisabled(),
      );
      await page
        .getByLabel("Итог задачи", { exact: true })
        .selectOption("abandoned");
      page.once("dialog", (dialog) => dialog.dismiss());
      await page
        .getByRole("button", { name: "Завершить замер", exact: true })
        .click();
      assert(
        (
          await page.evaluate(
            (key) => JSON.parse(localStorage.getItem(key)).sessions.at(-1),
            JOURNEY_KEY,
          )
        ).open,
      );
      page.once("dialog", (dialog) => dialog.accept());
      await page
        .getByRole("button", { name: "Завершить замер", exact: true })
        .click();
      const ended = await page.evaluate(
        (key) => JSON.parse(localStorage.getItem(key)).sessions.at(-1),
        JOURNEY_KEY,
      );
      assert.equal(ended.outcome, "abandoned");
      assert.equal(ended.open, false);
      assert.deepEqual(errors, []);
      await context.close();
      console.log(
        `PASS statuses ${width}: Russian outcomes, precise missing-save reason, elapsed separate, history unchanged, explicit step/session outcome, cancel/confirm stop`,
      );
    }
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
