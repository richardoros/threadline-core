"""Time utilities — the only place Threadline creates timestamps.

Every timestamp stored anywhere in Threadline is an ISO-8601 UTC string
produced by ``iso_now()``. Strings sort correctly only if they are all UTC
and all the same format, so never build timestamps by hand and never use
naive ``datetime`` values.
"""
from datetime import datetime, timezone


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def iso_now() -> str:
    """Return the current time as an ISO-8601 UTC string with microseconds.

    This is the canonical timestamp format for every database column and
    event record in Threadline.
    """
    return utc_now().isoformat(timespec="microseconds")


def today_str() -> str:
    """Return today's UTC date as ``YYYY-MM-DD`` (used for daily notes)."""
    return utc_now().date().isoformat()
