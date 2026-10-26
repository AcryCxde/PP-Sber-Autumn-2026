# Контейнер проекта: runner + Claude Code (бинарник внутри claude-agent-sdk) + Core Team Framework.
FROM python:3.12-slim

ARG CTF_REPO=https://github.com/noxxer/core-team
ARG CTF_COMMIT=416507a31409be1f63c939577d715ec63b61fc06

RUN apt-get update \
 && apt-get install -y --no-install-recommends git jq gawk diffutils curl procps ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN git clone -q "$CTF_REPO" /tmp/ctf && git -C /tmp/ctf checkout -q "$CTF_COMMIT" \
 && mkdir -p /opt/ctf && cp -R /tmp/ctf/.claude /opt/ctf/.claude && rm -rf /tmp/ctf

# Зависимости строго по uv.lock с проверкой хешей, затем сам пакет без зависимостей.
COPY --from=ghcr.io/astral-sh/uv:0.9.3 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock /src/
RUN cd /src && uv export --frozen --no-dev --no-emit-project -o /tmp/requirements.txt \
 && pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt
COPY src /src/src
RUN pip install --no-cache-dir --no-deps /src && rm -rf /src /usr/local/bin/uv

RUN useradd -m -u 1000 agent && mkdir -p /workspace /run/ctrunner && chown agent /workspace /run/ctrunner
USER agent
ENV HOME=/home/agent WORKSPACE=/workspace \
    GIT_AUTHOR_NAME=agent GIT_AUTHOR_EMAIL=agent@local \
    GIT_COMMITTER_NAME=agent GIT_COMMITTER_EMAIL=agent@local
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=2 CMD ["ctrunner-healthcheck"]
ENTRYPOINT ["ctrunner"]
