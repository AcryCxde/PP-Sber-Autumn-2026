# syntax=docker/dockerfile:1
# Backend: FastAPI + Claude Code CLI, который backend запускает подпроцессом.
# Контекст сборки — корень репозитория (нужен pyproject.toml).

FROM node:20-bookworm-slim

ARG CLAUDE_CODE_VERSION=latest

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    HOME=/home/node \
    DATABASE_URL=sqlite+aiosqlite:////app/backend/data/history.db \
    WORKSPACE_ROOT=/home/node/.claude-web/workspaces \
    CLAUDE_PERMISSION_MODE=acceptEdits

RUN apt-get update \
    && apt-get install --yes --no-install-recommends python3 python3-venv git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && npm install --global "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"

WORKDIR /app

COPY pyproject.toml ./
COPY backend ./backend
RUN pip install --no-cache-dir .

RUN mkdir -p /app/backend/data /home/node/.claude-web/workspaces /home/node/.claude \
    && chown -R node:node /app /home/node

USER node
EXPOSE 8000

CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
