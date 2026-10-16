"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

const SAFE_SEGMENT = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$/;
const SHA256 = /^[a-f0-9]{64}$/;
const COMMIT_SHA = /^[a-f0-9]{40}$/;

function git(repository, args, options = {}) {
  return execFileSync("git", ["-C", repository, ...args], {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    ...options,
  }).trim();
}

function assertSegment(value, field) {
  if (typeof value !== "string" || !SAFE_SEGMENT.test(value)) {
    throw new Error(`${field} содержит недопустимое значение`);
  }
  return value;
}

function assertSha256(value) {
  if (typeof value !== "string" || !SHA256.test(value)) {
    throw new Error("contentSha256 должен быть SHA-256 в hex");
  }
  return value;
}

class GitArtifactStorage {
  constructor(repository) {
    this.repository = path.resolve(repository);
  }

  initialize() {
    fs.mkdirSync(this.repository, { recursive: true });
    if (!fs.existsSync(path.join(this.repository, ".git"))) {
      git(this.repository, ["init", "-q"]);
      git(this.repository, ["symbolic-ref", "HEAD", "refs/heads/main"]);
      git(this.repository, ["config", "user.name", "CoreTeam Storage"]);
      git(this.repository, ["config", "user.email", "storage@coreteam.local"]);
      git(this.repository, ["commit", "-q", "--allow-empty", "-m", "Initialize artifact storage"]);
    }
  }

  artifactPath(projectId, runId) {
    return `projects/${assertSegment(projectId, "projectId")}/runs/${assertSegment(runId, "runId")}/result.md`;
  }

  findOperation(operationId) {
    assertSegment(operationId, "operationId");
    const commitSha = git(
      this.repository,
      ["log", "--all", "-1", "--format=%H", `--grep=Operation-Id: ${operationId}`],
    );
    return commitSha || null;
  }

  commitArtifact({ projectId, runId, operationId, content, contentSha256 }) {
    this.initialize();
    assertSegment(operationId, "operationId");
    assertSha256(contentSha256);
    if (typeof content !== "string" || content.length === 0) {
      throw new Error("Пустой артефакт не сохраняется");
    }

    const actualHash = crypto.createHash("sha256").update(content, "utf8").digest("hex");
    if (actualHash !== contentSha256) {
      throw new Error("Контрольная сумма артефакта не совпадает");
    }

    const gitPath = this.artifactPath(projectId, runId);
    const existingCommit = this.findOperation(operationId);
    if (existingCommit) {
      const restored = this.readArtifact(existingCommit, gitPath);
      const restoredHash = crypto.createHash("sha256").update(restored).digest("hex");
      if (restoredHash !== contentSha256) {
        throw new Error("Operation-Id уже связан с другим содержимым");
      }
      return { commitSha: existingCommit, gitPath, contentSha256, sizeBytes: Buffer.byteLength(restored) };
    }

    const target = path.join(this.repository, ...gitPath.split("/"));
    fs.mkdirSync(path.dirname(target), { recursive: true });
    const temporary = `${target}.tmp-${process.pid}-${crypto.randomUUID()}`;
    fs.writeFileSync(temporary, content, "utf8");
    fs.renameSync(temporary, target);

    git(this.repository, ["add", "--", gitPath]);
    git(this.repository, [
      "-c", "core.hooksPath=/dev/null",
      "commit", "-q", "--allow-empty",
      "-m", `feat(coreteam): save artifact for run ${runId}`,
      "-m", `Operation-Id: ${operationId}`,
      "-m", `Content-SHA256: ${contentSha256}`,
    ]);
    const commitSha = git(this.repository, ["rev-parse", "HEAD"]);
    return {
      commitSha,
      gitPath,
      contentSha256,
      sizeBytes: Buffer.byteLength(content, "utf8"),
    };
  }

  readArtifact(commitSha, gitPath) {
    if (typeof commitSha !== "string" || !COMMIT_SHA.test(commitSha)) {
      throw new Error("Некорректный commit SHA");
    }
    if (typeof gitPath !== "string" || !gitPath.startsWith("projects/") || gitPath.includes("..")) {
      throw new Error("Некорректный путь артефакта");
    }
    return execFileSync("git", ["-C", this.repository, "show", `${commitSha}:${gitPath}`]);
  }
}

module.exports = { GitArtifactStorage };
