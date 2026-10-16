"use strict";

// Opt-in contract probes: node --test contract-tests/storage-git.js
const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { GitArtifactStorage } = require("../git-storage");

function fixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "coreteams-git-contract-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const storage = new GitArtifactStorage(directory);
  storage.initialize();
  // Test repository only: do not inherit a developer's Windows newline conversion.
  execFileSync("git", ["-C", directory, "config", "core.autocrlf", "false"]);
  const save = (operationId, content, runId = "run-1") => storage.commitArtifact({
    projectId: "project-1", runId, operationId, content,
    contentSha256: crypto.createHash("sha256").update(content, "utf8").digest("hex"),
  });
  return { storage, save, directory };
}

test("Git stores exact UTF-8 bytes/hash/byte length, including CRLF", (t) => {
  const { storage, save } = fixture(t);
  const content = "# Вывод 👩🏽‍💻\r\n\r\nИсточники: α, 中文.\r\n";
  const saved = save("operation-utf8", content);
  const bytes = storage.readArtifact(saved.commitSha, saved.gitPath);
  assert.deepEqual(bytes, Buffer.from(content, "utf8"));
  assert.equal(saved.sizeBytes, bytes.length);
  assert.equal(saved.contentSha256, crypto.createHash("sha256").update(bytes).digest("hex"));
});

test("older commit remains immutable after another version and worktree deletion", (t) => {
  const { storage, save, directory } = fixture(t);
  const first = save("operation-v1", "# Первая версия\n");
  const second = save("operation-v2", "# Вторая версия\n");
  assert.notEqual(first.commitSha, second.commitSha);
  fs.unlinkSync(path.join(directory, ...second.gitPath.split("/")));
  assert.equal(storage.readArtifact(first.commitSha, first.gitPath).toString("utf8"), "# Первая версия\n");
  assert.equal(storage.readArtifact(second.commitSha, second.gitPath).toString("utf8"), "# Вторая версия\n");
});

test("same operation and bytes replay the same commit; conflicting bytes are rejected", (t) => {
  const { save } = fixture(t);
  const first = save("operation-retry", "# Result\n");
  assert.deepEqual(save("operation-retry", "# Result\n"), first);
  assert.throws(() => save("operation-retry", "# Changed\n"), /другим содержимым/);
});

test("operation lookup matches the complete ID, not a longer ID prefix", (t) => {
  const { storage, save } = fixture(t);
  save("operation-prefix-long", "# Result\n");
  assert.equal(storage.findOperation("operation-prefix"), null);
});

test("Git boundary rejects path traversal and mismatched content digest", (t) => {
  const { storage, save } = fixture(t);
  assert.throws(() => save("operation-path", "# Result\n", "../foreign"), /недопустимое/);
  assert.throws(() => storage.commitArtifact({
    projectId: "project-1", runId: "run-1", operationId: "operation-hash",
    content: "# Result\n", contentSha256: "0".repeat(64),
  }), /не совпадает/);
});
