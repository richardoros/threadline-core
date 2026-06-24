"""Daily note generator — turns a day's agent events into a human/agent-readable note.

WHY THIS MODULE EXISTS
----------------------
At end-of-day (or on demand), Threadline aggregates everything that happened
across all projects into a single ``DailyNote`` row. The note has two formats:

- ``markdown``: for humans reading Obsidian or any markdown viewer. Includes
  Obsidian wikilinks (``[[project-{key}]]``) next to each project heading.
- ``html``: a semantic HTML fragment for dashboards and API consumers. Never a
  full page — no ``<html>`` or ``<head>`` tags.

The note is also indexed in ``memory_fts`` so agents can search for past
activity across dates. On regeneration the old FTS row is deleted first to
avoid duplicate search results — see ``services/fts.py:fts_delete_for``.

PUBLIC API
----------
``generate_daily_note(db, date_str)`` — upsert + return the DailyNote for
``date_str`` (ISO format "YYYY-MM-DD"). Call at any time; the second call
overwrites the first.

``NO_ACTIVITY_TEXT`` — the canonical string used when there are no events.
Tests can import and assert on this constant without hard-coding the string.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from threadline_core.models import AgentEvent, DailyNote, Decision, OpenLoop
from threadline_core.protocol import EventType
from threadline_core.rendering import append_md_section, get_env
from threadline_core.services.fts import FTS_TITLE_MAX, fts_delete_for, fts_insert
from threadline_core.services.ingest import BLOCKER_PREFIX
from threadline_core.utils.time import iso_now

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NO_ACTIVITY_TEXT = "No agent activity recorded."

# FTS kind value for daily note rows
_FTS_KIND = "daily_note"

# EventType values that go into the "sessions" section
_SESSION_TYPES = {EventType.session_started.value, EventType.session_ended.value}

# EventType values that go into the "verification" section
_VERIFICATION_TYPES = {EventType.verification_result.value}

# EventType values that go into the "blockers" section
_BLOCKER_TYPES = {EventType.blocker.value}

# EventType values that are "other activity" (everything not handled above or
# by the structured rows)
_OTHER_TYPES = {
    EventType.checkpoint.value,
    EventType.file_change_summary.value,
    EventType.handoff_requested.value,
    EventType.handoff_generated.value,
    EventType.research_request.value,
}


# ---------------------------------------------------------------------------
# Per-project summary dataclass
# ---------------------------------------------------------------------------


@dataclass
class _ProjectSummary:
    """All note-worthy items for one project on one date.

    Each list holds plain-text strings ready for rendering. The dataclass
    keeps the per-project data typed and explicit instead of relying on an
    untyped dict.
    """

    key: str
    sessions: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    open_loops_added: list[str] = field(default_factory=list)
    open_loops_resolved: list[str] = field(default_factory=list)
    verification: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    other_activity: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        """Return True if no section has any content."""
        return not any([
            self.sessions,
            self.decisions,
            self.open_loops_added,
            self.open_loops_resolved,
            self.verification,
            self.blockers,
            self.other_activity,
        ])


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def generate_daily_note(db: AsyncSession, date_str: str) -> DailyNote:
    """Generate (or regenerate) the DailyNote for ``date_str``.

    Steps:
    1. Validate ``date_str`` is a canonical "YYYY-MM-DD" date
    2. Fetch all AgentEvents whose ``occurred_at`` starts with ``date_str``
       (prefix match works because timestamps are ISO UTC strings and SQLite
       LIKE on string columns does a simple left-anchored substring test)
    3. For each project, build a ``_ProjectSummary`` using the event rows plus
       derived Decision and OpenLoop rows; OpenLoop rows *resolved* on this
       date are included independently of events (a day can have resolutions
       and no events)
    4. Render markdown and HTML from the summaries
    5. Upsert the DailyNote row (insert on first call, update on subsequent)
    6. Delete any stale FTS row for this note, then insert a fresh one
    7. Commit and return

    Contract:
    - ``date_str`` must be canonical "YYYY-MM-DD"; anything else raises
      ValueError before any query runs.
    - Bucketing is by UTC date: ingest normalizes ``occurred_at`` to UTC
      (see ``ingest.py:_build_event_row``), so an event reported at
      2026-06-11T00:30+02:00 lands in the 2026-06-10 note.
    - Always commits before returning.
    """
    # Step 1 — validate before building a LIKE pattern. fromisoformat raises
    # ValueError on garbage (including SQL wildcards % and _); the round-trip
    # check additionally rejects non-canonical spellings fromisoformat would
    # accept (e.g. "20260610"), which would silently match zero rows.
    if date.fromisoformat(date_str).isoformat() != date_str:
        raise ValueError(f"date_str must be canonical YYYY-MM-DD, got {date_str!r}")

    # Step 2 — fetch the day's events
    # NOTE: ISO string prefix matching trick — "2026-06-10" is a valid prefix
    # for any datetime string on that day (e.g. "2026-06-10T09:00:00+00:00").
    # All occurred_at values are stored on the UTC basis, so this bucketing is
    # by UTC calendar date. The validation above guarantees the pattern
    # contains no SQL wildcard characters (%, _).
    events = (await db.execute(
        select(AgentEvent)
        .where(AgentEvent.occurred_at.like(f"{date_str}%"))
        .order_by(AgentEvent.project_key, AgentEvent.occurred_at)
    )).scalars().all()

    # Step 3 — group events by project and build summaries (also picks up
    # loops resolved on this date, independently of events)
    project_summaries = await _build_project_summaries(db, events, date_str)

    # Step 4 — render both formats
    markdown = _render_markdown(date_str, project_summaries)
    html = _render_html(date_str, project_summaries)

    # Step 5 — upsert DailyNote
    note = await _upsert_daily_note(db, date_str, markdown, html)

    # Step 6 — re-index in FTS (delete stale row first to prevent duplicates)
    await fts_delete_for(db, kind=_FTS_KIND, ref_id=note.id)
    await fts_insert(
        db,
        title=f"Daily note {date_str}"[:FTS_TITLE_MAX],
        body=markdown,
        kind=_FTS_KIND,
        ref_id=note.id,
        project_key="",  # daily notes span all projects; no single project_key
    )

    await db.commit()
    return note


# ---------------------------------------------------------------------------
# Data assembly helpers
# ---------------------------------------------------------------------------


async def _build_project_summaries(
    db: AsyncSession,
    events: list[AgentEvent],
    date_str: str,
) -> list[_ProjectSummary]:
    """Build one _ProjectSummary per project from the day's events and resolutions.

    Groups events by project_key (preserving sorted order since the query
    ORDER BY project_key). Also joins in Decision and OpenLoop rows derived
    from this day's events.

    Resolved loops are queried INDEPENDENTLY of events: a day can have zero
    events but still have loops resolved on it (e.g. a human closes a loop
    via the API). Such a day must produce a real note, so the eventless early
    return only happens when there are no resolutions either.

    Returns a list sorted by project_key. Empty list only when the day has
    neither events nor resolutions.
    """
    # Fetch OpenLoop rows *resolved* on this date (resolved_at prefix match).
    # Queried FIRST and regardless of events — resolutions alone make a day
    # noteworthy. They can come from any project, even ones with no events.
    loops_resolved_today = (await db.execute(
        select(OpenLoop).where(OpenLoop.resolved_at.like(f"{date_str}%"))
    )).scalars().all()

    if not events and not loops_resolved_today:
        return []

    event_ids = [e.id for e in events]

    # Fetch Decision rows whose source_event_id is among today's events
    decisions_by_event: dict[str, list[Decision]] = {}
    decision_rows = (await db.execute(
        select(Decision).where(Decision.source_event_id.in_(event_ids))
    )).scalars().all()
    for d in decision_rows:
        decisions_by_event.setdefault(d.source_event_id, []).append(d)

    # Fetch OpenLoop rows *added* via today's events (source_event_id in event_ids)
    loops_added_by_event: dict[str, list[OpenLoop]] = {}
    loops_added = (await db.execute(
        select(OpenLoop).where(OpenLoop.source_event_id.in_(event_ids))
    )).scalars().all()
    for ol in loops_added:
        loops_added_by_event.setdefault(ol.source_event_id, []).append(ol)

    # Build resolved loops index per project_key
    resolved_by_project: dict[str, list[str]] = {}
    for ol in loops_resolved_today:
        resolved_by_project.setdefault(ol.project_key, []).append(ol.description)

    # Group events by project
    by_project: dict[str, list[AgentEvent]] = {}
    for ev in events:
        by_project.setdefault(ev.project_key, []).append(ev)

    # Collect all project keys (events + any projects with resolved loops today)
    all_keys = sorted(set(by_project.keys()) | set(resolved_by_project.keys()))

    summaries: list[_ProjectSummary] = []
    for key in all_keys:
        ps = _ProjectSummary(key=key)

        for ev in by_project.get(key, []):
            _classify_event(ps, ev)
            # Append decisions derived from this event
            for d in decisions_by_event.get(ev.id, []):
                ps.decisions.append(d.statement)
            # Append open loops added via this event. Blocker-derived loops
            # (BLOCKER_PREFIX) are excluded: the blocker already surfaces
            # under the Blockers section via the event summary, and listing
            # the prefixed row here too would show the same text twice. The
            # OpenLoop row itself remains the tracking artefact — it still
            # exists, is FTS-indexed, and shows up under "Open loops
            # resolved" when closed.
            for ol in loops_added_by_event.get(ev.id, []):
                if ol.description.startswith(BLOCKER_PREFIX):
                    continue
                ps.open_loops_added.append(ol.description)

        # Resolved loops for this project (from any date, resolved today)
        ps.open_loops_resolved.extend(resolved_by_project.get(key, []))

        if not ps.is_empty():
            summaries.append(ps)

    return summaries


def _classify_event(ps: _ProjectSummary, ev: AgentEvent) -> None:
    """Route one event's summary into the correct section of a _ProjectSummary.

    Decision and open_loop event types are *intentionally excluded* from
    direct classification here: their structured rows (Decision / OpenLoop)
    are the canonical representation. Including both the raw event summary
    AND the derived row would produce duplicates in the note.

    Blocker events are classified here (into ``ps.blockers``) because the
    event summary is the human-readable text; the [blocker]-prefixed OpenLoop
    row is the *tracking* artefact, not the display text.
    """
    etype = ev.event_type
    summary = ev.summary

    if etype in _SESSION_TYPES:
        ps.sessions.append(summary)
    elif etype in _BLOCKER_TYPES:
        ps.blockers.append(summary)
    elif etype in _VERIFICATION_TYPES:
        ps.verification.append(summary)
    elif etype in _OTHER_TYPES:
        ps.other_activity.append(summary)
    # decision / open_loop event types are skipped here — see docstring.


# ---------------------------------------------------------------------------
# Markdown rendering helpers
# ---------------------------------------------------------------------------


def _render_markdown(date_str: str, summaries: list[_ProjectSummary]) -> str:
    """Assemble the full markdown string for the daily note.

    Format:
    ```
    # Daily Note — YYYY-MM-DD

    ## project-key [[project-project-key]]

    ### Sessions
    - ...

    ### Decisions
    - ...
    ...
    ```

    Returns the NO_ACTIVITY_TEXT constant (inside a minimal heading block)
    when there are no summaries.
    """
    lines: list[str] = [f"# Daily Note — {date_str}", ""]

    if not summaries:
        lines.append(NO_ACTIVITY_TEXT)
        return "\n".join(lines)

    for ps in summaries:
        # Obsidian wikilink next to the project heading — lets vault users
        # navigate straight to the project page from any daily note.
        lines.append(f"## {ps.key} [[project-{ps.key}]]")
        lines.append("")
        # level=3: daily-note sections nest under per-project "##" headings.
        append_md_section(lines, "Sessions", ps.sessions, level=3)
        append_md_section(lines, "Decisions", ps.decisions, level=3)
        append_md_section(lines, "Open loops added", ps.open_loops_added, level=3)
        append_md_section(lines, "Open loops resolved", ps.open_loops_resolved, level=3)
        append_md_section(lines, "Verification", ps.verification, level=3)
        append_md_section(lines, "Blockers", ps.blockers, level=3)
        append_md_section(lines, "Activity", ps.other_activity, level=3)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML rendering helpers
# ---------------------------------------------------------------------------


def _render_html(date_str: str, summaries: list[_ProjectSummary]) -> str:
    """Render the HTML fragment for the daily note via Jinja2.

    Returns a ``<article data-type="daily-note">`` fragment — NOT a full HTML
    page. The template lives at templates/fragments/daily_note.html.

    The Jinja2 environment has ``autoescape=True``, so any agent-supplied
    summary text is automatically HTML-escaped before it reaches the output.
    This protects against XSS from summaries that contain ``<``, ``>``, or
    ``&`` characters.
    """
    env = get_env()
    tmpl = env.get_template("fragments/daily_note.html")

    # Convert _ProjectSummary dataclasses to plain dicts for the template.
    # Jinja2 can access dataclass attributes directly, but dicts are simpler
    # to test against and avoid coupling the template to the Python class.
    proj_dicts = [
        {
            "key": ps.key,
            "sessions": ps.sessions,
            "decisions": ps.decisions,
            "open_loops_added": ps.open_loops_added,
            "open_loops_resolved": ps.open_loops_resolved,
            "verification": ps.verification,
            "blockers": ps.blockers,
            "other_activity": ps.other_activity,
        }
        for ps in summaries
    ]

    return tmpl.render(
        date=date_str,
        projects=proj_dicts,
        no_activity=len(summaries) == 0,
    )


# ---------------------------------------------------------------------------
# Database upsert helper
# ---------------------------------------------------------------------------


async def _upsert_daily_note(
    db: AsyncSession,
    date_str: str,
    markdown: str,
    html: str,
) -> DailyNote:
    """Insert a new DailyNote or update the existing one for ``date_str``.

    The ``daily_notes.date`` column has a UNIQUE constraint (see models.py),
    so only one row per date is allowed. On regeneration (second call for the
    same date), we update ``markdown``, ``html``, and ``updated_at`` in place
    rather than inserting a second row.

    Returns the DailyNote row (new or updated).
    """
    existing = (await db.execute(
        select(DailyNote).where(DailyNote.date == date_str)
    )).scalars().first()

    if existing is not None:
        existing.markdown = markdown
        existing.html = html
        existing.updated_at = iso_now()
        await db.flush()
        return existing

    note = DailyNote(
        markdown=markdown,
        html=html,
        date=date_str,
    )
    db.add(note)
    await db.flush()
    return note
