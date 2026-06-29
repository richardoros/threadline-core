"""Regression: "last session" must be the latest ENDED substantive session.

Ported from the upstream 2026-06-29 continuation fix. Both get_project_state and
the project detail API selected the newest session by started_at regardless of
status, so an empty *active* bootstrap session (opened by the SessionStart hook
but not yet finished) showed up as "the last session". The correct predecessor
is the latest ended session that carries a real handoff summary; when none
exists, it is None — an explicit, honest fallback rather than a misleading row.
"""
import pytest

from threadline_core.models import AgentSession, Project
from threadline_core.services.project_state import (
    get_project_state,
    latest_substantive_session,
)


async def _seed(db, *sessions):
    db.add(Project(key="p", name="p"))
    await db.flush()  # parent row must exist before FK children are inserted
    for s in sessions:
        db.add(s)
    await db.commit()


def _session(sid, *, status, summary, started_at, ended_at=None):
    return AgentSession(
        id=sid, project_key="p", agent_name="claude_code", agent_type="coding_agent",
        status=status, summary=summary, started_at=started_at, ended_at=ended_at,
    )


@pytest.mark.asyncio
async def test_prefers_ended_substantive_over_newer_active_empty(db):
    await _seed(
        db,
        _session("s-sub", status="ended", summary="shipped the BIGINT widening",
                 started_at="2026-06-29T08:00:00.000000+00:00",
                 ended_at="2026-06-29T08:30:00.000000+00:00"),
        # Newer, but an empty ACTIVE bootstrap session — must be ignored.
        _session("s-active", status="active", summary=None,
                 started_at="2026-06-29T09:00:00.000000+00:00"),
    )
    chosen = await latest_substantive_session(db, "p")
    assert chosen is not None and chosen.id == "s-sub"

    state = await get_project_state(db, "p")
    assert state.last_session_id == "s-sub"
    assert state.last_session_summary == "shipped the BIGINT widening"


@pytest.mark.asyncio
async def test_none_when_only_active_or_auto_reaped(db):
    await _seed(
        db,
        _session("s-active", status="active", summary=None,
                 started_at="2026-06-29T09:00:00.000000+00:00"),
        _session("s-reaped", status="ended",
                 summary="(auto-reaped: no SessionEnd received; idle > 6h)",
                 started_at="2026-06-29T08:00:00.000000+00:00"),
    )
    assert await latest_substantive_session(db, "p") is None
    state = await get_project_state(db, "p")
    assert state.last_session_id is None


@pytest.mark.asyncio
async def test_picks_newest_among_several_substantive(db):
    await _seed(
        db,
        _session("older", status="ended", summary="older work",
                 started_at="2026-06-28T08:00:00.000000+00:00"),
        _session("newer", status="ended", summary="newer work",
                 started_at="2026-06-29T08:00:00.000000+00:00"),
    )
    chosen = await latest_substantive_session(db, "p")
    assert chosen is not None and chosen.id == "newer"
