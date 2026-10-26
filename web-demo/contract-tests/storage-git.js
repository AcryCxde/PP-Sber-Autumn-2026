"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { GitArtifactStorage } = require("../git-storage");

function createStorage(t) {
  const repository = fs.mkdtempSync(path.join(os.tmpdir(), "coreteam-storage-git-"));
  t.after(() => fs.rmSync(repository, { recursive: true, force: true }));
  return new GitArtifactStorage(repository);
}

function artifact(overrides = {}) {
  const content = overrides.content || "Результат запуска\n";
  return {
    projectId: "project-1",
    runId: "run-1",
    operationId: "operation-1",
    content,
    contentSha256: crypto.createHash("sha256").update(content, "utf8").digest("hex"),
    ...overrides,
  };
}

test("operation lookup matches the complete ID", (t) => {
  const longStorage = createStorage(t);
  const longCommit = longStorage.commitArtifact(artifact({ operationId: "operation-prefix-long" }));

  assert.equal(longStorage.findOperation("operation-prefix"), null);
  assert.equal(longStorage.findOperation("operation-prefix-long"), longCommit.commitSha);
  assert.equal(longStorage.findOperation("operation-missing"), null);

  const shortStorage = createStorage(t);
  const shortCommit = shortStorage.commitArtifact(artifact({ operationId: "operation-prefix" }));

  assert.equal(shortStorage.findOperation("operation-prefix-long"), null);
  assert.equal(shortStorage.findOperation("operation-prefix"), shortCommit.commitSha);
});

test("idempotent retry returns the original commit", (t) => {
  const storage = createStorage(t);
  const request = artifact();
  const first = storage.commitArtifact(request);
  const retry = storage.commitArtifact(request);

  assert.equal(retry.commitSha, first.commitSha);
  assert.equal(retry.gitPath, first.gitPath);
  assert.equal(retry.contentSha256, first.contentSha256);
});

test("conflicting content for the same operation ID is rejected", (t) => {
  const storage = createStorage(t);
  storage.commitArtifact(artifact());

  assert.throws(
    () => storage.commitArtifact(artifact({ content: "Другой результат\n" })),
    /Operation-Id уже связан с другим содержимым/,
  );
});
