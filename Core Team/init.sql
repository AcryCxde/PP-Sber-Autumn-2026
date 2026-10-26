CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email VARCHAR(255) UNIQUE NOT NULL,
    role VARCHAR(32) NOT NULL DEFAULT 'user',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE projects (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    owner_id UUID NOT NULL REFERENCES users(id),
    title VARCHAR(255) NOT NULL,
    description TEXT,
    status VARCHAR(32) NOT NULL DEFAULT 'draft',
    git_repo_url VARCHAR(512) NOT NULL,
    git_branch VARCHAR(128) NOT NULL DEFAULT 'main',
    context_data JSONB NOT NULL DEFAULT '{}',
    schema_version INT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE operations (
    operation_id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id),
    run_id UUID,
    kind VARCHAR(32) NOT NULL,
    state VARCHAR(32) NOT NULL,
    target_git_path VARCHAR(512) NOT NULL,
    content_sha256 CHAR(64) NOT NULL,
    commit_sha VARCHAR(40),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE artifacts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id UUID NOT NULL REFERENCES projects(id),
    run_id UUID NOT NULL,
    operation_id UUID UNIQUE NOT NULL REFERENCES operations(operation_id),
    title VARCHAR(255) NOT NULL,
    mime_type VARCHAR(64) NOT NULL DEFAULT 'text/markdown',
    git_path VARCHAR(512) NOT NULL,
    content_sha256 CHAR(64) NOT NULL,
    commit_sha VARCHAR(40) NOT NULL,
    git_tree_url VARCHAR(512),
    size_bytes BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE runs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id UUID NOT NULL REFERENCES projects(id),
    client_request_id VARCHAR(128) UNIQUE NOT NULL,
    status VARCHAR(32) NOT NULL,
    current_sequence INT NOT NULL DEFAULT 0,
    last_artifact_id UUID REFERENCES artifacts(id),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_runs_one_active_per_project
    ON runs (project_id)
    WHERE status IN ('queued', 'running', 'waiting_for_user');

CREATE TABLE events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id UUID NOT NULL REFERENCES runs(id),
    project_id UUID NOT NULL REFERENCES projects(id),
    sequence INT NOT NULL,
    type VARCHAR(64) NOT NULL,
    actor JSONB,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_events_run_sequence UNIQUE (run_id, sequence)
);

INSERT INTO users (id, email, role)
VALUES ('00000000-0000-0000-0000-000000000001', 'test@example.com', 'user');

INSERT INTO projects (id, owner_id, title, git_repo_url, context_data)
VALUES (
    '11111111-1111-1111-1111-111111111111',
    '00000000-0000-0000-0000-000000000001',
    'Demo Project',
    'https://git.internal/repo.git',
    '{"goal": "Analytics report", "inputs": ["spec.md"]}'
);