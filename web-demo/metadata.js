"use strict";

const { Pool } = require("pg");

class MetadataStore {
  constructor(databaseUrl) {
    this.pool = databaseUrl ? new Pool({ connectionString: databaseUrl }) : null;
  }

  get enabled() { return this.pool !== null; }

  async initialize() {
    if (this.pool) await this.pool.query("SELECT 1");
  }

  async close() {
    if (this.pool) await this.pool.end();
  }

  async projectId(client, projectSlug) {
    const result = await client.query("SELECT id FROM projects WHERE slug = $1", [projectSlug]);
    if (result.rowCount !== 1) throw new Error("Проект для сохранения не найден");
    return result.rows[0].id;
  }

  async ensureRun(projectSlug, run) {
    if (!this.pool) return;
    const client = await this.pool.connect();
    try {
      const projectId = await this.projectId(client, projectSlug);
      await client.query(
        `INSERT INTO runs (id, project_id, client_request_id, status, current_sequence, started_at, created_at)
         VALUES ($1, $2, $3, $4, 0, NOW(), NOW())
         ON CONFLICT (id) DO UPDATE SET
           status = EXCLUDED.status,
           finished_at = CASE WHEN EXCLUDED.status IN ('completed', 'failed', 'unknown', 'outcome_unknown', 'cancelled', 'limit_reached', 'limit_stopped') THEN NOW() ELSE runs.finished_at END`,
        [run.id, projectId, run.clientRequestId || `runner:${run.id}`, run.status],
      );
    } finally { client.release(); }
  }

  async appendEvent(projectSlug, event) {
    if (!this.pool) return;
    const client = await this.pool.connect();
    try {
      const projectId = await this.projectId(client, projectSlug);
      await client.query(
        `INSERT INTO events (id, run_id, project_id, sequence, type, actor, payload, created_at)
         VALUES ($1, $2, $3, $4, $5, NULL, $6::jsonb, $7)
         ON CONFLICT (run_id, sequence) DO NOTHING`,
        [event.eventId, event.runId, projectId, event.sequence, event.type, JSON.stringify(event.payload), event.createdAt],
      );
      await client.query(
        "UPDATE runs SET current_sequence = GREATEST(current_sequence, $2) WHERE id = $1",
        [event.runId, event.sequence],
      );
    } finally { client.release(); }
  }

  async createIntent(projectSlug, run, operation) {
    if (!this.pool) return;
    const client = await this.pool.connect();
    try {
      await client.query("BEGIN");
      const projectId = await this.projectId(client, projectSlug);
      await client.query(
        `INSERT INTO runs (id, project_id, client_request_id, status, current_sequence, started_at, created_at)
         VALUES ($1, $2, $3, 'running', 0, NOW(), NOW())
         ON CONFLICT (id) DO NOTHING`,
        [run.id, projectId, run.clientRequestId || `runner:${run.id}`],
      );
      await client.query(
        `INSERT INTO operations
           (operation_id, project_id, run_id, kind, state, target_git_path, content_sha256, created_at, updated_at)
         VALUES ($1, $2, $3, 'artifact_write', 'intent', $4, $5, NOW(), NOW())
         ON CONFLICT (operation_id) DO UPDATE SET updated_at = NOW()`,
        [operation.operationId, projectId, run.id, operation.gitPath, operation.contentSha256],
      );
      await client.query("COMMIT");
    } catch (error) {
      await client.query("ROLLBACK");
      throw error;
    } finally { client.release(); }
  }

  async confirmArtifact(projectSlug, run, operation, artifact, events) {
    if (!this.pool) return;
    const client = await this.pool.connect();
    try {
      await client.query("BEGIN");
      const projectId = await this.projectId(client, projectSlug);
      await client.query(
        `UPDATE operations SET state = 'confirmed', commit_sha = $2, updated_at = NOW()
         WHERE operation_id = $1 AND state IN ('intent', 'committed', 'confirmed')`,
        [operation.operationId, artifact.commitSha],
      );
      const storedArtifact = await client.query(
        `INSERT INTO artifacts
           (id, project_id, run_id, operation_id, title, mime_type, git_path, content_sha256,
            commit_sha, git_tree_url, size_bytes, created_at)
         VALUES ($1, $2, $3, $4, $5, 'text/markdown', $6, $7, $8, $9, $10, $11)
         ON CONFLICT (operation_id) DO UPDATE SET commit_sha = EXCLUDED.commit_sha
         RETURNING id`,
        [artifact.id, projectId, run.id, operation.operationId, artifact.name, artifact.gitPath,
          artifact.contentSha256, artifact.commitSha, artifact.gitTreeUrl, artifact.sizeBytes, artifact.createdAt],
      );
      artifact.id = storedArtifact.rows[0].id;
      for (const event of events) {
        if (event.payload.artifactId) event.payload.artifactId = artifact.id;
        if (event.payload.downloadUrl) {
          event.payload.downloadUrl = `/api/projects/${run.projectId}/artifacts/${artifact.id}`;
        }
        await client.query(
          `INSERT INTO events (id, run_id, project_id, sequence, type, actor, payload, created_at)
           VALUES ($1, $2, $3, $4, $5, NULL, $6::jsonb, $7)
           ON CONFLICT (run_id, sequence) DO NOTHING`,
          [event.eventId, event.runId, projectId, event.sequence, event.type, JSON.stringify(event.payload), event.createdAt],
        );
      }
      await client.query(
        `UPDATE runs SET status = 'completed', last_artifact_id = $2,
           current_sequence = GREATEST(current_sequence, $3), finished_at = NOW()
         WHERE id = $1`,
        [run.id, artifact.id, events.at(-1).sequence],
      );
      await client.query("COMMIT");
    } catch (error) {
      await client.query("ROLLBACK");
      throw error;
    } finally { client.release(); }
  }

  async failOperation(operationId) {
    if (!this.pool) return;
    await this.pool.query(
      "UPDATE operations SET state = 'failed', updated_at = NOW() WHERE operation_id = $1 AND state <> 'confirmed'",
      [operationId],
    );
  }
}

module.exports = { MetadataStore };
