"""Small date helpers shared by persistence and API models."""

from __future__ import annotations

from datetime import UTC, datetime


def to_utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utc_now_iso() -> str:
    return to_utc_iso(datetime.now(UTC))
