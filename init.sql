CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) NOT NULL UNIQUE,
    role VARCHAR(32) NOT NULL CHECK (role IN ('guest', 'user', 'admin')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    slug VARCHAR(128) NOT NULL UNIQUE,
    title VARCHAR(255) NOT NULL,
    description TEXT,
    status VARCHAR(32) NOT NULL CHECK (status IN ('draft', 'team_proposed', 'active', 'archived')),
    git_repo_url VARCHAR(512) NOT NULL,
    git_branch VARCHAR(128) NOT NULL DEFAULT 'main',
    context_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    schema_version INT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE runs (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    client_request_id VARCHAR(128) NOT NULL UNIQUE,
    status VARCHAR(32) NOT NULL CHECK (status IN (
        'queued', 'running', 'waiting_for_user', 'partial', 'completed',
        'failed', 'cancelled', 'limit_stopped', 'limit_reached',
        'outcome_unknown', 'unknown'
    )),
    current_sequence INT NOT NULL DEFAULT 0,
    last_artifact_id UUID,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX idx_runs_one_active_per_project
    ON runs (project_id)
    WHERE status IN ('queued', 'running', 'waiting_for_user');

CREATE TABLE operations (
    operation_id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    run_id UUID REFERENCES runs(id) ON DELETE CASCADE,
    kind VARCHAR(32) NOT NULL CHECK (kind IN ('artifact_write', 'material_upload', 'ctf_sync')),
    state VARCHAR(32) NOT NULL CHECK (state IN ('intent', 'committed', 'confirmed', 'failed')),
    target_git_path VARCHAR(512) NOT NULL,
    content_sha256 CHAR(64) NOT NULL,
    commit_sha VARCHAR(40),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX uq_operations_run_path_hash
    ON operations (run_id, target_git_path, content_sha256)
    WHERE run_id IS NOT NULL;

CREATE TABLE events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sequence INT NOT NULL,
    type VARCHAR(64) NOT NULL,
    actor JSONB,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_events_run_sequence UNIQUE (run_id, sequence)
);

CREATE TABLE artifacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    run_id UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    operation_id UUID NOT NULL UNIQUE REFERENCES operations(operation_id),
    title VARCHAR(255) NOT NULL,
    mime_type VARCHAR(64) NOT NULL DEFAULT 'text/markdown',
    git_path VARCHAR(512) NOT NULL,
    content_sha256 CHAR(64) NOT NULL,
    commit_sha VARCHAR(40) NOT NULL,
    git_tree_url VARCHAR(512),
    size_bytes BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_artifacts_run_operation UNIQUE (run_id, operation_id)
);

ALTER TABLE runs
    ADD CONSTRAINT fk_runs_last_artifact
    FOREIGN KEY (last_artifact_id) REFERENCES artifacts(id) ON DELETE SET NULL;

CREATE TABLE materials (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    operation_id UUID NOT NULL UNIQUE REFERENCES operations(operation_id),
    filename VARCHAR(255) NOT NULL,
    mime_type VARCHAR(128) NOT NULL,
    size_bytes BIGINT NOT NULL,
    git_path VARCHAR(512) NOT NULL,
    content_sha256 CHAR(64) NOT NULL,
    commit_sha VARCHAR(40) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE ctf_files (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    run_id UUID REFERENCES runs(id) ON DELETE CASCADE,
    operation_id UUID NOT NULL UNIQUE REFERENCES operations(operation_id),
    kind VARCHAR(64) NOT NULL CHECK (kind IN ('team_manifest', 'role_definition', 'framework_state')),
    git_path VARCHAR(512) NOT NULL,
    content_sha256 CHAR(64) NOT NULL,
    commit_sha VARCHAR(40) NOT NULL,
    is_internal BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE containers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    docker_container_id VARCHAR(255),
    agent_role VARCHAR(100),
    image_tag VARCHAR(255),
    network_name VARCHAR(255),
    status VARCHAR(50),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO users (id, email, role) VALUES
    ('00000000-0000-4000-8000-000000000001', 'demo-user@coreteam.local', 'user'),
    ('00000000-0000-4000-8000-000000000002', 'demo-other@coreteam.local', 'user');

INSERT INTO projects (id, owner_id, slug, title, status, git_repo_url) VALUES
    ('10000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000001', 'demo-main', 'Пересмотр учебной программы', 'active', 'file:///var/lib/coreteam/artifacts'),
    ('10000000-0000-4000-8000-000000000002', '00000000-0000-4000-8000-000000000002', 'demo-isolated', 'Изолированный тестовый проект', 'active', 'file:///var/lib/coreteam/artifacts');
