-- Таблица 1: Пользователи
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) NOT NULL UNIQUE,
    role VARCHAR(50) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Таблица 2: Проекты
CREATE TABLE projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title VARCHAR(255) NOT NULL,
    git_repo_url TEXT,
    status VARCHAR(50) NOT NULL
);

-- Таблица 3: Запуски
CREATE TABLE runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    status VARCHAR(50) NOT NULL,
    last_artifact_id UUID
);

-- Таблица 4: Артефакты
CREATE TABLE artifacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    run_id UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    title VARCHAR(255) NOT NULL,
    git_path TEXT,
    commit_sha VARCHAR(64),
    git_tree_url TEXT
);

-- Циклическая связь: runs -> artifacts
ALTER TABLE runs
    ADD CONSTRAINT fk_runs_last_artifact
    FOREIGN KEY (last_artifact_id) REFERENCES artifacts(id) ON DELETE SET NULL;

-- Таблица 5: Контейнеры
CREATE TABLE containers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    docker_container_id VARCHAR(255),
    agent_role VARCHAR(100),
    image_tag VARCHAR(255),
    network_name VARCHAR(255),
    status VARCHAR(50)
);