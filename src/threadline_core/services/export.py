"""Raw governed-state export for threadline-core.

Exports the durable lifecycle records for every project as JSON and Markdown
files.  Only transparent, durable records are included — no synthesis, no
compiled memory, no LLM output, no ranking.

Export manifest metadata is written alongside every export so consumers know
exactly what the file contains:

    {
      "export_mode": "governed_state",
      "compiled_memory_included": false,
      "continuation_included": false
    }
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from threadline_core.models import AgentEvent, AgentSession, DailyNote, Project
from threadline_core.services.project_state import get_project_state
from threadline_core.utils.time import iso_now

# Written into every export file so consumers know what is and is not present.
EXPORT_MANIFEST = {
    "export_mode": "governed_state",
    "compiled_memory_included": False,
    "continuation_included": False,
    "ranked_next_steps_included": False,
    "morning_pulse_included": False,
    "research_recommendations_included": False,
}


@dataclass
class ExportReport:
    """Summary of a completed export run."""

    root: Path
    files_written: int
    projects_exported: int
    daily_notes_exported: int


def _write(path: Path, content: str, report: list[int]) -> None:
    """Write ``content`` to ``path``, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    report[0] += 1


def _json_dump(obj: object) -> str:
    return json.dumps(obj, indent=2, default=str) + "\n"


async def _export_project(
    db: AsyncSession,
    project: Project,
    root: Path,
    now: str,
    report: list[int],
) -> None:
    """Export one project's governed state to ``root/projects/<key>/``."""
    key = project.key
    proj_dir = root / "projects" / key
    state = await get_project_state(db, key)

    # --- manifest ---------------------------------------------------------- #
    manifest_payload = {
        **EXPORT_MANIFEST,
        "project_key": key,
        "exported_at": now,
    }
    _write(proj_dir / "manifest.json", _json_dump(manifest_payload), report)

    # --- project identity -------------------------------------------------- #
    identity = {
        "project_key": key,
        "objective": state.objective,
        "created_at": state.created_at,
        "exported_at": now,
    }
    _write(proj_dir / "identity.json", _json_dump(identity), report)

    # --- sessions ---------------------------------------------------------- #
    sessions = list((await db.execute(
        select(AgentSession)
        .where(AgentSession.project_key == key)
        .order_by(AgentSession.started_at.desc())
        .limit(100)
    )).scalars().all())

    sessions_payload = [
        {
            "id": s.id,
            "project_key": s.project_key,
            "started_at": s.started_at,
            "ended_at": s.ended_at,
            "summary": s.summary,
        }
        for s in sessions
    ]
    _write(proj_dir / "sessions.json", _json_dump(sessions_payload), report)

    # --- recent events (last 200, sanitized — no raw transcript) ----------- #
    events = list((await db.execute(
        select(AgentEvent)
        .where(AgentEvent.project_key == key)
        .order_by(AgentEvent.occurred_at.desc())
        .limit(200)
    )).scalars().all())

    events_payload = [
        {
            "id": e.id,
            "event_type": e.event_type,
            "summary": e.summary,
            "occurred_at": e.occurred_at,
        }
        for e in events
    ]
    _write(proj_dir / "events.json", _json_dump(events_payload), report)

    # --- open loops -------------------------------------------------------- #
    _write(proj_dir / "open_loops.json", _json_dump(state.open_loops), report)

    # --- decisions --------------------------------------------------------- #
    decisions_payload = {
        "active": state.decisions,
        "known_traps": state.known_traps,
    }
    _write(proj_dir / "decisions.json", _json_dump(decisions_payload), report)

    # --- findings ---------------------------------------------------------- #
    findings_payload = {
        "confirmed_gaps": state.confirmed_gaps,
        "confirmed_caveats": state.confirmed_caveats,
    }
    _write(proj_dir / "findings.json", _json_dump(findings_payload), report)

    # --- evidence IDs ------------------------------------------------------ #
    _write(
        proj_dir / "evidence_ids.json",
        _json_dump({"evidence_ids": state.evidence_ids}),
        report,
    )

    # --- human-readable summary (Markdown) --------------------------------- #
    md_lines = [
        f"# {key}",
        "",
        f"> Exported: {now}  ",
        "> Export mode: governed_state (no synthesis, no compiled memory)",
        "",
        f"**Objective:** {state.objective or '_not set_'}",
        "",
        f"## Open loops ({len(state.open_loops)})",
        "",
    ]
    for loop in state.open_loops:
        md_lines.append(f"- [{loop.get('id', '')}] {loop.get('description', '')}")
    md_lines += [
        "",
        f"## Active decisions ({len(state.decisions)})",
        "",
    ]
    for d in state.decisions:
        md_lines.append(f"- [{d.get('id', '')}] {d.get('statement', '')}")
    md_lines += [
        "",
        f"## Known traps ({len(state.known_traps)})",
        "",
    ]
    for t in state.known_traps:
        md_lines.append(f"- [{t.get('id', '')}] {t.get('statement', '')}")
    md_lines += [
        "",
        f"## Confirmed gaps ({len(state.confirmed_gaps)})",
        "",
    ]
    for g in state.confirmed_gaps:
        md_lines.append(
            f"- [{g.get('severity', '')}] {g.get('statement', '')}"
        )
    md_lines += [
        "",
        f"## Confirmed caveats ({len(state.confirmed_caveats)})",
        "",
    ]
    for c in state.confirmed_caveats:
        md_lines.append(f"- {c.get('statement', '')}")

    _write(proj_dir / "summary.md", "\n".join(md_lines) + "\n", report)


async def _export_daily_notes(
    db: AsyncSession,
    root: Path,
    report: list[int],
) -> int:
    """Export all daily notes to ``root/daily/``."""
    notes = list((await db.execute(
        select(DailyNote).order_by(DailyNote.date.asc())
    )).scalars().all())

    for note in notes:
        content = note.markdown or ""
        path = root / "daily" / f"{note.date}.md"
        _write(path, content, report)

    return len(notes)


async def export_all(
    db: AsyncSession,
    root: Path,
) -> ExportReport:
    """Export all projects' governed state to ``root``.

    Creates the following layout::

        root/
          index.json          — export metadata and project list
          projects/
            <key>/
              manifest.json   — what is and is not in this export
              identity.json   — project key, objective, created_at
              sessions.json   — sessions (last 100)
              events.json     — recent events, summaries only (last 200)
              open_loops.json — all open loops
              decisions.json  — active decisions + known traps
              findings.json   — confirmed gaps and caveats
              evidence_ids.json
              summary.md      — human-readable overview
          daily/
            YYYY-MM-DD.md     — daily notes
    """
    now = iso_now()
    write_count = [0]

    projects = list((await db.execute(
        select(Project).order_by(Project.key)
    )).scalars().all())

    for project in projects:
        await _export_project(db, project, root, now, write_count)

    daily_count = await _export_daily_notes(db, root, write_count)

    index = {
        **EXPORT_MANIFEST,
        "exported_at": now,
        "projects": [p.key for p in projects],
    }
    _write(root / "index.json", _json_dump(index), write_count)

    return ExportReport(
        root=root,
        files_written=write_count[0],
        projects_exported=len(projects),
        daily_notes_exported=daily_count,
    )
