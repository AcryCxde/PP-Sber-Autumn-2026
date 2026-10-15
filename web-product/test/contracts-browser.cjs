// Synthetic network only: NOT real CTF, storage or a participant measurement.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const URL = process.env.FIXTURE_URL || "http://127.0.0.1:4191";
const OUT = path.join(__dirname, "../test-results");
fs.mkdirSync(OUT, { recursive: true });
const artifact = { id: "a1", documentId: "doc1", stageId: "understand", version: 1, title: "Fixture document", content: "# Fixture result\nTest only", saved: true, savedAt: "2026-10-16T00:00:00Z" };
const q = text => ({ id: text === "Original?" ? "old-fork" : "new-fork", questions: [
  { text, header: null, options: [{ label: "A", description: null }, { label: "B", description: null }], multi_select: true, allow_custom: true },
  { text: "Choose one?", header: null, options: [{ label: "Yes", description: null }, { label: "No", description: null }], multi_select: false, allow_custom: false },
] });
async function seed(page, snapshot) {
  await page.goto(URL);
  await page.evaluate(async snapshot => {
    const { newProject, KEY } = await import("/src/domain.ts");
    const p = { ...newProject(), id: "fixture-project", serverId: "server-project", step: 4, title: "Contract fixture", goal: "Synthetic task", run: snapshot };
    localStorage.setItem(KEY, JSON.stringify({ version: 3, projects: [p] }));
  }, snapshot);
  await page.reload();
  await page.getByRole("button").filter({ hasText: "Contract fixture" }).click();
}
(async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: process.env.PW_HEADLESS === "true" });
  try {
    for (const width of [1440, 390]) {
      const context = await browser.newContext({ viewport: { width, height: 1000 } });
      const page = await context.newPage(), errors = [];
      page.on("pageerror", e => errors.push(e.message));
      let snapshot = { id: "run1", revision: 1, status: "waiting_for_input", stageId: "understand", question: q("Original?"), messages: [], artifacts: [] };
      const attempts = [];
      let approveCount = 0;
      await page.route("**/api/**", async route => {
        const req = route.request();
        const json = value => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(value) });
        if (req.method() === "GET") return json(snapshot);
        if (req.url().endsWith("/answers")) {
          attempts.push({ key: req.headers()["idempotency-key"], body: req.postData() });
          if (attempts.length === 1) {
            snapshot = { ...snapshot, revision: 2, question: q("Updated?") };
            return route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ code: "unknown_fork" }) });
          }
          if (attempts.length === 2) return route.abort("failed");
          assert.deepEqual(req.postDataJSON().answers, { "Updated?": "A, B, Extra", "Choose one?": "No" });
          snapshot = { id: "run1", revision: 4, status: "completed", stageId: "understand", messages: [], artifacts: [artifact], roleEvents: [{ id: "role1", roleId: "analyst", type: "role.completed", at: "2026-10-16T00:00:00Z" }] };
          return json(snapshot);
        }
        if (req.url().endsWith("/approve")) {
          approveCount++;
          return json({ receiptId: "approval", artifactId: "a1", version: approveCount === 1 ? 99 : 1, approved: true, approvedAt: "2026-10-16T00:00:01Z" });
        }
        throw Error("Unexpected mutation: " + req.url());
      });
      await seed(page, snapshot);
      await page.getByRole("button", { name: "Команда", exact: true }).click();
      await page.getByText(/Подтверждённых событий ролей пока нет/).waitFor();
      await page.getByRole("button", { name: "Артефакты", exact: true }).click();
      await page.getByRole("checkbox", { name: "A", exact: true }).check();
      await page.getByRole("radio", { name: "Yes", exact: true }).check();
      await page.getByRole("button", { name: "Ответить и продолжить" }).click();
      await page.getByText("Updated?", { exact: true }).waitFor();
      assert.equal(await page.getByRole("checkbox", { name: "A", exact: true }).isChecked(), false);
      assert.equal(attempts.length, 1); // no auto resend
      await page.getByRole("checkbox", { name: "A", exact: true }).check();
      await page.getByRole("checkbox", { name: "B", exact: true }).check();
      await page.getByLabel("Свой ответ — вопрос 1", { exact: true }).fill("Extra");
      await page.getByRole("radio", { name: "No", exact: true }).check();
      await page.screenshot({ path: path.join(OUT, `contracts-questions-${width}.png`), fullPage: true });
      await page.getByRole("button", { name: "Ответить и продолжить" }).click();
      await page.getByText(/Исход доставки запроса не подтверждён/).waitFor();
      assert.equal(attempts.length, 2);
      snapshot = { ...snapshot, revision: 3, progress: { stage: "recovering", attempt: 1, context_restored: false } };
      await page.getByRole("button", { name: "Обновить статус" }).click();
      await page.getByText(/Сервис восстанавливает выполнение/).waitFor();
      assert.equal(attempts.length, 2); // GET recovery is not another POST
      await page.getByRole("button", { name: "Повторить запрос", exact: true }).click();
      await page.getByRole("heading", { name: "Fixture result" }).waitFor();
      assert.deepEqual(attempts[1], attempts[2]); // original key AND bytes
      const before = await page.evaluate(() => JSON.parse(localStorage.getItem("coreteams-product-workspace:v2")).projects[0].run);
      await page.getByRole("button", { name: "Согласовать результат", exact: true }).click();
      await page.getByRole("alert").filter({ hasText: "не подтвердил одобрение" }).waitFor();
      assert.equal(await page.getByRole("button", { name: "Версия согласована", exact: true }).count(), 0);
      await page.getByRole("button", { name: "Повторить запрос", exact: true }).click();
      await page.getByRole("button", { name: "Версия согласована", exact: true }).waitFor();
      const after = await page.evaluate(() => JSON.parse(localStorage.getItem("coreteams-product-workspace:v2")).projects[0].run);
      assert.deepEqual(after, before); // approval is outside terminal Run
      await page.getByRole("button", { name: "Команда", exact: true }).click();
      await page.getByText(/Аналитик: работа завершена/).waitFor();
      await page.getByRole("button", { name: "Артефакты", exact: true }).click();
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      assert.deepEqual(errors, []);
      await page.screenshot({ path: path.join(OUT, `contracts-${width}.png`), fullPage: true });
      await context.close();
      console.log(`PASS contracts ${width}: multi/custom, stale fork refresh/no answer carry, timeout/manual GET/recovery, stable retry, exact approval receipt, unchanged terminal Run`);
    }
    const context = await browser.newContext(), page = await context.newPage();
    let posts = 0;
    const unknown = { id: "unknown-run", revision: 2, status: "outcome_unknown", stageId: "understand", messages: [], artifacts: [] };
    await page.route("**/api/**", route => {
      if (route.request().method() !== "GET") posts++;
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(unknown) });
    });
    await seed(page, unknown);
    await page.getByText(/Сервер сообщил неизвестный исход/).waitFor();
    assert(await page.getByRole("button", { name: "Доработать в новом запуске" }).isDisabled());
    await page.getByRole("button", { name: "Обновить статус" }).click();
    assert.equal(posts, 0);
    await context.close();
    console.log("PASS explicit outcome_unknown: no success, auto POST or new run");
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
