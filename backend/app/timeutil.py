"""Dates are all UTC, but SQLite does not keep the timezone.

A value read back from the database therefore comes out *naive*. Comparing a
stored date with a fresh one raises TypeError, and grouping parts does nothing
else. So we normalize systematically before comparing and before serializing.
"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """Make the date timezone-aware as UTC (naive ones are UTC by convention)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
