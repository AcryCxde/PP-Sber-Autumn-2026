"""UTC timestamps at JSON boundaries, including SQLite's naive datetimes."""
from datetime import UTC, datetime


def utc_iso(value: datetime) -> str:
    moment = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return moment.isoformat()
