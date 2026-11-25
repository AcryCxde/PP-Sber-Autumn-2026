"""Catalog, independent gateway checks and durable admission policy for Claude CLI."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings
from backend.db import Database
from backend.models import AiModelHealth, AiModelRouting, AiModelRoutingAudit
from backend.timestamps import utc_iso

logger = logging.getLogger(__name__)
CHECK_INTERVAL = 300
HEALTH_TTL = timedelta(seconds=2 * CHECK_INTERVAL)
REQUEST_TIMEOUT = 12
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}")
RETRYABLE_MODEL_ERRORS = frozenset({"ai_launches_paused", "ai_models_unavailable", "ai_model_request_failed"})
MODEL_ERROR_MESSAGES = {
    "ai_launches_paused": "Администратор временно приостановил новые запуски агентов.",
    "ai_models_unavailable": "Модели временно недоступны. Повторите попытку позже.",
    "ai_model_request_failed": "Модель перестала отвечать. Этап можно повторить на доступной модели.",
}
MODEL_FAILURES = frozenset({
    "authentication_failed", "billing_error", "rate_limit", "server_error", "model_not_found",
    "overloaded", "connection_error",
})
MODEL_FAILURE_HTTP_STATUSES = frozenset({401, 402, 403, 404, 408, 429})
PROVIDER_FAILURE_REASON = "provider_request_failed"
CLI_HTTP_ERROR = re.compile(r"\s*API Error:\s*(\d{3})\b")
CLI_CONNECTION_ERROR = re.compile(r"\s*API Error:\s*Unable to connect to API\b")


class ModelRoutingError(Exception):
    def __init__(self, code: str, status: int, message: str):
        super().__init__(message)
        self.code, self.status = code, status


class GatewayError(Exception):
    def __init__(self, reason: str, status: int | None = None):
        super().__init__(reason)
        self.reason, self.status = reason, status


def valid_model(value: object) -> bool:
    return isinstance(value, str) and MODEL_ID.fullmatch(value) is not None


def provider_failure(value: object, detail: object = None, *, http_status: int | None = None) -> bool:
    if isinstance(value, str) and value in MODEL_FAILURES:
        return True
    if isinstance(detail, str):
        # The installed CLI reports connection refusals as category "unknown".
        if CLI_CONNECTION_ERROR.match(detail):
            return True
        if http_status is None and (match := CLI_HTTP_ERROR.match(detail)):
            http_status = int(match[1])
    return http_status is not None and (http_status in MODEL_FAILURE_HTTP_STATUSES or 500 <= http_status < 600)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


@dataclass(frozen=True)
class Check:
    state: str
    reason: str | None = None
    reported_model: str | None = None


class GatewayModels:
    def __init__(self, base_url: str, token: str):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("ANTHROPIC_BASE_URL must be an HTTP(S) gateway URL")
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}", "anthropic-version": "2023-06-01"}

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with httpx.AsyncClient(headers=self.headers, follow_redirects=False) as client:
                    async with client.stream(method, self.base_url + path, **kwargs) as response:
                        if not response.is_success:
                            code = response.status_code
                            reason = "access_rejected" if code in {401, 403} else "model_unavailable"
                            raise GatewayError(reason, code)
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > 2 * 1024 * 1024:
                                raise GatewayError("invalid_gateway_response")
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise GatewayError("invalid_gateway_response")
            return payload
        except (httpx.RequestError, TimeoutError) as error:
            raise GatewayError("gateway_unreachable") from error
        except (ValueError, UnicodeError) as error:
            raise GatewayError("invalid_gateway_response") from error

    async def catalog(self) -> list[str]:
        ids: set[str] = set()
        seen_cursors: set[str] = set()
        after: str | None = None
        for _ in range(20):
            data = await self._request("GET", "/v1/models", params={"after_id": after} if after else {})
            rows = data.get("data")
            if not isinstance(rows, list):
                raise GatewayError("invalid_gateway_response")
            ids.update(row["id"] for row in rows if isinstance(row, dict) and valid_model(row.get("id")))
            if data.get("has_more") is not True:
                return sorted(ids)
            after = data.get("last_id")
            if not valid_model(after):
                after = rows[-1].get("id") if rows and isinstance(rows[-1], dict) else None
            if not valid_model(after) or after in seen_cursors:
                break
            seen_cursors.add(after)
        raise GatewayError("invalid_gateway_response")

    async def check(self, model: str) -> Check:
        marker = "coreteams_probe"
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": 256,
            "messages": [{"role": "user", "content": f"Use the {marker} tool."}],
            "tools": [{"name": marker, "description": "Synthetic health check", "input_schema": {"type": "object"}}],
            "tool_choice": {"type": "tool", "name": marker},
        }
        try:
            data = await self._request("POST", "/v1/messages", json=body)
        except GatewayError as error:
            if error.status in {400, 422}:
                return Check("incompatible", "tools_unsupported")
            return Check("unavailable", error.reason)
        blocks = data.get("content")
        if data.get("type") != "message" or not isinstance(blocks, list):
            return Check("unavailable", "invalid_gateway_response")
        if not any(isinstance(block, dict) and block.get("type") == "tool_use"
                   and block.get("name") == marker for block in blocks):
            return Check("incompatible", "tools_unsupported")
        reported = data.get("model")
        return Check("available", reported_model=reported if valid_model(reported) else None)


async def _policy(session: AsyncSession, *, lock: bool = False) -> AiModelRouting:
    if lock:
        policy = await session.scalar(update(AiModelRouting).where(AiModelRouting.id == 1)
            .values(version=AiModelRouting.version).returning(AiModelRouting)
            .execution_options(populate_existing=True))
    else:
        policy = await session.get(AiModelRouting, 1, populate_existing=True)
    if policy is None:
        raise RuntimeError("Model routing singleton is missing")
    return policy


def _summary(policy: AiModelRouting) -> dict[str, Any]:
    return {key: getattr(policy, key) for key in
            ("primary_model", "reserve_model", "paused", "active_model", "reason")}


async def _health(session: AsyncSession, policy: AiModelRouting) -> dict[str, AiModelHealth]:
    models = (policy.primary_model, policy.reserve_model)
    items = await session.scalars(select(AiModelHealth).where(AiModelHealth.model.in_(models)))
    return {item.model: item for item in items}


def _ready(item: AiModelHealth | None, now: datetime) -> bool:
    return bool(item and item.compatible and item.state == "available" and now - _utc(item.checked_at) <= HEALTH_TTL)


def _evaluate(policy: AiModelRouting, health: dict[str, AiModelHealth], now: datetime) -> tuple[str | None, str | None]:
    if policy.paused:
        return None, "paused"
    for model in (policy.primary_model, policy.reserve_model):
        if model and _ready(health.get(model), now):
            return model, None
    if not health:
        return None, "checking"
    if len(health) == 2 and all(item.reason == "gateway_unreachable" for item in health.values()):
        return None, "gateway_unavailable"
    return None, "models_unavailable"


async def select_execution_model(session: AsyncSession, *, lock: bool = True) -> tuple[str | None, str | None]:
    policy = await _policy(session, lock=lock)
    model, reason = _evaluate(policy, await _health(session, policy), datetime.now(UTC))
    return model, ("ai_launches_paused" if reason == "paused" else "ai_models_unavailable") if reason else None


async def _update_active_model(session: AsyncSession, policy: AiModelRouting, now: datetime) -> None:
    before = _summary(policy)
    policy.active_model, policy.reason = _evaluate(policy, await _health(session, policy), now)
    if before["active_model"] != policy.active_model:
        session.add(AiModelRoutingAudit(action="automatic", before=before, after=_summary(policy)))


class ModelRouting:
    def __init__(self, database: Database, settings: Settings):
        if (settings.executor != "cli" or not settings.anthropic_base_url or not settings.anthropic_auth_token
                or not valid_model(settings.ai_model_primary) or not valid_model(settings.ai_model_reserve)
                or settings.ai_model_primary == settings.ai_model_reserve):
            raise ValueError("AI model routing requires CLI, gateway credentials and two different model IDs")
        self.database = database
        self.gateway = GatewayModels(settings.anthropic_base_url, settings.anthropic_auth_token)
        self.defaults = (settings.ai_model_primary, settings.ai_model_reserve)
        self._wake = asyncio.Event()
        self._check_lock = asyncio.Lock()
        self._catalog_lock = asyncio.Lock()
        self._catalog: tuple[datetime, list[str]] | None = None

    async def initialize(self) -> None:
        async with self.database.session() as session:
            # Startup already holds the gateway lease for this database.
            if await session.get(AiModelRouting, 1) is None:
                session.add(AiModelRouting(id=1, primary_model=self.defaults[0], reserve_model=self.defaults[1]))
                await session.commit()

    async def status(self, session: AsyncSession) -> dict[str, Any]:
        policy = await _policy(session)
        checked = list(await session.scalars(select(AiModelHealth).order_by(AiModelHealth.checked_at.desc(), AiModelHealth.model)))
        now = datetime.now(UTC)
        relevant = {item.model: item for item in checked if item.model in (policy.primary_model, policy.reserve_model)}
        active, reason = _evaluate(policy, relevant, now)
        return {
            **_summary(policy), "active_model": active, "reason": reason, "version": policy.version, "as_of": utc_iso(now),
            "last_checked_at": utc_iso(policy.last_checked_at) if policy.last_checked_at else None,
            "check_interval_seconds": CHECK_INTERVAL,
            "models": [{
                "id": item.model, "state": "stale" if item.state == "available" and not _ready(item, now) else item.state,
                "checked_at": utc_iso(item.checked_at), "reported_model": item.reported_model,
            } for item in checked],
        }

    async def catalog(self) -> list[str]:
        try:
            # Bound the whole lookup, including lock contention and pagination.
            async with asyncio.timeout(REQUEST_TIMEOUT), self._catalog_lock:
                now = datetime.now(UTC)
                if self._catalog is None or now - self._catalog[0] > timedelta(seconds=CHECK_INTERVAL):
                    self._catalog = (now, await self.gateway.catalog())
                return self._catalog[1]
        except (GatewayError, TimeoutError) as error:
            raise ModelRoutingError("model_catalog_unavailable", 503, "Каталог Brotherhood недоступен.") from error

    async def search(self, session: AsyncSession, query: str) -> dict[str, Any]:
        query = query.strip().casefold()
        if not query:
            return {"items": [], "total": 0}
        checked = set(await session.scalars(select(AiModelHealth.model)))
        matches = [id for id in await self.catalog() if query in id.casefold() and id not in checked]
        matches.sort(key=lambda id: (id.casefold() != query, not id.casefold().startswith(query), id))
        return {"items": matches[:8], "total": len(matches)}

    async def _save_checks(
        self, checks: dict[str, Check], *, started_at: datetime, scheduled: bool, actor: str | None = None,
    ) -> None:
        async with self.database.session() as session:
            policy = await _policy(session, lock=True)
            now = datetime.now(UTC)
            skipped = False
            for model, result in checks.items():
                row = await session.get(AiModelHealth, model)
                # A probe started before a failed run was recorded cannot restore its route.
                if row is not None and row.reason == PROVIDER_FAILURE_REASON and _utc(row.checked_at) >= started_at:
                    skipped = True
                    continue
                if row is None:
                    row = AiModelHealth(model=model, compatible=False)
                    session.add(row)
                    if actor:
                        session.add(AiModelRoutingAudit(actor=actor, action="model_checked",
                                                       after={"model": model, "state": result.state}))
                row.checked_at, row.state, row.reason = now, result.state, result.reason
                if result.state == "available":
                    row.compatible = True
                    row.reported_model = result.reported_model or row.reported_model
                elif result.state == "incompatible":
                    row.compatible = False
            if scheduled and not skipped and set(checks) == {policy.primary_model, policy.reserve_model}:
                policy.last_checked_at = now
            await session.flush()
            await _update_active_model(session, policy, now)
            await session.commit()

    async def record_provider_failure(self, session: AsyncSession, model: str) -> None:
        policy = await _policy(session, lock=True)
        now = datetime.now(UTC)
        row = await session.get(AiModelHealth, model)
        if row is None:
            row = AiModelHealth(model=model, compatible=False)
            session.add(row)
        row.checked_at, row.state, row.reason = now, "unavailable", PROVIDER_FAILURE_REASON
        await session.flush()
        await _update_active_model(session, policy, now)

    async def refresh(self) -> None:
        async with self._check_lock:
            async with self.database.session() as session:
                policy = await _policy(session)
                models = [model for model in (policy.primary_model, policy.reserve_model) if model]
            started_at = datetime.now(UTC)
            checks = await asyncio.gather(*(self.gateway.check(model) for model in models))
            await self._save_checks(dict(zip(models, checks)), started_at=started_at, scheduled=True)

    async def verify(self, model: str, actor: str) -> None:
        if model not in await self.catalog():
            raise ModelRoutingError("model_not_in_catalog", 422, "Модель отсутствует в каталоге Brotherhood.")
        async with self._check_lock:
            started_at = datetime.now(UTC)
            result = await self.gateway.check(model)
            await self._save_checks({model: result}, started_at=started_at, scheduled=False, actor=actor)

    def request_check(self) -> None:
        self._wake.set()

    async def watch(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception:
                logger.exception("Could not persist model routing health")
            try:
                await asyncio.wait_for(self._wake.wait(), CHECK_INTERVAL)
            except TimeoutError:
                continue
            self._wake.clear()
            # Coalesce a burst of failed CLI runs into one check.
            await asyncio.sleep(2)
            self._wake.clear()

    async def change(
        self, session: AsyncSession, *, actor: str, version: int,
        primary: str, reserve: str, paused: bool,
    ) -> dict[str, Any]:
        policy = await _policy(session, lock=True)
        if (policy.primary_model, policy.reserve_model, policy.paused) == (primary, reserve, paused):
            return await self.status(session)
        if version != policy.version:
            raise ModelRoutingError("model_routing_conflict", 409, "Настройки уже изменились. Обновите состояние.")
        if primary == reserve:
            raise ModelRoutingError("duplicate_model", 422, "Основная и резервная модели должны различаться.")
        rows = {item.model: item for item in await session.scalars(
            select(AiModelHealth).where(AiModelHealth.model.in_([primary, reserve])))}
        existing = {policy.primary_model, policy.reserve_model}
        now = datetime.now(UTC)
        if any(not _ready(rows.get(id), now) for id in {primary, reserve} - existing):
            raise ModelRoutingError("model_not_ready", 422, "Сначала проверьте доступность новой модели.")
        if (existing != {primary, reserve} and primary in rows and reserve in rows
                and rows[primary].reported_model and rows[primary].reported_model == rows[reserve].reported_model):
            raise ModelRoutingError("duplicate_model", 422, "Оба имени маршрутизируются к одной модели.")
        before = _summary(policy)
        policy.primary_model, policy.reserve_model, policy.paused = primary, reserve, paused
        policy.version += 1
        policy.active_model, policy.reason = _evaluate(policy, rows, now)
        session.add(AiModelRoutingAudit(actor=actor, action="settings", before=before, after=_summary(policy)))
        await session.commit()
        if existing != {primary, reserve}:
            self.request_check()
        return await self.status(session)

    async def history(self, session: AsyncSession, page: int, size: int) -> dict[str, Any]:
        total = await session.scalar(select(func.count()).select_from(AiModelRoutingAudit))
        items = await session.scalars(select(AiModelRoutingAudit)
            .order_by(AiModelRoutingAudit.created_at.desc(), AiModelRoutingAudit.id.desc())
            .offset((page - 1) * size).limit(size))
        return {"items": [{
            "id": item.id, "created_at": utc_iso(item.created_at), "actor": item.actor, "action": item.action,
            "before": item.before, "after": item.after,
        } for item in items], "total": total, "page": page, "page_size": size, "as_of": utc_iso(datetime.now(UTC))}
