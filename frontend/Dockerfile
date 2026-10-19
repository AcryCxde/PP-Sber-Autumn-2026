# syntax=docker/dockerfile:1

FROM node:20-bookworm

ARG CLAUDE_CODE_VERSION=latest

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    HOME=/home/node \
    DATABASE_URL=sqlite+aiosqlite:////app/backend/data/history.db \
    WORKSPACE_ROOT=/home/node/.claude-web/workspaces \
    CLAUDE_PERMISSION_MODE=acceptEdits

RUN apt-get update \
    && apt-get install --yes --no-install-recommends python3 python3-venv supervisor \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && npm install --global "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"

WORKDIR /app

COPY pyproject.toml ./
COPY backend ./backend
RUN pip install --no-cache-dir .

COPY frontend/package.json frontend/package-lock.json ./frontend/
RUN npm ci --prefix frontend

COPY frontend ./frontend

RUN mkdir -p /app/backend/data /home/node/.claude-web/workspaces \
    && chown -R node:node /app /home/node/.claude-web \
    && printf '%s\n' \
        '[supervisord]' \
        'nodaemon=true' \
        'logfile=/dev/null' \
        'pidfile=/tmp/supervisord.pid' \
        '' \
        '[program:backend]' \
        'command=/opt/venv/bin/python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000' \
        'directory=/app' \
        'user=node' \
        'autorestart=true' \
        'stopasgroup=true' \
        'killasgroup=true' \
        'stdout_logfile=/dev/fd/1' \
        'stdout_logfile_maxbytes=0' \
        'stderr_logfile=/dev/fd/2' \
        'stderr_logfile_maxbytes=0' \
        '' \
        '[program:frontend]' \
        'command=/usr/local/bin/npm run dev -- --host 0.0.0.0 --port 5173' \
        'directory=/app/frontend' \
        'user=node' \
        'autorestart=true' \
        'stopasgroup=true' \
        'killasgroup=true' \
        'stdout_logfile=/dev/fd/1' \
        'stdout_logfile_maxbytes=0' \
        'stderr_logfile=/dev/fd/2' \
        'stderr_logfile_maxbytes=0' \
        > /etc/supervisor/conf.d/claude-web.conf

EXPOSE 5173 8000

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/claude-web.conf"]
