"""Reap stale agent sessions whose SessionEnd never arrived.

SessionEnd hooks are best-effort: a crash, ``kill -9``, or a reboot means the
hook never fires, so an ``AgentSession`` is left stuck at ``status="active"``
forever. That corrupts every "sessions" / "active now" view and inflates
session counts. This module sweeps active sessions whose *last activity* is
older than a threshold and marks them ended, so reads stay honest.

It is deliberately conservative:
- "Last activity" is the newest ``agent_events.occurred_at`` for the session,
  falling back to ``started_at`` when the session logged no events — so a
  long-but-active session that keeps reporting is never reaped.
- An already-ended session is never touched, and an existing summary is never
  clobbered (the auto-reap marker is only written when summary is empty), so no
  real handoff text is lost.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from threadline_core.models import AgentEvent, AgentSession
from threadline_core.utils.time import iso_now


async def reap_stale_sessions(
    db: AsyncSession,
    *,
    max_idle_seconds: int,
    now: str | None = None,
    dry_run: bool = False,
) -> list[str]:
    """Mark active sessions idle longer than ``max_idle_seconds`` as ended.

    Args:
        db: Open async session; the caller owns the transaction (commit/rollback).
        max_idle_seconds: Idle budget. A session whose last activity is older
            than ``now - max_idle_seconds`` is reaped.
        now: ISO-8601 UTC "current time"; defaults to :func:`iso_now`. Injectable
            so tests are not wall-clock dependent.
        dry_run: When True, identify and return the ids that *would* be reaped
            without mutating anything (the safe lever for the live DB).

    Returns:
        The ids of the sessions reaped (or, under ``dry_run``, the candidates).
    """
    now = now or iso_now()
    cutoff_dt = datetime.fromisoformat(now) - timedelta(seconds=max_idle_seconds)

    # Newest event time per session (NULL when the session logged no events).
    last_event = (
        select(
            AgentEvent.session_id.label("sid"),
            func.max(AgentEvent.occurred_at).label("last_at"),
        )
        .group_by(AgentEvent.session_id)
        .subquery()
    )
    result = await db.execute(
        select(AgentSession, last_event.c.last_at)
        .outerjoin(last_event, last_event.c.sid == AgentSession.id)
        .where(AgentSession.status == "active")
    )

    reaped: list[str] = []
    for session, last_at in result.all():
        # Fall back to started_at so a session that never logged an event is
        # still judged by *some* timestamp rather than skipped entirely.
        last_activity = last_at or session.started_at
        if datetime.fromisoformat(last_activity) >= cutoff_dt:
            continue  # still within the idle budget — leave it active

        reaped.append(session.id)
        if dry_run:
            continue

        session.status = "ended"
        session.ended_at = now
        # Only stamp a reason when there is no real summary to lose.
        if not session.summary:
            hours = max_idle_seconds // 3600
            session.summary = (
                f"(auto-reaped: no SessionEnd received; idle > {hours}h)"
            )

    if reaped and not dry_run:
        await db.flush()
    return reaped


async def backfill_orphan_ends(
    db: AsyncSession,
    *,
    dry_run: bool = False,
) -> list[str]:
    """Relabel pre-#9 historical orphans: ``session_ended`` rows with no session.

    Before PR #9 introduced the orphan-end downgrade, an unmatched
    ``session_ended`` event was stored verbatim — ``event_type="session_ended"``
    with ``session_id=NULL`` — instead of being recorded as the ``"orphan_end"``
    diagnostic the current ingest path produces. This is a one-shot maintenance
    sweep that brings those legacy rows in line with the post-#9 convention so
    orphan-event queries and genuine session-ended counts stop disagreeing.

    The predicate is definitionally safe: in the current ingest path a *matched*
    ``session_ended`` always carries the matched ``session_id``, and an unmatched
    one is already written as ``orphan_end``. So ``event_type="session_ended"``
    **and** ``session_id IS NULL`` can only be a pre-#9 orphan. Other NULL-session
    rows (e.g. a checkpoint reported against a foreign session) are left alone.

    It is label-only and conservative:
    - never touches a session, never deletes a row;
    - only rewrites the diagnostic ``event_type`` string;
    - the JSONL evidence log still records the original ``session_ended`` signal,
      so the raw report is preserved.

    Idempotent: after one run the predicate matches nothing, so re-running is a
    no-op. The caller owns the transaction (commit/rollback), mirroring
    :func:`reap_stale_sessions`.

    Args:
        db: Open async session; the caller commits.
        dry_run: When True, return the ids that *would* be relabeled without
            mutating anything (the safe lever for the live DB).

    Returns:
        The ids of the relabeled events (or, under ``dry_run``, the candidates).
    """
    result = await db.execute(
        select(AgentEvent).where(
            AgentEvent.event_type == "session_ended",
            AgentEvent.session_id.is_(None),
        )
    )
    orphans = result.scalars().all()

    relabeled = [event.id for event in orphans]
    if dry_run:
        return relabeled

    for event in orphans:
        event.event_type = "orphan_end"
    if relabeled:
        await db.flush()
    return relabeled
