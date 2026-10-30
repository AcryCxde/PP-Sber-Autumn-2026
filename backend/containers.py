"""Runner-контейнер на сессию пользователя: создание с контекстом, наблюдение, уборка по простою.

Один диалог — один проект ctrunner: контейнер `ct-<id>`, том проекта `proj-<id>`, том секретов
`ctsec-<id>` и, если задан каталог контекста, том `ctseed-<id>` (runner копирует его в проект
при первом старте). Изоляция повторяет `ctrunner.sessions.run_args`.

Сессия живёт в томах, контейнер одноразовый: после простоя он удаляется и при следующем
сообщении создаётся заново из тех же томов. Тома удаляются по сроку хранения
(RUNNER_VOLUME_RETENTION_SECONDS, 0 — никогда), при сбросе контекста и удалении диалога.

Токен gateway выводится из мастер-секрета HMAC-ом от project_id: после перезапуска backend
токены уже работающих контейнеров восстанавливаются без отдельного хранилища.
Docker SDK синхронный, поэтому публичные корутины уходят в поток.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import logging
import os
import re
import secrets
import sqlite3
import tarfile
import time
from collections.abc import Mapping
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from backend.config import Settings
from backend.runner_app import RunnerBridge
from backend.runner_transport import RunnerTransport

if TYPE_CHECKING:
    from backend.container_registry import ContainerRegistry


logger = logging.getLogger(__name__)

MANAGED_LABEL = "coreteams.managed"
PROJECT_LABEL = "coreteams.project"
AGENT_UID = 1000
PROJECT_DIR = PurePosixPath("/workspace/project")
PROJECT_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
# Что из окружения backend уходит в контейнер: адрес шлюза моделей и выбор моделей. Ключи — только файлом.
ENV_ALLOW = re.compile(
    r"ANTHROPIC_BASE_URL|ANTHROPIC_DEFAULT_[A-Z]+_MODEL(_NAME)?"
    r"|CLAUDE_CODE_(SUBAGENT_MODEL|AUTO_COMPACT_WINDOW|ENABLE_GATEWAY_MODEL_DISCOVERY)"
)
ENV_DENY = re.compile(r"TOKEN|KEY|SECRET|PASSWORD")
# Коды выхода ctrunner, после которых перезапуск не поможет (см. runner/README.md).
FATAL_EXIT_CODES = {65: "runner_state_corrupt", 78: "runner_config_rejected"}
# Docker returns a tar stream; an empty POSIX tar archive alone occupies 10 KiB.
TAR_ARCHIVE_OVERHEAD_BYTES = 10 * 1024
MATERIALS_WAIT_SECONDS = 60.0
# Запись активности в SQLite не чаще раза в минуту: для таймаутов в минутах и днях этого хватает.
ACTIVITY_WRITE_EVERY_SECONDS = 60.0


class ContainerError(RuntimeError):
    """Стабильный код ошибки для пользователя; без деталей Docker и секретов."""


@dataclass(frozen=True)
class ContainerStatus:
    project_id: str
    state: str
    health: str | None
    restarts: int
    exit_code: int | None
    started_at: float | None
    finished_at: float | None


def _docker_time(value: str | None) -> float | None:
    if not value or value.startswith("0001-"):
        return None
    head, _, fraction = value.rstrip("Z").partition(".")
    stamp = datetime.fromisoformat(head).replace(tzinfo=UTC).timestamp()
    return stamp + (float(f"0.{fraction[:6]}") if fraction.isdigit() else 0.0)


def _tar_files(
    files: Mapping[str, bytes],
    *,
    mode: int = 0o400,
    directories: tuple[str, ...] = (),
) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name in directories:
            info = tarfile.TarInfo(name)
            info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o755, int(time.time())
            info.uid = info.gid = AGENT_UID
            tar.addfile(info)
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), mode, int(time.time())
            info.uid = info.gid = AGENT_UID
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _tar_directory(root: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                continue  # ссылки из контекста не должны вести за пределы проекта
            info = tar.gettarinfo(str(path), path.relative_to(root).as_posix())
            info.uid = info.gid = AGENT_UID
            info.uname = info.gname = ""
            if path.is_file():
                with path.open("rb") as stream:
                    tar.addfile(info, stream)
            else:
                tar.addfile(info)
    return buffer.getvalue()


def _read_single_file(chunks: Any, *, max_bytes: int) -> bytes:
    """Extract one regular file without buffering more than its limit plus tar overhead."""
    budget = max_bytes + TAR_ARCHIVE_OVERHEAD_BYTES
    raw = bytearray()
    try:
        for chunk in chunks:
            if len(chunk) > budget - len(raw):
                raise ContainerError("artifact_unreadable")
            raw.extend(chunk)
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            member = next((member for member in tar if member.isfile()), None)
            if member is None or member.size > max_bytes:
                raise ContainerError("artifact_unreadable")
            stream = tar.extractfile(member)
            if stream is None:
                raise ContainerError("artifact_unreadable")
            data = stream.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ContainerError("artifact_unreadable")
            return data
    except ContainerError:
        raise
    except (tarfile.TarError, OSError, ValueError) as error:
        raise ContainerError("artifact_unreadable") from error
    except Exception as error:
        # Docker's lazy archive iterator can fail after get_archive() has returned.
        raise ContainerError("artifact_unreadable") from error


class ContainerManager:
    def __init__(
        self,
        settings: Settings,
        bridge: RunnerBridge,
        transport: RunnerTransport,
        *,
        client: Any = None,
        environ: Mapping[str, str] | None = None,
        registry: ContainerRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.bridge = bridge
        self.transport = transport
        self._client = client
        self._environ = os.environ if environ is None else environ
        self._master = self._load_master_secret(settings.runner_secret_file)
        self._activity: dict[str, float] = {}
        self._starting: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.relaying: set[str] = set()

    @property
    def client(self) -> Any:
        if self._client is None:
            import docker  # только в режиме runner; режим cli обходится без Docker SDK

            self._client = docker.from_env()
        return self._client

    @staticmethod
    def _load_master_secret(path: Path) -> bytes:
        try:
            return path.read_bytes().strip()
        except FileNotFoundError:
            path.parent.mkdir(parents=True, exist_ok=True)
            value = secrets.token_urlsafe(32).encode()
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(value)
            return value

    @staticmethod
    def project_id(conversation_id: str) -> str:
        project = conversation_id.lower()
        if not PROJECT_ID_RE.fullmatch(project):
            raise ContainerError("invalid_project_id")
        return project

    def token(self, project: str) -> str:
        return hmac.new(self._master, project.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def names(project: str) -> dict[str, str]:
        return {"container": f"ct-{project}", "project": f"proj-{project}",
                "secrets": f"ctsec-{project}", "seed": f"ctseed-{project}"}

    # --- последняя активность -----------------------------------------------------------------
    # Хранится в файле транспорта: после удаления контейнера и перезапуска backend
    # срок хранения томов всё равно считается от реального последнего использования.

    def _activity_db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.transport.database, timeout=10)
        db.execute("CREATE TABLE IF NOT EXISTS runner_activity ("
                   "project TEXT PRIMARY KEY, last_used REAL NOT NULL)")
        return db

    def touch(self, project: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        previous = self._activity.get(project)
        self._activity[project] = now
        if previous is None or now - previous >= ACTIVITY_WRITE_EVERY_SECONDS:
            with closing(self._activity_db()) as db, db:
                db.execute("INSERT INTO runner_activity VALUES(?,?) ON CONFLICT(project) "
                           "DO UPDATE SET last_used=MAX(last_used, excluded.last_used)", (project, now))

    def last_activity(self, project: str) -> float | None:
        return self._activity.get(project)

    def _forget_activity(self, project: str) -> None:
        self._activity.pop(project, None)
        with closing(self._activity_db()) as db, db:
            db.execute("DELETE FROM runner_activity WHERE project=?", (project,))

    def _lock(self, project: str) -> asyncio.Lock:
        return self._locks.setdefault(project, asyncio.Lock())

    # --- жизненный цикл -------------------------------------------------------------------
    # Контейнер одноразовый (корень только для чтения, всё состояние в томах): после простоя
    # он удаляется и пересоздаётся из тех же томов со свежим образом, окружением и секретами.

    async def restore(self) -> list[str]:
        """После перезапуска backend: вернуть токены работающих контейнеров и учёт активности."""
        with closing(self._activity_db()) as db:
            self._activity.update(db.execute("SELECT project,last_used FROM runner_activity"))
        containers = await asyncio.to_thread(self.list_containers)
        for project in containers:
            self.bridge.register(project, self.token(project))
        if self.registry is not None:
            await self.registry.reconcile(set(containers))
        return containers

    async def ensure_running(self, project: str, *, run_id: str | None = None) -> bool:
        """Создать контейнер проекта из его томов. True — контейнер только что стартовал."""
        async with self._lock(project):
            self.bridge.register(project, self.token(project))
            created = await asyncio.to_thread(self._ensure_running_sync, project)
            if created is not None:
                self._starting[project] = time.time()
                if self.registry is not None:
                    await self.registry.started(
                        project,
                        container_id=getattr(created, "id", None),
                        name=self.names(project)["container"],
                        image=self.settings.runner_image,
                        network=self.settings.runner_network,
                        run_id=run_id,
                    )
            elif run_id is not None and self.registry is not None:
                await self.registry.attach_run(project, run_id)
            self.touch(project)
            return created is not None

    def _ensure_running_sync(self, project: str) -> Any:
        """Новый запущенный контейнер или None, если он уже работал."""
        container = self._get(self.names(project)["container"])
        if container is not None:
            if container.attrs["State"]["Status"] in {"running", "restarting"}:
                return None
            container.remove(force=True)  # остановленный: пересоздать на текущем образе
        self.transport.connecting(project)
        self._provision(project)
        created = self._create(project)
        created.start()
        logger.info("Runner container for project %s started", project)
        return created

    async def retire(self, project: str) -> None:
        """Удалить контейнер, сохранив тома: сессия продолжится при следующем сообщении."""
        async with self._lock(project):
            container = await asyncio.to_thread(self._get, self.names(project)["container"])
            if container is not None:
                await asyncio.to_thread(container.stop, timeout=15)
                await asyncio.to_thread(container.remove, force=True)
                logger.info("Runner container for project %s removed, volumes kept", project)
            self.bridge.revoke(project)
            self._starting.pop(project, None)
            if self.registry is not None:
                await self.registry.stopped(project, status="retired", reason="idle")

    async def discard(self, project: str, *, reason: str) -> None:
        """Удалить контейнер и все тома проекта: сброс контекста, удаление диалога, срок хранения."""
        async with self._lock(project):
            active = self.transport.active_run(project)
            if active is not None:
                self.transport.abandon(project, active["run_id"], reason)
            await asyncio.to_thread(self._remove_sync, project)
            self.bridge.revoke(project)
            self._starting.pop(project, None)
            self._forget_activity(project)
            if self.registry is not None:
                await self.registry.stopped(project, status="discarded", reason=reason)
            logger.info("Runner container and volumes for project %s removed: %s", project, reason)

    def _remove_sync(self, project: str) -> None:
        names = self.names(project)
        container = self._get(names["container"])
        if container is not None:
            container.remove(force=True)
        for key in ("project", "secrets", "seed"):
            volume = self._get_volume(names[key])
            if volume is not None:
                volume.remove(force=True)

    async def read_file(self, project: str, path: str, *, max_bytes: int) -> bytes:
        container = await asyncio.to_thread(self._get, self.names(project)["container"])
        if container is None:
            raise ContainerError("runner_container_missing")
        chunks, _ = await asyncio.to_thread(container.get_archive, str(PROJECT_DIR / path))
        return await asyncio.to_thread(_read_single_file, chunks, max_bytes=max_bytes)

    async def write_files(
        self,
        project: str,
        directory: str,
        files: Mapping[str, bytes],
        *,
        wait_seconds: float = MATERIALS_WAIT_SECONDS,
    ) -> None:
        """Кладёт файлы в каталог проекта агента; удалённые ранее файлы остаются на томе.

        Каталог проекта создаёт сам runner (seed_project) сразу после старта, до подключения
        к gateway. Создавать его раньше нельзя — runner тогда пропустит копирование seed,
        поэтому ждём его появления. Команда этапа уходит runner уже после записи файлов.
        """
        from docker.errors import NotFound

        container = await asyncio.to_thread(self._get, self.names(project)["container"])
        if container is None:
            raise ContainerError("runner_container_missing")
        archive = _tar_files({f"{directory}/{name}": data for name, data in files.items()},
                             mode=0o644, directories=(directory,))
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                await asyncio.to_thread(container.put_archive, str(PROJECT_DIR), archive)
                return
            except NotFound:
                if time.monotonic() >= deadline:
                    raise ContainerError("runner_project_missing") from None
                await asyncio.sleep(0.5)

    def list_containers(self) -> list[str]:
        containers = self.client.containers.list(all=True, filters={"label": f"{MANAGED_LABEL}=1"})
        return sorted({c.labels[PROJECT_LABEL] for c in containers if PROJECT_LABEL in c.labels})

    def list_projects(self) -> list[str]:
        """Проекты с контейнером или хотя бы с сохранёнными томами."""
        volumes = self.client.volumes.list(filters={"label": f"{MANAGED_LABEL}=volume"})
        kept = {v.attrs["Labels"][PROJECT_LABEL] for v in volumes
                if PROJECT_LABEL in (v.attrs.get("Labels") or {})}
        return sorted(kept | set(self.list_containers()))

    def inspect(self, project: str) -> ContainerStatus:
        container = self._get(self.names(project)["container"])
        if container is None:
            return ContainerStatus(project, "missing", None, 0, None, None, None)
        state = container.attrs["State"]
        return ContainerStatus(
            project_id=project,
            state=state["Status"],
            health=(state.get("Health") or {}).get("Status"),
            restarts=int(container.attrs.get("RestartCount") or 0),
            exit_code=state.get("ExitCode") if state["Status"] in {"exited", "dead"} else None,
            started_at=_docker_time(state.get("StartedAt")),
            finished_at=_docker_time(state.get("FinishedAt")),
        )

    def describe(self, project: str) -> dict[str, Any]:
        """Сводка для API: Docker + последнее heartbeat runner + активный запуск."""
        status = self.inspect(project)
        heartbeat = self.transport.project_state(project)
        active = self.transport.active_run(project)
        return {
            **asdict(status),
            "connected": self.bridge.is_connected(project),
            "phase": heartbeat["phase"],
            "runner_health": heartbeat["health"],
            "last_heartbeat_at": heartbeat["seen"],
            "last_activity_at": self._activity.get(project),
            "active_run": active and {"run_id": active["run_id"], "status": active["status"]},
            "volumes_kept": self._get_volume(self.names(project)["project"]) is not None,
            "idle_timeout_seconds": self.settings.runner_idle_timeout_seconds,
            "volume_retention_seconds": self.settings.runner_volume_retention_seconds,
        }

    # --- Docker -------------------------------------------------------------------------

    def _get(self, name: str) -> Any:
        from docker.errors import NotFound

        try:
            container = self.client.containers.get(name)
        except NotFound:
            return None
        return container

    def _get_volume(self, name: str) -> Any:
        from docker.errors import NotFound

        try:
            return self.client.volumes.get(name)
        except NotFound:
            return None

    def _provision(self, project: str) -> None:
        token = self.settings.anthropic_auth_token
        if not token:
            raise ContainerError("anthropic_token_missing")
        names = self.names(project)
        labels = {MANAGED_LABEL: "volume", PROJECT_LABEL: project}
        for key in ("project", "secrets"):
            if self._get_volume(names[key]) is None:
                self.client.volumes.create(names[key], labels=labels)
        # Секреты переписываются при каждом создании контейнера: смена ключа без правки томов.
        mounts = {names["secrets"]: {"bind": "/secrets", "mode": "rw"}}
        context = self.settings.runner_context_dir
        if (context is not None and context.is_dir()
                and self._get_volume(names["seed"]) is None):
            # Контекст копируется один раз: runner берёт его только в пустой проект.
            self.client.volumes.create(names["seed"], labels=labels)
            mounts[names["seed"]] = {"bind": "/seed", "mode": "rw"}
        # Вспомогательный контейнер не запускается: docker cp пишет прямо в его тома.
        helper = self.client.containers.create(
            self.settings.runner_image, entrypoint=["true"], user="0",
            network_disabled=True, volumes=mounts,
            labels={MANAGED_LABEL: "helper", PROJECT_LABEL: project},
        )
        try:
            helper.put_archive("/secrets", _tar_files({
                "anthropic_token": token.encode() + b"\n",
                "gateway_token": self.token(project).encode() + b"\n",
            }))
            if names["seed"] in mounts:
                helper.put_archive("/seed", _tar_directory(context))
        finally:
            helper.remove(force=True)

    def _create(self, project: str) -> Any:
        names = self.names(project)
        env = {k: v for k, v in self._environ.items()
               if ENV_ALLOW.fullmatch(k) and not ENV_DENY.search(k)}
        env.update(
            PROJECT_ID=project,
            STALL_AFTER_S=f"{self.settings.runner_stall_after_seconds:g}",
            GATEWAY_URL=self.settings.runner_gateway_url,
        )
        volumes = {
            names["project"]: {"bind": "/workspace", "mode": "rw"},
            names["secrets"]: {"bind": "/run/secrets", "mode": "ro"},
        }
        if self._get_volume(names["seed"]) is not None:
            volumes[names["seed"]] = {"bind": "/seed", "mode": "ro"}
        return self.client.containers.create(
            self.settings.runner_image,
            name=names["container"],
            labels={MANAGED_LABEL: "1", PROJECT_LABEL: project},
            # init: PID 1 собирает зомби от Bash-процессов агента, иначе упрёмся в pids_limit.
            init=True,
            restart_policy={"Name": "on-failure", "MaximumRetryCount": 5},
            user="agent",
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            mem_limit=self.settings.runner_memory,
            nano_cpus=int(self.settings.runner_cpus * 1e9),
            pids_limit=256,
            tmpfs={"/tmp": "", "/home/agent": f"uid={AGENT_UID}", "/run/ctrunner": f"uid={AGENT_UID}"},
            volumes=volumes,
            environment=env,
            network=self.settings.runner_network,
        )

    # --- наблюдение и таймауты --------------------------------------------------------------

    async def supervise(self) -> None:
        while True:
            try:
                await self.supervise_once()
            except Exception:
                logger.exception("Runner supervision pass failed")
            await asyncio.sleep(self.settings.runner_supervise_interval_seconds)

    async def supervise_once(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for project in await asyncio.to_thread(self.list_projects):
            status = await asyncio.to_thread(self.inspect, project)
            if project not in self._activity:
                self.touch(project, now)  # неизвестная активность: отсчёт с первого наблюдения
            active = self.transport.active_run(project)
            if active is None:
                await self._supervise_idle(project, status, now)
            else:
                await self._supervise_active(project, status, active, now)

    async def _supervise_active(
        self, project: str, status: ContainerStatus, active: dict[str, Any], now: float,
    ) -> None:
        if self.bridge.is_connected(project):
            self._starting.pop(project, None)
        if status.state in {"exited", "dead", "missing"}:
            code = FATAL_EXIT_CODES.get(status.exit_code or 0, "runner_exited")
            if self.transport.abandon(project, active["run_id"], code):
                logger.warning("Run %s of project %s abandoned: %s", active["run_id"], project, code)
            return
        started = self._starting.get(project)
        if started is not None and now - started > self.settings.runner_connect_timeout_seconds:
            self._starting.pop(project, None)
            if self.transport.abandon(project, active["run_id"], "runner_connect_timeout"):
                logger.warning("Runner of project %s did not connect in time", project)
        if status.health == "unhealthy":
            logger.warning("Runner container of project %s is unhealthy (restarts=%s)",
                           project, status.restarts)

    async def _supervise_idle(self, project: str, status: ContainerStatus, now: float) -> None:
        idle_for = now - self._activity[project]
        if status.state != "missing":
            # Работающий или упавший контейнер без работы: после простоя удаляется, тома остаются.
            if idle_for > self.settings.runner_idle_timeout_seconds:
                logger.info("Runner of project %s idle for %.0fs, retiring", project, idle_for)
                await self.retire(project)
            return
        retention = self.settings.runner_volume_retention_seconds
        if retention > 0 and idle_for > retention:
            await self.discard(project, reason="volume_retention_expired")
