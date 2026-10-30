"""Учёт runner-контейнеров в базе: какой контейнер обслуживал диалог, на каком образе и когда.

Docker остаётся источником правды о живых контейнерах; записи нужны для истории и отладки,
поэтому ошибка записи не должна ронять запуск — она только логируется.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select, update

from backend.db import Database
from backend.models import Conversation, Run, RunnerContainer


logger = logging.getLogger(__name__)

# Роль агента в контейнере: runner ведёт всю команду CTF в одном процессе.
RUNNER_ROLE = "facilitator"


class ContainerRegistry:
    def __init__(self, database: Database):
        self.database = database

    async def started(
        self,
        project: str,
        *,
        container_id: str | None,
        name: str,
        image: str,
        network: str | None,
        run_id: str | None,
    ) -> None:
        try:
            async with self.database.session() as session:
                if await session.get(Conversation, project) is None:
                    return
                now = datetime.now(UTC)
                # Новый контейнер диалога: прежний, если запись ещё открыта, пропал без нас.
                await session.execute(
                    update(RunnerContainer)
                    .where(
                        RunnerContainer.conversation_id == project,
                        RunnerContainer.status == "running",
                    )
                    .values(status="gone", stopped_at=now, stop_reason="replaced")
                )
                session.add(RunnerContainer(
                    conversation_id=project,
                    last_run_id=await self._existing_run(session, run_id),
                    docker_container_id=container_id,
                    container_name=name,
                    agent_role=RUNNER_ROLE,
                    image_tag=image,
                    network_name=network,
                    status="running",
                    started_at=now,
                ))
                await session.commit()
        except Exception:
            logger.exception("Failed to record runner container of project %s", project)

    async def attach_run(self, project: str, run_id: str) -> None:
        try:
            async with self.database.session() as session:
                if await self._existing_run(session, run_id) is None:
                    return
                await session.execute(
                    update(RunnerContainer)
                    .where(
                        RunnerContainer.conversation_id == project,
                        RunnerContainer.status == "running",
                    )
                    .values(last_run_id=run_id)
                )
                await session.commit()
        except Exception:
            logger.exception("Failed to record run %s of project %s", run_id, project)

    async def stopped(self, project: str, *, status: str, reason: str | None) -> None:
        try:
            async with self.database.session() as session:
                await session.execute(
                    update(RunnerContainer)
                    .where(
                        RunnerContainer.conversation_id == project,
                        RunnerContainer.status == "running",
                    )
                    .values(status=status, stopped_at=datetime.now(UTC), stop_reason=reason)
                )
                await session.commit()
        except Exception:
            logger.exception("Failed to record stop of runner container of project %s", project)

    async def reconcile(self, alive: set[str]) -> None:
        """После перезапуска backend: открытые записи без контейнера в Docker закрываются."""
        try:
            async with self.database.session() as session:
                open_projects = set(await session.scalars(
                    select(RunnerContainer.conversation_id).where(RunnerContainer.status == "running")
                ))
                missing = open_projects - alive
                if missing:
                    await session.execute(
                        update(RunnerContainer)
                        .where(
                            RunnerContainer.conversation_id.in_(missing),
                            RunnerContainer.status == "running",
                        )
                        .values(status="gone", stopped_at=datetime.now(UTC), stop_reason="missing_on_restore")
                    )
                    await session.commit()
        except Exception:
            logger.exception("Failed to reconcile runner container records")

    @staticmethod
    async def _existing_run(session, run_id: str | None) -> str | None:
        if run_id is None:
            return None
        return run_id if await session.get(Run, run_id) is not None else None
