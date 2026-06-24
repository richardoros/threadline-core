"""ID utilities — the only place Threadline creates IDs.

Every generated primary key in Threadline is a 32-character uuid4 hex string
from ``new_id()``. Keeping ID creation in one function means the format can
never drift between tables.
"""
import uuid


def new_id() -> str:
    """Return a new random ID: 32 lowercase hex characters (uuid4, no dashes)."""
    return uuid.uuid4().hex
