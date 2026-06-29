"""Project progress view — "how is this project moving?".

Pure aggregation over rows the store already holds (no new schema, no LLM). It
gives the dashboard and CLI a momentum read alongside the Next Steps Engine:
how much unresolved work remains, how much was closed vs opened in the last
week (net momentum), session health, and store noise (orphan_ends).

All windows are computed against an injectable ``now`` so callers/tests are not
wall-clock dependent.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from threadline_core.models import AgentEvent, AgentSession, OpenLoop, Project
from threadline_core.services.ingest import BLOCKER_PREFIX
from threadline_core.services.loop_classification import classify_loop
from threadline_core.utils.time import iso_now

PROGRESS_WINDOW_DAYS = 7


def is_incomplete_summary(summary: str | None) -> bool:
    """An ended session with no real handoff summary (incl. auto-reaped ones).

    Single source of truth for "is this session a real handoff?" — shared with
    ``project_state.latest_substantive_session`` so the progress count and the
    "last session" selector can never disagree about what counts as substantive.
    """
    return not summary or summary.startswith("(auto-reaped")


@dataclass
class ProjectProgress:
    """A momentum snapshot for one project."""

    project: str
    open_loops: int       # status == "open" — the honest total
    blockers: int         # open loops with the [blocker] prefix
    bookkeeping_loops: int  # open loops hidden from the Next Steps feed (v1.1)
    resolved_7d: int      # loops resolved within the window
    opened_7d: int        # loops created within the window
    active_sessions: int
    ended_sessions: int
    incomplete_sessions: int  # ended without a real handoff summary
    orphan_ends: int      # unmatched SessionEnds (store noise)
    last_activity: str | None

    @property
    def momentum(self) -> int:
        """Net loops closed minus opened in the window (positive = catching up)."""
        return self.resolved_7d - self.opened_7d


async def project_progress(
    db: AsyncSession,
    project_key: str,
    *,
    now: str | None = None,
) -> ProjectProgress:
    """Aggregate a momentum snapshot for ``project_key``.

    Raises LookupError if the project does not exist (callers translate to a 404
    or a CLI error) — a progress view for a non-existent project is a typo, not
    a project with zero progress.
    """
    project = await db.get(Project, project_key)
    if project is None:
        raise LookupError(f"Project not found: {project_key!r}")

    now = now or iso_now()
    cutoff = (datetime.fromisoformat(now) - timedelta(days=PROGRESS_WINDOW_DAYS)).isoformat(
        timespec="microseconds"
    )

    async def _count(stmt) -> int:
        return (await db.scalar(stmt)) or 0

    open_descriptions = (await db.execute(
        select(OpenLoop.description).where(
            OpenLoop.project_key == project_key, OpenLoop.status == "open"
        )
    )).scalars().all()
    open_loops = len(open_descriptions)  # honest total
    # Hidden-from-feed count: classify each open loop's text; the noise classes
    # (approval_gate / status / bookkeeping) are still counted here for audit,
    # they are just not surfaced in "Continue where you stopped".
    bookkeeping_loops = sum(1 for d in open_descriptions if not classify_loop(d).in_feed)
    blockers = await _count(
        select(func.count()).select_from(OpenLoop).where(
            OpenLoop.project_key == project_key,
            OpenLoop.status == "open",
            OpenLoop.description.like(f"{BLOCKER_PREFIX}%"),
        )
    )
    resolved_7d = await _count(
        select(func.count()).select_from(OpenLoop).where(
            OpenLoop.project_key == project_key,
            OpenLoop.status == "resolved",
            OpenLoop.resolved_at >= cutoff,
        )
    )
    opened_7d = await _count(
        select(func.count()).select_from(OpenLoop).where(
            OpenLoop.project_key == project_key,
            OpenLoop.created_at >= cutoff,
        )
    )
    active_sessions = await _count(
        select(func.count()).select_from(AgentSession).where(
            AgentSession.project_key == project_key, AgentSession.status == "active"
        )
    )
    orphan_ends = await _count(
        select(func.count()).select_from(AgentEvent).where(
            AgentEvent.project_key == project_key, AgentEvent.event_type == "orphan_end"
        )
    )
    last_activity = await db.scalar(
        select(func.max(AgentEvent.occurred_at)).where(
            AgentEvent.project_key == project_key
        )
    )

    # Ended sessions split into complete vs incomplete (no real handoff summary).
    ended_rows = (await db.execute(
        select(AgentSession.summary).where(
            AgentSession.project_key == project_key, AgentSession.status == "ended"
        )
    )).scalars().all()
    ended_sessions = len(ended_rows)
    incomplete_sessions = sum(1 for s in ended_rows if is_incomplete_summary(s))

    return ProjectProgress(
        project=project_key,
        open_loops=open_loops,
        blockers=blockers,
        bookkeeping_loops=bookkeeping_loops,
        resolved_7d=resolved_7d,
        opened_7d=opened_7d,
        active_sessions=active_sessions,
        ended_sessions=ended_sessions,
        incomplete_sessions=incomplete_sessions,
        orphan_ends=orphan_ends,
        last_activity=last_activity,
    )
