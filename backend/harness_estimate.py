"""Strict metadata boundary for diagnostic client-side session cost snapshots."""
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from backend.db import Database
from backend.models import Run


def snapshot(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"session_ref", "observed_at", "journal_seq", "value_usd"}:
        raise ValueError("invalid_harness_snapshot")
    if type(value["journal_seq"]) is not int or value["journal_seq"] < 1:
        raise ValueError("invalid_harness_snapshot")
    if not isinstance(value["value_usd"], str) or not 1 <= len(value["value_usd"]) <= 64:
        raise ValueError("invalid_harness_snapshot")
    try:
        amount = Decimal(value["value_usd"])
        UUID(value["session_ref"])
        observed = datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))
        if not amount.is_finite() or amount < 0 or observed.tzinfo is None:
            raise ValueError
    except (ValueError, TypeError, AttributeError, InvalidOperation):
        raise ValueError("invalid_harness_snapshot") from None
    return {"session_ref": value["session_ref"], "observed_at": observed.astimezone(UTC).isoformat(),
            "journal_seq": value["journal_seq"], "value_usd": str(amount)}


def project_snapshot(value: object, *, event_id: str, previous: dict | None) -> dict:
    safe = snapshot(value)
    if previous and previous.get("session_ref") == safe["session_ref"]:
        old_seq = previous.get("source", {}).get("journal_seq")
        if type(old_seq) is int and old_seq >= safe["journal_seq"]:
            return previous
    return {"state": "known", "value_usd": safe["value_usd"], "scope": "session_total",
            "session_ref": safe["session_ref"], "segment_ref": None, "observed_at": safe["observed_at"],
            "source": {"kind": "harness_result", "field": "total_cost_usd", "event_id": event_id,
                       "journal_seq": safe["journal_seq"], "runner_version": None},
            "baseline": None, "continuity": "unverified", "unavailable_reason": None}


def safe_estimate(value: object) -> dict | None:
    if isinstance(value, dict) and value.get("storage_schema") == "harness_estimate.v1":
        value = value.get("estimate")
    if not isinstance(value, dict) or value.get("scope") != "session_total":
        return None
    try:
        source = value["source"]
        UUID(source["event_id"])
        safe = project_snapshot({"session_ref": value["session_ref"], "observed_at": value["observed_at"],
            "journal_seq": source["journal_seq"], "value_usd": value["value_usd"]},
            event_id=source["event_id"], previous=None)
        version = source.get("runner_version")
        safe["source"]["runner_version"] = version if isinstance(version, str) and len(version) <= 100 else None
        return safe
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def wrapper(observation: dict) -> dict:
    frame = observation["frame"]
    estimate = project_snapshot({**frame["payload"], "journal_seq": observation["source_order"]["journal_seq"]},
                                event_id=frame["source_event_id"], previous=None)
    return {"storage_schema": "harness_estimate.v1", "estimate": estimate,
            "diagnostic_id": frame["diagnostic_id"], "lineage": observation["lineage"],
            "source_order": observation["source_order"]}


def monotonic(previous: object, candidate: dict) -> dict:
    if previous is None:
        return candidate
    if not isinstance(previous, dict) or previous.get("storage_schema") != "harness_estimate.v1":
        raise ValueError("harness_lineage_conflict")
    if previous.get("lineage") != candidate["lineage"]:
        raise ValueError("harness_lineage_conflict")
    old, new = previous["source_order"]["public_seq"], candidate["source_order"]["public_seq"]
    if new < old:
        return previous
    if new == old and previous != candidate:
        raise ValueError("harness_observation_conflict")
    return candidate


async def catch_up(database: Database, run_id: str, observations: list[dict]) -> None:
    """Read transport before calling; no transport lock spans this transaction."""
    if not observations:
        return
    candidates = [wrapper(observation) for observation in observations]

    def advance(previous: object) -> dict:
        current = previous
        for candidate in candidates:
            current = monotonic(current, candidate)
        return current

    # The optimistic check is read-only. A concurrent writer can only advance the
    # projection; any actual write still rechecks fresh state under the Run lock.
    async with database.session() as session:
        row = (await session.execute(select(Run.harness_estimate).where(Run.id == run_id))).first()
        if row is None or advance(row[0]) == row[0]:
            return
    for attempt in range(3):
        try:
            async with database.session() as session:
                if session.bind.dialect.name == "sqlite":
                    await session.execute(text("BEGIN IMMEDIATE"))
                else:
                    await session.execute(text("SET LOCAL lock_timeout = '2s'"))
                # Projection only locks one Run and never subsequently locks its parents.
                run = await session.scalar(select(Run).where(Run.id == run_id).with_for_update()
                                           .execution_options(populate_existing=True))
                if run is None:
                    return
                current = advance(run.harness_estimate)
                if current != run.harness_estimate:
                    run.harness_estimate = current
                    await session.commit()
                return
        except DBAPIError as error:
            code = getattr(error.orig, "sqlstate", None) or getattr(error.orig, "pgcode", None)
            busy = getattr(error.orig, "sqlite_errorcode", None) in {5, 6}
            if attempt == 2 or not (busy or code in {"40P01", "55P03"}):
                raise
