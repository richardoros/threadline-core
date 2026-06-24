"""Ingest service — the heart of Threadline.

This module turns a validated ``AgentEventIn`` payload into durable memory.
``ingest_event`` is the single entry point: it runs each step in order, commits
once at the end, and returns an ``IngestResult`` summary.

The design principle: each logical step is its own helper function with a
docstring. The main orchestrator ``ingest_event`` reads as a plain list of
named steps, not as an implementation. All helper functions are synchronous
where they only manipulate in-memory SQLAlchemy objects, and async where they
need the DB.

Deduplication note: Decisions and OpenLoops are deduplicated by exact
case-sensitive string match against active/open rows for the same project.
This keeps the logic simple and predictable. Agents that change the wording
of a statement even slightly will create a new row; that is intentional —
slightly different wording often means a genuinely different thought.

Session tolerance note: agents may report against closed, foreign, or unknown
sessions. The ``agent_events.session_id`` foreign key is enforced by SQLite,
so a dangling reference cannot be stored — instead, when an event carries a
``session_id`` that matches no AgentSession row, the event is still ingested
and the DB row's ``session_id`` is set to None. The agent's original
``session_id`` is always preserved verbatim in the JSONL evidence line, so
nothing the agent reported is lost. See ``_resolve_session_id``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from threadline_core.config import Settings
from threadline_core.models import (
    AgentEvent,
    AgentSession,
    AuditLog,
    Decision,
    OpenLoop,
    Project,
)
from threadline_core.protocol import AgentEventIn, EventType
from threadline_core.services.fts import FTS_TITLE_MAX, fts_insert
from threadline_core.services.sessions import reap_stale_sessions
from threadline_core.utils.ids import new_id
from threadline_core.utils.time import iso_now, today_str

# Prefix applied to OpenLoop descriptions derived from blocker events.
# PUBLIC: this module owns the convention; consumers (e.g. services/daily.py)
# import it to recognise blocker-derived loops instead of re-hardcoding the
# string. Includes the trailing space on purpose — "[blocker] summary".
BLOCKER_PREFIX = "[blocker] "


@dataclass
class IngestResult:
    """Summary of what ``ingest_event`` created.

    Fields
    ------
    event_id:
        Primary key of the new AgentEvent row.
    session_id:
        The AgentSession id associated with this event, or None if the event
        carried no session context.
    decisions_created:
        Number of new Decision rows created (0 when all were deduplicated).
    open_loops_created:
        Number of new OpenLoop rows created (0 when all were deduplicated).
    """

    event_id: str
    session_id: str | None
    decisions_created: int = field(default=0)
    open_loops_created: int = field(default=0)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def ingest_event(
    db: AsyncSession,
    settings: Settings,
    event: AgentEventIn,
    actor: str = "api",
) -> IngestResult:
    """Persist one agent event and derive structured memory from it.

    This is the single entry point for all agent-reported events. It runs
    each step in order, then commits once. If any step raises, nothing is
    committed (SQLAlchemy rolls back on context-manager exit).

    Steps (each is its own helper):
    1. Project upsert
    2. Session lifecycle
    3. AgentEvent row insert
    4. JSONL evidence log append
    5. Memory derivation (Decisions, OpenLoops)
    6. FTS indexing
    7. Audit log entry
    8. Single commit + return IngestResult
    """
    project_key = event.project_key

    # Step 1 — ensure the project exists
    await _upsert_project(db, project_key)

    # Step 2 — handle session lifecycle; returns the resolved session_id and
    # the effective event type (which may differ from what the agent sent —
    # e.g. an unmatched session_ended becomes "orphan_end" on the DB row).
    session_id, effective_event_type = await _handle_session_lifecycle(db, event)

    # Step 2b — opportunistic sweep. A new session starting is a natural,
    # low-frequency moment to close sessions whose SessionEnd never arrived
    # (crash/kill/reboot), so "active" never accumulates ghosts. Bounded: runs
    # once per session start, inside this same transaction.
    if event.event_type == EventType.session_started:
        await reap_stale_sessions(
            db, max_idle_seconds=settings.session_max_idle_seconds
        )

    # Step 3 — write the raw AgentEvent row
    event_row = _build_event_row(event, session_id, effective_event_type)
    db.add(event_row)
    await db.flush()  # populate event_row.id before downstream steps use it

    # Step 4 — append to JSONL evidence log. Written BEFORE the commit on
    # purpose: the line records receipt of the event even if the DB
    # transaction later fails. See _append_jsonl docstring for the contract.
    _append_jsonl(settings, event, event_row.id)

    # Step 5 — derive Decisions and OpenLoops from this event
    new_decisions, new_open_loops = await _derive_memory(db, event, event_row.id)

    # Step 6 — index everything into FTS
    await _index_fts(db, event, event_row.id, new_decisions, new_open_loops)

    # Step 7 — write audit row
    db.add(AuditLog(
        actor=actor,
        action="ingest_event",
        detail=f"{event.event_type.value} {project_key}",
    ))

    # Step 8 — single commit
    await db.commit()

    return IngestResult(
        event_id=event_row.id,
        session_id=session_id,
        decisions_created=len(new_decisions),
        open_loops_created=len(new_open_loops),
    )


# ---------------------------------------------------------------------------
# Step helpers
# ---------------------------------------------------------------------------


async def _upsert_project(db: AsyncSession, key: str) -> Project:
    """Return the Project for ``key``, creating it with ``name=key`` if absent.

    Projects are created on first mention — agents do not need to pre-register
    a project before reporting events against it.
    """
    # Read-then-insert is safe here because SQLite serializes writes — two
    # concurrent ingests cannot both pass the "is None" check and insert.
    # Revisit if migrating to Postgres, where this is a real race and an
    # ON CONFLICT DO NOTHING upsert would be needed instead.
    project = await db.get(Project, key)
    if project is None:
        project = Project(key=key, name=key)
        db.add(project)
        await db.flush()
    return project


async def _handle_session_lifecycle(
    db: AsyncSession, event: AgentEventIn
) -> tuple[str | None, str]:
    """Create, close, or resolve an AgentSession based on event type.

    Returns a tuple (session_id, effective_event_type):

    - session_started (idempotent):
        If a ``client_session_id`` is present in details AND an active
        AgentSession already exists with that external id for this project,
        re-use it (no duplicate created). Otherwise, create a new AgentSession
        populated with provenance columns from details. Returns
        ``(session.id, "session_started")``.

    - session_ended (orphan-safe):
        Resolves the session by EITHER the top-level event.session_id as a
        Threadline primary key (the MCP ``end_session`` path) OR by matching
        ``external_session_id == details.client_session_id`` (the hook path).
        If found: marks it ended and returns ``(session.id, "session_ended")``.
        If NOT found: returns ``(None, "orphan_end")`` — no session is closed,
        and the DB row gets event_type="orphan_end" as a diagnostic marker.
        The agent always sends "session_ended"; the server downgrades to
        "orphan_end" here so operators can spot unmatched hook events.
        NOTE: "orphan_end" is a server-side DB string only — it is never
        added to the EventType protocol enum.

    - all other event types:
        Delegates to ``_resolve_session_id`` (unchanged tolerance logic).
        Returns ``(resolved_id_or_none, event.event_type.value)``.
    """
    if event.event_type == EventType.session_started:
        details = event.details or {}
        client_session_id: str | None = details.get("client_session_id")
        cwd: str | None = details.get("cwd")
        repo: str | None = details.get("repo")
        branch: str | None = details.get("branch")
        # "source" defaults to "hook" when client_session_id present (hook-opened),
        # "mcp" otherwise (opened via the MCP start_session tool).
        source: str = details.get("source") or ("hook" if client_session_id else "mcp")

        # Idempotent: if a matching active session already exists, reuse it.
        if client_session_id:
            existing = await _find_active_session_by_external_id(
                db, event.project_key, client_session_id
            )
            if existing is not None:
                return (existing.id, "session_started")

        session = AgentSession(
            id=new_id(),
            project_key=event.project_key,
            agent_name=event.agent.name,
            agent_type=event.agent.type,
            status="active",
            external_session_id=client_session_id,
            cwd=cwd,
            repo=repo,
            branch=branch,
            source=source,
        )
        db.add(session)
        await db.flush()
        return (session.id, "session_started")

    if event.event_type == EventType.session_ended:
        details = event.details or {}
        client_session_id = details.get("client_session_id")
        session: AgentSession | None = None

        # Path 1: top-level session_id is a Threadline primary key (MCP end_session path).
        if event.session_id:
            session = await db.get(AgentSession, event.session_id)

        # Path 2: match by external_session_id (hook path — client sends client_session_id
        # in details because the top-level session_id is the Threadline internal pk
        # which hooks don't know).
        if session is None and client_session_id:
            session = await _find_active_session_by_external_id(
                db, event.project_key, client_session_id
            )

        if session is None:
            # No matching session found — record as orphan diagnostic, not as a real end.
            return (None, "orphan_end")

        session.ended_at = iso_now()
        session.status = "ended"
        session.summary = event.summary
        await db.flush()
        return (session.id, "session_ended")

    resolved = await _resolve_session_id(db, event)
    return (resolved, event.event_type.value)


async def _find_active_session_by_external_id(
    db: AsyncSession,
    project_key: str,
    external_id: str,
) -> AgentSession | None:
    """Find the most recent active AgentSession matching a client-side external id.

    Used by the hook path to bridge from the client's own session id (e.g.
    Claude Code's conversation UUID stored in ``external_session_id``) to
    Threadline's internal primary key, without the client needing to know the
    internal id at all.

    Prefers status="active" rows; falls back to any row with that external id
    for this project (newest first) in case the session was already ended by
    another path. Returns None when no matching row exists.
    """
    result = await db.execute(
        select(AgentSession)
        .where(
            AgentSession.project_key == project_key,
            AgentSession.external_session_id == external_id,
        )
        # active rows first, then by start time descending so the newest wins
        .order_by(
            # SQLite: "active" < "ended" alphabetically, so DESC puts "ended" first —
            # invert with ASC to get "active" first; then secondary sort by started_at DESC.
            AgentSession.status.asc(),
            AgentSession.started_at.desc(),
        )
        .limit(1)
    )
    return result.scalars().first()


async def _resolve_session_id(db: AsyncSession, event: AgentEventIn) -> str | None:
    """Return the event's session_id only if that AgentSession row exists.

    Session tolerance: agents may report against closed, foreign, or unknown
    sessions (a prior run, a different process, a session Threadline never
    saw start). Rejecting such events would lose real work history, but the
    ``agent_events.session_id`` foreign key is enforced, so a dangling id
    cannot be stored. The compromise: keep the event and drop the dangling
    DB reference (store None). The agent's original session_id is preserved
    verbatim in the JSONL evidence line, so nothing reported is lost.
    """
    if event.session_id is None:
        return None
    session = await db.get(AgentSession, event.session_id)
    return session.id if session is not None else None


def _build_event_row(
    event: AgentEventIn,
    session_id: str | None,
    effective_event_type: str,
) -> AgentEvent:
    """Construct an AgentEvent ORM object from the validated payload.

    ``occurred_at`` uses the agent's reported timestamp when provided, falling
    back to the current UTC time. Agent timestamps are NORMALIZED TO UTC
    before storage: the DB keeps a single timezone basis so string sorting and
    date-prefix bucketing (daily notes) are correct across agents in different
    timezones. The agent's original offset is not lost — the JSONL evidence
    line (``_append_jsonl``) records ``occurred_at`` exactly as reported.
    ``timespec="microseconds"`` matches the ``iso_now()`` format so all
    occurred_at strings have identical shape. ``details`` is serialised to
    JSON text.

    ``effective_event_type`` is what the server decided to store and may differ
    from ``event.event_type.value``: a ``session_ended`` with no matching
    session becomes ``"orphan_end"`` on the DB row so operators can distinguish
    real session ends from hook events that arrived before or without a
    corresponding start.  The JSONL evidence line records ``event.event_type``
    (what the agent reported) to preserve the raw signal; the DB row stores the
    effective type for query correctness.
    """
    occurred_at = (
        event.occurred_at.astimezone(timezone.utc).isoformat(timespec="microseconds")
        if event.occurred_at is not None
        else iso_now()
    )
    return AgentEvent(
        id=new_id(),
        session_id=session_id,
        project_key=event.project_key,
        event_type=effective_event_type,
        summary=event.summary,
        details_json=json.dumps(event.details),
        occurred_at=occurred_at,
        contains_secrets=event.privacy.contains_secrets,
        allow_cloud_processing=event.privacy.allow_cloud_processing,
    )


def _append_jsonl(settings: Settings, event: AgentEventIn, event_id: str) -> None:
    """Append one JSON line to the daily JSONL evidence log.

    Each line is the full validated event payload plus ``id`` (the DB row id)
    and ``received_at`` (the ingest timestamp). The file is named
    ``YYYY-MM-DD.jsonl`` in ``settings.events_dir``.

    This is the replay/audit layer: the DB row is canonical for queries; the
    JSONL is an immutable proof trail that can reconstruct the DB if needed.

    Ordering contract: this line is written BEFORE the DB commit. If the
    commit later fails, the JSONL will contain an event that is absent from
    the database. That is intentional — the JSONL is evidence that the event
    was received, not evidence that it was committed. The DB stays canonical;
    consumers of the JSONL must treat it as a superset of committed events.
    """
    log_dir: Path = settings.events_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{today_str()}.jsonl"

    record = event.model_dump(mode="json")
    record["id"] = event_id
    record["received_at"] = iso_now()

    # Sync file I/O is acceptable at v0.1 single-agent load; switch to a
    # thread pool or aiofiles if high event throughput ever blocks the loop.
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


async def _derive_memory(
    db: AsyncSession,
    event: AgentEventIn,
    event_id: str,
) -> tuple[list[Decision], list[OpenLoop]]:
    """Derive structured Decisions and OpenLoops from the event payload.

    Primary derivation rules (driven by event_type):
    - ``decision``  → one Decision from event.summary + details.rationale
    - ``open_loop`` → one OpenLoop from event.summary
    - ``blocker``   → one OpenLoop with ``BLOCKER_PREFIX`` on event.summary

    Secondary derivation (applies to ANY event type):
    - Each string in details["decisions"]  → one Decision
    - Each string in details["open_loops"] → one OpenLoop

    Deduplication: a Decision is skipped if an active Decision with the same
    statement already exists for this project. An OpenLoop is skipped if an
    open OpenLoop with the same description already exists. Both checks are
    exact case-sensitive string matches; rationale and source_event_id are
    not considered. This keeps dedup simple and avoids false positives.
    """
    new_decisions: list[Decision] = []
    new_open_loops: list[OpenLoop] = []

    # Primary: event_type drives the main derived row
    if event.event_type == EventType.decision:
        # details is always a dict (protocol default_factory) — no None guard needed
        rationale = event.details.get("rationale")
        d = await _maybe_create_decision(
            db, event.project_key, event.summary, rationale, event_id
        )
        if d:
            new_decisions.append(d)

    elif event.event_type == EventType.open_loop:
        ol = await _maybe_create_open_loop(
            db, event.project_key, event.summary, event_id
        )
        if ol:
            new_open_loops.append(ol)

    elif event.event_type == EventType.blocker:
        description = f"{BLOCKER_PREFIX}{event.summary}"
        ol = await _maybe_create_open_loop(
            db, event.project_key, description, event_id
        )
        if ol:
            new_open_loops.append(ol)

    # Secondary: embedded lists in details apply to all event types
    for statement in event.details.get("decisions", []):
        d = await _maybe_create_decision(
            db, event.project_key, statement, None, event_id
        )
        if d:
            new_decisions.append(d)

    for description in event.details.get("open_loops", []):
        ol = await _maybe_create_open_loop(
            db, event.project_key, description, event_id
        )
        if ol:
            new_open_loops.append(ol)

    return new_decisions, new_open_loops


async def _maybe_create_decision(
    db: AsyncSession,
    project_key: str,
    statement: str,
    rationale: str | None,
    source_event_id: str,
) -> Decision | None:
    """Create a Decision if no active Decision with the same statement exists.

    Returns the new Decision, or None if the row was deduplicated.
    """
    existing = (await db.execute(
        select(Decision).where(
            Decision.project_key == project_key,
            Decision.statement == statement,
            Decision.status == "active",
        )
    )).scalars().first()

    if existing is not None:
        return None

    decision = Decision(
        id=new_id(),
        project_key=project_key,
        statement=statement,
        rationale=rationale,
        source_event_id=source_event_id,
        status="active",
    )
    db.add(decision)
    await db.flush()
    return decision


async def _maybe_create_open_loop(
    db: AsyncSession,
    project_key: str,
    description: str,
    source_event_id: str,
) -> OpenLoop | None:
    """Create an OpenLoop if no open OpenLoop with the same description exists.

    Returns the new OpenLoop, or None if the row was deduplicated.
    """
    existing = (await db.execute(
        select(OpenLoop).where(
            OpenLoop.project_key == project_key,
            OpenLoop.description == description,
            OpenLoop.status == "open",
        )
    )).scalars().first()

    if existing is not None:
        return None

    loop = OpenLoop(
        id=new_id(),
        project_key=project_key,
        description=description,
        source_event_id=source_event_id,
        status="open",
    )
    db.add(loop)
    await db.flush()
    return loop


async def _index_fts(
    db: AsyncSession,
    event: AgentEventIn,
    event_id: str,
    decisions: list[Decision],
    open_loops: list[OpenLoop],
) -> None:
    """Insert rows into the ``memory_fts`` FTS5 virtual table.

    One row per event, one per created Decision, one per created OpenLoop.
    The ``title`` is truncated to FTS_TITLE_MAX characters so long summaries
    do not bloat the index title field.
    """
    await fts_insert(
        db,
        title=f"{event.event_type.value}: {event.summary}"[:FTS_TITLE_MAX],
        body=event.summary,
        kind="event",
        ref_id=event_id,
        project_key=event.project_key,
    )

    for d in decisions:
        body = d.statement if not d.rationale else f"{d.statement}\n{d.rationale}"
        await fts_insert(
            db,
            title=d.statement[:FTS_TITLE_MAX],
            body=body,
            kind="decision",
            ref_id=d.id,
            project_key=event.project_key,
        )

    for ol in open_loops:
        await fts_insert(
            db,
            title=ol.description[:FTS_TITLE_MAX],
            body=ol.description,
            kind="open_loop",
            ref_id=ol.id,
            project_key=event.project_key,
        )


# ---------------------------------------------------------------------------
# State-transition helpers
# ---------------------------------------------------------------------------


async def _gate_loop_resolution(
    db: AsyncSession,
    *,
    project_key: str,
    evidence_refs: list[str] | None,
    operator_confirmed: bool,
) -> list[str]:
    """Trust gate for DURABLE loop resolution — the same admissibility tier as the
    decision/finding lifecycles, worded for loops. Returns the cleaned evidence refs.

    Agent path (``operator_confirmed=False``): requires at least one admissible
    same-project evidence ref; raises ``ValueError`` (caller must NOT mutate) otherwise.
    Operator path: skips the evidence requirement but still validates any supplied refs.

    Reuses the existing primitives. Imported LAZILY because ``ingest`` is low-level and
    ``findings -> next_steps -> ingest`` would otherwise form an import cycle.
    """
    from threadline_core.services.decisions import _resolve_evidence_ref  # noqa: PLC2701
    from threadline_core.services.findings import _is_admissible_evidence  # noqa: PLC2701

    refs = [r.strip() for r in (evidence_refs or []) if r and r.strip()]
    if operator_confirmed:
        for ref in refs:  # operator path still validates that any cited refs resolve
            await _resolve_evidence_ref(db, ref, project_key=project_key, decision_id="")
        return refs
    if not refs:
        raise ValueError(
            "an open loop may be resolved only with admissible same-project evidence "
            "(a record that independently bears out the completion) or explicit operator "
            "action; an agent must never close a loop from confidence alone. Pass "
            "evidence_refs like ['agent_event:<id>'] (verification-bearing), or resolve via "
            "the operator CLI (`threadline loop resolve <id> --operator`)."
        )
    for ref in refs:
        await _resolve_evidence_ref(db, ref, project_key=project_key, decision_id="")
        kind, _, rid = ref.partition(":")
        if not await _is_admissible_evidence(db, kind.strip(), rid.strip()):
            raise ValueError(
                f"evidence_ref {ref!r} is not independently evidence-bearing — an open loop, "
                "a bare agent_event, or a finding cannot prove a loop's completion. Cite a "
                "verification-bearing event, an evidence-backed pulse/decision, or a sourced "
                "research_brief."
            )
    return refs


async def resolve_open_loop(
    db: AsyncSession,
    loop_id: str,
    actor: str = "api",
    *,
    evidence_refs: list[str] | None = None,
    operator_confirmed: bool = False,
) -> OpenLoop:
    """Resolve an open loop — GATED like every other trusted lifecycle.

    Durable completion requires EITHER admissible same-project ``evidence_refs`` (a record
    that independently bears out the completion) OR ``operator_confirmed=True`` (the trusted
    CLI/dashboard operator path). The MCP/agent path passes ``operator_confirmed=False`` and
    must supply evidence; the model may never close a loop from confidence alone. See
    ``_gate_loop_resolution``.

    Idempotent: an already-resolved loop is returned unchanged with no new audit row.
    Rejects with ``ValueError`` and NO mutation on missing / nonexistent / cross-project /
    inadmissible evidence. Raises ``LookupError`` (before the gate) if ``loop_id`` is unknown.
    """
    loop = await db.get(OpenLoop, loop_id)
    if loop is None:
        raise LookupError(f"OpenLoop not found: {loop_id!r}")
    if loop.status == "resolved":
        return loop  # idempotent — no re-mutation, no duplicate audit row

    refs = await _gate_loop_resolution(
        db, project_key=loop.project_key,
        evidence_refs=evidence_refs, operator_confirmed=operator_confirmed,
    )

    now = iso_now()
    loop.status = "resolved"
    loop.resolved_at = now
    loop.updated_at = now

    basis = "operator" if operator_confirmed else "evidence:" + ",".join(refs)
    db.add(AuditLog(
        actor=actor,
        action="resolve_open_loop",
        detail=f"loop {loop_id} basis={basis}",
    ))
    await db.commit()
    return loop


async def supersede_decision(
    db: AsyncSession,
    old_id: str,
    new_statement: str,
    rationale: str | None = None,
    actor: str = "api",
) -> Decision:
    """Replace an existing Decision with a new one, marking the old as superseded.

    Creates a new active Decision with ``new_statement`` and optional
    ``rationale``, then sets the old Decision's ``status='superseded'`` and
    ``superseded_by=<new_id>``. Writes an audit row and commits.

    Returns the NEW Decision (the one that is now active).

    The replacement Decision intentionally has no ``source_event_id``: it was
    born from this API call, not from an agent event. The audit log row
    (action ``supersede_decision``, detail "old_id → new_id") is the
    provenance record for supersedes.

    Raises LookupError if ``old_id`` is not found — callers should translate
    this to a 404 HTTP response. If the old decision was already superseded
    this function will still process it; agents may supersede out-of-order and
    Threadline should not block legitimate corrections.
    """
    old = await db.get(Decision, old_id)
    if old is None:
        raise LookupError(f"Decision not found: {old_id!r}")

    new = Decision(
        id=new_id(),
        project_key=old.project_key,
        statement=new_statement,
        rationale=rationale,
        status="active",
    )
    db.add(new)
    await db.flush()  # new.id must exist before we reference it below

    old.status = "superseded"
    old.superseded_by = new.id

    db.add(AuditLog(
        actor=actor,
        action="supersede_decision",
        detail=f"{old_id} → {new.id}",
    ))
    await db.commit()
    return new
