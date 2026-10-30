"""Durable, idempotent delivery of staged artifact storage receipts."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from backend.repository import (
    ArtifactConfirmationClaim,
    claim_due_artifact_confirmations,
    claim_pending_artifact_abandons,
    finalize_artifact_confirmation_failure,
    mark_artifact_abandon_delivered,
    mark_artifact_confirmation_conflicted,
    mark_artifact_confirmation_confirmed,
    reschedule_artifact_confirmation,
)
from backend.runner_transport import TransportError

if TYPE_CHECKING:
    from backend.config import Settings
    from backend.models import Event
    from backend.db import Database
    from backend.runner_transport import RunnerTransport


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconciliationResult:
    claimed: int
    confirmed: int
    conflicted: int
    rescheduled: int
    conflicted_confirmation_ids: frozenset[str]
    terminal_events: tuple[Event, ...]


def _backoff_delay(attempt: int, maximum: float) -> float:
    """Return a capped exponential retry delay, without jittering durable tests."""
    return min(float(2 ** max(attempt - 1, 0)), maximum)


def _safe_transport_category(error: TransportError) -> str:
    if str(error) == "storage_receipt_conflict":
        return "transport_conflict"
    return "transport"


def _age_seconds(now: datetime, created_at: datetime) -> float:
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return max(0.0, (now - created_at).total_seconds())


async def _deliver_terminal_abandon(
    database: Database,
    transport: RunnerTransport,
    claim: ArtifactConfirmationClaim,
) -> None:
    try:
        await asyncio.to_thread(
            transport.abandon,
            claim.project_id,
            claim.transport_run_id,
            "artifact_confirmation_failed",
        )
    except Exception:
        logger.exception(
            "Artifact confirmation terminal transport abandon failed",
            extra={"confirmation_id": claim.id, "run_id": claim.run_id},
        )
    else:
        async with database.session() as session:
            await mark_artifact_abandon_delivered(session, claim)


async def reconcile_artifact_confirmations(
    database: Database,
    transport: RunnerTransport,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> ReconciliationResult:
    """Claim due confirmations, then perform transport I/O outside database transactions."""
    claimed_at = now or datetime.now(UTC)
    async with database.session() as session:
        claims = await claim_due_artifact_confirmations(
            session,
            now=claimed_at,
            limit=settings.artifact_confirmation_batch_size,
            lease_seconds=settings.artifact_confirmation_lease_seconds,
            max_attempts=settings.artifact_confirmation_max_attempts,
        )

    confirmed = conflicted = rescheduled = 0
    conflicted_confirmation_ids: set[str] = set()
    terminal_events: list[Event] = []
    async with database.session() as session:
        abandon_claims = await claim_pending_artifact_abandons(
            session, limit=settings.artifact_confirmation_batch_size,
        )
    for abandon_claim in abandon_claims:
        await _deliver_terminal_abandon(database, transport, abandon_claim)
    for claim in claims:
        if claim.recovered_exhausted_claim:
            async with database.session() as session:
                changed = await mark_artifact_confirmation_conflicted(
                    session, claim, category="attempts_exhausted",
                )
            conflicted += int(changed)
            if changed:
                conflicted_confirmation_ids.add(claim.id)
                async with database.session() as session:
                    terminal_event = await finalize_artifact_confirmation_failure(session, claim.run_id)
                if terminal_event is not None:
                    terminal_events.append(terminal_event)
                await _deliver_terminal_abandon(database, transport, claim)
            logger.warning(
                "Artifact confirmation stale final claim exhausted",
                extra={"confirmation_id": claim.id, "run_id": claim.run_id,
                       "category": "attempts_exhausted", "attempt": claim.attempts,
                       "age_seconds": _age_seconds(claimed_at, claim.created_at)},
            )
            continue
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    transport.confirm_saved,
                    claim.project_id,
                    claim.transport_run_id,
                    claim.artifact_id,
                    dict(claim.receipt),
                ),
                timeout=settings.artifact_confirmation_timeout_seconds,
            )
        except asyncio.TimeoutError:
            category = "timeout"
        except TransportError as error:
            category = _safe_transport_category(error)
            if category == "transport_conflict":
                async with database.session() as session:
                    changed = await mark_artifact_confirmation_conflicted(
                        session, claim, category=category,
                    )
                conflicted += int(changed)
                if changed:
                    conflicted_confirmation_ids.add(claim.id)
                    async with database.session() as session:
                        terminal_event = await finalize_artifact_confirmation_failure(
                            session, claim.run_id,
                        )
                    if terminal_event is not None:
                        terminal_events.append(terminal_event)
                    await _deliver_terminal_abandon(database, transport, claim)
                logger.warning(
                    "Artifact confirmation conflicted",
                    extra={"confirmation_id": claim.id, "run_id": claim.run_id,
                           "category": category, "attempt": claim.attempts,
                           "age_seconds": _age_seconds(claimed_at, claim.created_at)},
                )
                continue
        except Exception:
            category = "transport"
        else:
            async with database.session() as session:
                changed = await mark_artifact_confirmation_confirmed(session, claim)
            confirmed += int(changed)
            logger.info(
                "Artifact confirmation delivered",
                extra={"confirmation_id": claim.id, "run_id": claim.run_id,
                       "attempt": claim.attempts,
                       "age_seconds": _age_seconds(claimed_at, claim.created_at)},
            )
            continue

        if claim.attempts >= settings.artifact_confirmation_max_attempts:
            async with database.session() as session:
                changed = await mark_artifact_confirmation_conflicted(
                    session, claim, category="attempts_exhausted",
                )
            conflicted += int(changed)
            if changed:
                conflicted_confirmation_ids.add(claim.id)
                async with database.session() as session:
                    terminal_event = await finalize_artifact_confirmation_failure(
                        session, claim.run_id,
                    )
                if terminal_event is not None:
                    terminal_events.append(terminal_event)
                await _deliver_terminal_abandon(database, transport, claim)
            logger.warning(
                "Artifact confirmation attempts exhausted",
                extra={"confirmation_id": claim.id, "run_id": claim.run_id,
                       "category": "attempts_exhausted", "attempt": claim.attempts,
                       "age_seconds": _age_seconds(claimed_at, claim.created_at)},
            )
            continue

        next_attempt_at = claimed_at + timedelta(
            seconds=_backoff_delay(claim.attempts, settings.artifact_confirmation_max_retry_delay_seconds),
        )
        async with database.session() as session:
            changed = await reschedule_artifact_confirmation(
                session, claim, category=category, next_attempt_at=next_attempt_at,
            )
        rescheduled += int(changed)
        logger.info(
            "Artifact confirmation rescheduled",
            extra={"confirmation_id": claim.id, "run_id": claim.run_id,
                   "category": category, "attempt": claim.attempts,
                   "age_seconds": _age_seconds(claimed_at, claim.created_at)},
        )

    return ReconciliationResult(
        claimed=len(claims),
        confirmed=confirmed,
        conflicted=conflicted,
        rescheduled=rescheduled,
        conflicted_confirmation_ids=frozenset(conflicted_confirmation_ids),
        terminal_events=tuple(terminal_events),
    )
