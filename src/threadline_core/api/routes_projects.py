"""Project read routes — structured views over the project data.

Routes
------
GET /api/projects
    List every project Threadline knows about.  Returns a lightweight summary
    of each project: key, name, status, and current_objective.  Projects are
    created automatically when the first event for a project_key is ingested,
    so this list grows without any manual registration step.

GET /api/projects/{key}
    Return the full detail view for one project.  Includes the project's own
    fields, live counts of open loops and active decisions, the total number of
    agent sessions, and the most recent session (by started_at).

    Returns 404 if the key is not found.

Count definitions
-----------------
- open_loops: OpenLoop rows where status == "open" (not "resolved").
- active_decisions: Decision rows where status == "active" (not "superseded").
- sessions: total AgentSession rows for this project (all statuses).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from threadline_core.api.deps import get_db
from threadline_core.models import AgentEvent, AgentSession, Decision, OpenLoop, Project

router = APIRouter()


@router.get("/projects")
async def list_projects(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict[str, Any]]:
    """Return a lightweight list of all known projects.

    Each entry contains: key, name, status, current_objective.
    The list is ordered by project key for deterministic output.
    """
    rows = (await db.execute(select(Project).order_by(Project.key))).scalars().all()
    return [
        {
            "key": p.key,
            "name": p.name,
            "status": p.status,
            "current_objective": p.current_objective,
        }
        for p in rows
    ]


@router.get("/projects/{key}")
async def get_project(
    key: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Return full detail for one project, including counts and last session.

    Path parameter
    --------------
    key:
        The project slug (e.g. ``"acme-api"``).

    Response shape
    --------------
    {
        "key": str,
        "name": str,
        "description": str | null,
        "current_objective": str | null,
        "status": str,
        "counts": {
            "open_loops": int,       # status == "open" only
            "active_decisions": int, # status == "active" only
            "sessions": int,         # all sessions regardless of status
            "orphan_ends": int,      # agent_events with event_type == "orphan_end"
        },
        "last_session": {
            "id": str,
            "agent_name": str,
            "started_at": str,
            "ended_at": str | null,
            "summary": str | null,
            "status": str,
        } | null  # null if no sessions exist yet
    }

    Raises 404 if the key is not found.
    """
    project = await db.get(Project, key)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project not found: {key!r}")

    # Count open loops (status == "open" only)
    open_loop_count: int = (
        await db.scalar(
            select(func.count()).select_from(OpenLoop).where(
                OpenLoop.project_key == key,
                OpenLoop.status == "open",
            )
        )
    ) or 0

    # Count active decisions (status == "active" only)
    active_decision_count: int = (
        await db.scalar(
            select(func.count()).select_from(Decision).where(
                Decision.project_key == key,
                Decision.status == "active",
            )
        )
    ) or 0

    # Count all sessions
    session_count: int = (
        await db.scalar(
            select(func.count()).select_from(AgentSession).where(
                AgentSession.project_key == key,
            )
        )
    ) or 0

    # Count orphan-end events — SessionEnds that arrived with no matching session
    # (a hook firing without a recorded start). Surfaced so operators can spot
    # misconfigured or out-of-order session lifecycles.
    orphan_end_count: int = (
        await db.scalar(
            select(func.count()).select_from(AgentEvent).where(
                AgentEvent.project_key == key,
                AgentEvent.event_type == "orphan_end",
            )
        )
    ) or 0

    # Most recent session by started_at
    last_session_row = (
        await db.execute(
            select(AgentSession)
            .where(AgentSession.project_key == key)
            .order_by(AgentSession.started_at.desc())
            .limit(1)
        )
    ).scalars().first()

    last_session = None
    if last_session_row is not None:
        last_session = {
            "id": last_session_row.id,
            "agent_name": last_session_row.agent_name,
            "started_at": last_session_row.started_at,
            "ended_at": last_session_row.ended_at,
            "summary": last_session_row.summary,
            "status": last_session_row.status,
        }

    return {
        "key": project.key,
        "name": project.name,
        "description": project.description,
        "current_objective": project.current_objective,
        "status": project.status,
        "counts": {
            "open_loops": open_loop_count,
            "active_decisions": active_decision_count,
            "sessions": session_count,
            "orphan_ends": orphan_end_count,
        },
        "last_session": last_session,
    }


