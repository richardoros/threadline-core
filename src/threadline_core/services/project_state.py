"""Raw governed-state retrieval — the public boundary for structured project memory.

This module provides deterministic, unevaluated queries over the project's
durable lifecycle records.  It deliberately contains NO synthesis, NO ranking,
NO LLM calls, and NO private-product imports.  The private Threadline product
wraps this module and adds compiled memory, continuation selection, and ranked
next actions on top.

Public guarantee
----------------
The shape of ``ProjectState`` and the semantics of ``get_project_state`` are
part of the ``threadline-core`` public contract.  Additions are backwards
compatible; fields are never removed without a version bump.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from threadline_core.models import (
    AgentEvent,
    AgentSession,
    Decision,
    OpenLoop,
    Project,
    ProjectMemory,
)
from threadline_core.protocol import EventType
from threadline_core.services.findings import (
    _SEVERITY_RANK,
    CRITICAL_GAP_FLOOR,
    FINDING_KIND,
)
from threadline_core.services.progress import is_incomplete_summary
from threadline_core.utils.time import iso_now


async def latest_substantive_session(
    db: AsyncSession, project_key: str
) -> AgentSession | None:
    """The latest ENDED session that carries a real handoff summary, or None.

    "The last session" should be the work an agent would resume — not an empty
    *active* bootstrap session (opened by the SessionStart hook but not yet
    finished) nor an auto-reaped one. We scan ended sessions newest-first and
    return the first substantive one; None is an explicit, honest fallback when
    no completed, summarized session exists yet. Shared by ``get_project_state``
    and the project detail API so both surfaces agree.
    """
    ended_newest_first = (await db.execute(
        select(AgentSession)
        .where(
            AgentSession.project_key == project_key,
            AgentSession.status == "ended",
        )
        .order_by(AgentSession.started_at.desc())
    )).scalars().all()
    return next(
        (s for s in ended_newest_first if not is_incomplete_summary(s.summary)), None
    )

# How many recent verification summaries to surface in project state.
_RECENT_VERIFICATIONS = 5
# How many decisions to surface (newest first).
_MAX_DECISIONS = 20
# How many open loops to surface (oldest first — longest-waiting first).
_MAX_OPEN_LOOPS = 50


@dataclass
class ProjectState:
    """Unevaluated structured state for one project.

    Every field reflects durable records in the database exactly as stored —
    no synthesis, no ranking, no LLM output.
    """

    project_key: str
    objective: str | None
    created_at: str

    # Sessions
    last_session_id: str | None
    last_session_started: str | None
    last_session_ended: str | None
    last_session_summary: str | None

    # Lifecycle records — raw, ordered as documented on each field
    open_loops: list[dict[str, Any]] = field(default_factory=list)
    """Open loops, oldest-first (longest-waiting work first)."""

    decisions: list[dict[str, Any]] = field(default_factory=list)
    """Active decisions, newest-first."""

    known_traps: list[dict[str, Any]] = field(default_factory=list)
    """Decisions marked incorrect/reverted, with corrected_rule where recorded."""

    confirmed_gaps: list[dict[str, Any]] = field(default_factory=list)
    """Confirmed gap findings at or above critical floor (high/critical)."""

    confirmed_caveats: list[dict[str, Any]] = field(default_factory=list)
    """All confirmed caveat findings."""

    evidence_ids: list[str] = field(default_factory=list)
    """IDs of recent evidence-bearing agent events (verification_result type)."""

    recent_verifications: list[str] = field(default_factory=list)
    """Summaries of recent verification_result events, newest-first."""

    retrieved_at: str = field(default_factory=iso_now)


def _loop_row(loop: OpenLoop) -> dict[str, Any]:
    return {
        "id": loop.id,
        "description": loop.description,
        "project_key": loop.project_key,
        "status": loop.status,
        "created_at": loop.created_at,
        "updated_at": loop.updated_at,
    }


def _decision_row(d: Decision) -> dict[str, Any]:
    return {
        "id": d.id,
        "project_key": d.project_key,
        "statement": d.statement,
        "rationale": d.rationale,
        "status": d.status,
        "created_at": d.created_at,
    }


def _finding_row(mem: ProjectMemory) -> dict[str, Any]:
    """Parse a confirmed finding ProjectMemory row into a plain dict."""
    import json as _json
    try:
        body = _json.loads(mem.content or "{}")
    except Exception:
        body = {}
    return {
        "id": body.get("finding_id", mem.id),
        "finding_class": body.get("finding_class"),
        "category": body.get("category"),
        "statement": body.get("statement"),
        "severity": body.get("severity"),
        "impact": body.get("impact"),
        "resolution_condition": body.get("resolution_condition"),
        "status": body.get("status"),
        "created_at": mem.created_at,
    }


async def get_project_state(db: AsyncSession, project_key: str) -> ProjectState:
    """Return the raw governed state for ``project_key``.

    Raises
    ------
    LookupError
        If the project does not exist.

    Returns
    -------
    ProjectState
        A snapshot of all durable lifecycle records for the project, ordered
        as documented on each field.  No synthesis or ranking is applied.
    """
    project = await db.get(Project, project_key)
    if project is None:
        raise LookupError(f"Project not found: {project_key!r}")

    # Latest ENDED substantive session — not an empty active bootstrap session.
    last_session = await latest_substantive_session(db, project_key)

    # Open loops — oldest-first (longest-waiting work floats up)
    open_loops = list((await db.execute(
        select(OpenLoop)
        .where(OpenLoop.project_key == project_key, OpenLoop.status == "open")
        .order_by(OpenLoop.created_at.asc())
        .limit(_MAX_OPEN_LOOPS)
    )).scalars().all())

    # Active decisions — newest-first
    decisions = list((await db.execute(
        select(Decision)
        .where(
            Decision.project_key == project_key,
            Decision.status.in_(["active", "accepted", "validated"]),
        )
        .order_by(Decision.created_at.desc())
        .limit(_MAX_DECISIONS)
    )).scalars().all())

    # Known traps — decisions proven wrong
    traps = list((await db.execute(
        select(Decision)
        .where(
            Decision.project_key == project_key,
            Decision.status.in_(["incorrect", "reverted"]),
        )
        .order_by(Decision.created_at.desc())
    )).scalars().all())

    # Confirmed findings
    confirmed_findings = list((await db.execute(
        select(ProjectMemory)
        .where(
            ProjectMemory.project_key == project_key,
            ProjectMemory.kind == FINDING_KIND,
            ProjectMemory.status == "active",
        )
        .order_by(ProjectMemory.created_at.desc())
    )).scalars().all())

    # Split into gaps and caveats; apply floor to gaps
    gaps: list[dict[str, Any]] = []
    caveats: list[dict[str, Any]] = []
    for mem in confirmed_findings:
        row = _finding_row(mem)
        if row.get("status") != "confirmed":
            continue
        fc = row.get("finding_class")
        sev = row.get("severity", "low")
        if fc == "gap" and sev in CRITICAL_GAP_FLOOR:
            gaps.append(row)
        elif fc == "caveat":
            caveats.append(row)

    gaps.sort(key=lambda r: _SEVERITY_RANK.get(r.get("severity", "low"), 0), reverse=True)

    # Recent verification events
    ver_events = list((await db.execute(
        select(AgentEvent)
        .where(
            AgentEvent.project_key == project_key,
            AgentEvent.event_type == EventType.verification_result.value,
        )
        .order_by(AgentEvent.occurred_at.desc())
        .limit(_RECENT_VERIFICATIONS)
    )).scalars().all())

    return ProjectState(
        project_key=project_key,
        objective=project.current_objective,
        created_at=project.created_at,
        last_session_id=last_session.id if last_session else None,
        last_session_started=last_session.started_at if last_session else None,
        last_session_ended=last_session.ended_at if last_session else None,
        last_session_summary=last_session.summary if last_session else None,
        open_loops=[_loop_row(lp) for lp in open_loops],
        decisions=[_decision_row(d) for d in decisions],
        known_traps=[_decision_row(t) for t in traps],
        confirmed_gaps=gaps,
        confirmed_caveats=caveats,
        evidence_ids=[e.id for e in ver_events],
        recent_verifications=[e.summary for e in ver_events],
    )
