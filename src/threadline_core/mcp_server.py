"""Public MCP server for threadline-core.

Exposes the lifecycle and retrieval tool surface that any agent can use against
a threadline-core (or full Threadline) instance.  Only public-contract tools are
registered here; synthesis, ranking, and continuation tools live in the private
product.

Locked public tool surface
--------------------------
Session lifecycle:  start_session, end_session, log_agent_event
Project state:      get_project_state
Open loops:         get_open_loops, mark_open_loop_resolved (evidence-gated)
Decisions:          get_decisions, get_decision, mark_decision_outcome, get_known_traps
Findings:           propose_finding, confirm_finding, resolve_finding, dismiss_finding
Evidence:           get_evidence
Search:             search_memory

Excluded (private product): morning pulse tools, continuation generation,
research recommendation/storage, ranking, project grouping, dashboard tools.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from threadline_core.config import Settings, get_settings
from threadline_core.db import create_engine_for, init_db, session_factory
from threadline_core.models import Decision, OpenLoop
from threadline_core.protocol import AgentEventIn, EventType
from threadline_core.services import decisions as decisions_svc
from threadline_core.services import findings as findings_svc
from threadline_core.services import ingest as ingest_svc
from threadline_core.services import search as search_svc
from threadline_core.services.project_state import get_project_state


def build_server(settings: Settings | None = None) -> FastMCP:
    """Return a configured public FastMCP server."""
    if settings is None:
        settings = get_settings()

    engine: AsyncEngine = create_engine_for(settings.db_path)
    factory: async_sessionmaker[AsyncSession] = session_factory(engine)

    _initialized = False
    _init_lock = asyncio.Lock()

    async def _ensure_initialized() -> None:
        nonlocal _initialized
        if _initialized:
            return
        async with _init_lock:
            if not _initialized:
                await init_db(engine)
                _initialized = True

    mcp = FastMCP("threadline-core")

    # -----------------------------------------------------------------------
    # Session lifecycle
    # -----------------------------------------------------------------------

    @mcp.tool()
    async def start_session(
        project_key: str,
        agent_name: str = "claude_code",
    ) -> dict[str, Any]:
        """Call at the BEGINNING of every work session on a project.

        Returns a ``session_id`` to pass in all subsequent ``log_agent_event``
        calls so Threadline can group events into a coherent session.

        Returns
        -------
        dict with keys: ``session_id``, ``project_key``.
        """
        await _ensure_initialized()
        event = AgentEventIn(
            event_type=EventType.session_started,
            project_key=project_key,
            agent={"name": agent_name},
            summary=f"{agent_name} started a session on {project_key}",
        )
        async with factory() as db:
            result = await ingest_svc.ingest_event(db, settings, event, actor="mcp")
        return {"session_id": result.session_id, "project_key": project_key}

    @mcp.tool()
    async def end_session(
        session_id: str,
        summary: str,
        project_key: str,
        agent_name: str = "claude_code",
    ) -> dict[str, Any]:
        """Call at the END of a work session to close it cleanly.

        Records a ``session_ended`` event so the next agent can see what was done.

        Returns
        -------
        dict with keys: ``session_id``, ``status``, ``project_key``.
        """
        await _ensure_initialized()
        event = AgentEventIn(
            event_type=EventType.session_ended,
            project_key=project_key,
            session_id=session_id,
            agent={"name": agent_name},
            summary=summary,
        )
        async with factory() as db:
            await ingest_svc.ingest_event(db, settings, event, actor="mcp")
        return {"session_id": session_id, "status": "ended", "project_key": project_key}

    @mcp.tool()
    async def log_agent_event(event_json: str) -> dict[str, Any]:
        """Report a work event to Threadline — call this as you work.

        Parse and ingest a single ``AgentEventIn`` JSON payload.  Returns a
        summary of what was derived (decision count, open-loop count).

        WHEN TO CALL
        ------------
        - At significant checkpoints (code written, tests passing, a decision made).
        - When you encounter a blocker.
        - When you notice something deferred (open loop).

        EVENT SHAPE (compact example)
        ------------------------------
        ```json
        {
          "event_type": "checkpoint",
          "project_key": "my-project",
          "agent": {"name": "claude_code"},
          "session_id": "<from start_session>",
          "summary": "Implemented login route; all tests pass.",
          "details": {
            "decisions": ["Use JWT over session cookies for stateless auth"],
            "open_loops": ["Rate limiting not yet implemented"],
            "files_changed": ["src/auth.py", "tests/test_auth.py"],
            "verification": ["uv run pytest -q"]
          }
        }
        ```

        Returns
        -------
        dict with keys: ``event_id``, ``session_id``, ``decisions_created``,
        ``open_loops_created``.  On parse/validation error:
        ``{"error": true, "message": "..."}`` — fix and retry.
        """
        await _ensure_initialized()
        try:
            payload = json.loads(event_json)
            event = AgentEventIn(**payload)
        except Exception as exc:
            return {"error": True, "message": str(exc)}
        async with factory() as db:
            result = await ingest_svc.ingest_event(db, settings, event, actor="mcp")
        return {
            "event_id": result.event_id,
            "session_id": result.session_id,
            "decisions_created": result.decisions_created,
            "open_loops_created": result.open_loops_created,
        }

    # -----------------------------------------------------------------------
    # Project state
    # -----------------------------------------------------------------------

    @mcp.tool(name="get_project_state")
    async def get_project_state_tool(
        project_key: str,
    ) -> dict[str, Any]:
        """Return the raw governed state for a project.

        Returns durable lifecycle records only — no synthesis, no compiled
        memory, no LLM output, no ranking.

        Includes: objective, open loops, active decisions, known traps,
        confirmed gaps and caveats, evidence IDs, recent session metadata.

        Returns
        -------
        dict — the full ``ProjectState`` as a plain dict.

        Raises
        ------
        LookupError if the project does not exist (surfaced as an error dict).
        """
        await _ensure_initialized()
        try:
            async with factory() as db:
                state = await get_project_state(db, project_key)
        except LookupError as exc:
            return {"error": True, "message": str(exc)}
        return {
            "project_key": state.project_key,
            "objective": state.objective,
            "created_at": state.created_at,
            "last_session_id": state.last_session_id,
            "last_session_started": state.last_session_started,
            "last_session_ended": state.last_session_ended,
            "last_session_summary": state.last_session_summary,
            "open_loops": state.open_loops,
            "decisions": state.decisions,
            "known_traps": state.known_traps,
            "confirmed_gaps": state.confirmed_gaps,
            "confirmed_caveats": state.confirmed_caveats,
            "evidence_ids": state.evidence_ids,
            "recent_verifications": state.recent_verifications,
            "retrieved_at": state.retrieved_at,
        }

    # -----------------------------------------------------------------------
    # Open loops
    # -----------------------------------------------------------------------

    @mcp.tool()
    async def get_open_loops(project_key: str) -> list[dict[str, Any]]:
        """Return all open loops for a project, oldest first.

        Open loops are deferred threads — things noticed but not finished.
        Oldest-first because the longest-waiting item is most likely blocking.

        Returns
        -------
        list of dicts with keys: ``id``, ``description``, ``project_key``,
        ``status``, ``created_at``, ``updated_at``.
        """
        await _ensure_initialized()
        async with factory() as db:
            rows = list((await db.execute(
                select(OpenLoop)
                .where(OpenLoop.project_key == project_key, OpenLoop.status == "open")
                .order_by(OpenLoop.created_at.asc())
            )).scalars().all())
        return [
            {
                "id": r.id, "description": r.description,
                "project_key": r.project_key, "status": r.status,
                "created_at": r.created_at, "updated_at": r.updated_at,
            }
            for r in rows
        ]

    @mcp.tool()
    async def mark_open_loop_resolved(
        loop_id: str,
        evidence_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        """Mark an open loop resolved — GATED: requires admissible evidence.

        Evidence refs must be ``'<kind>:<id>'`` strings pointing to records that
        independently verify the loop is done.  Self-reference is rejected.

        Returns
        -------
        dict with keys: ``id``, ``status``, ``resolved_at``.
        """
        await _ensure_initialized()
        async with factory() as db:
            result = await ingest_svc.resolve_open_loop(
                db, loop_id, evidence_refs=evidence_refs or [], operator_confirmed=False
            )
        return result

    # -----------------------------------------------------------------------
    # Decisions
    # -----------------------------------------------------------------------

    @mcp.tool()
    async def get_decisions(project_key: str) -> list[dict[str, Any]]:
        """Return the LIVE decisions for a project, newest first.

        Live = status in (active, accepted, validated).  Decisions marked
        incorrect/reverted are returned by ``get_known_traps`` instead.

        Returns
        -------
        list of dicts with keys: ``id``, ``statement``, ``rationale``,
        ``status``, ``created_at``.
        """
        await _ensure_initialized()
        async with factory() as db:
            rows = list((await db.execute(
                select(Decision)
                .where(
                    Decision.project_key == project_key,
                    Decision.status.in_(["active", "accepted", "validated"]),
                )
                .order_by(Decision.created_at.desc())
            )).scalars().all())
        return [
            {
                "id": r.id, "statement": r.statement, "rationale": r.rationale,
                "status": r.status, "created_at": r.created_at,
            }
            for r in rows
        ]

    @mcp.tool()
    async def get_decision(decision_id: str) -> dict[str, Any]:
        """Return one decision + its outcome detail, or an error dict if not found.

        Returns
        -------
        dict with keys: ``id``, ``statement``, ``rationale``, ``status``,
        ``created_at``, ``outcome`` (the detail dict or None).
        """
        await _ensure_initialized()
        async with factory() as db:
            row = await db.get(Decision, decision_id)
            if row is None:
                return {"error": True, "message": f"Decision not found: {decision_id!r}"}
            outcome = await decisions_svc.get_decision_outcome(db, decision_id)
        return {
            "id": row.id, "statement": row.statement, "rationale": row.rationale,
            "status": row.status, "created_at": row.created_at,
            "outcome": outcome,
        }

    @mcp.tool()
    async def mark_decision_outcome(
        decision_id: str,
        outcome: str,
        reason: str | None = None,
        corrected_rule: str | None = None,
        evidence_refs: list[str] | None = None,
        severity: str | None = None,
        applies_to: list[str] | None = None,
    ) -> dict[str, Any]:
        """Record the real-world OUTCOME of a past decision.

        ``outcome`` must be one of: accepted, validated, incorrect, reverted,
        unresolved.

        HARD RULE: marking ``incorrect``/``reverted``/``validated`` requires
        admissible ``evidence_refs`` — records that independently back the
        outcome.  Self-certification is rejected.  An operator may override via
        the CLI.

        Returns
        -------
        dict with keys: ``id``, ``decision_id``, ``outcome``, ``status``,
        ``severity``, ``evidence_refs``, ``superseded``, ``marked_at``.
        """
        await _ensure_initialized()
        async with factory() as db:
            result = await decisions_svc.mark_decision_outcome(
                db,
                decision_id=decision_id,
                outcome=outcome,
                reason=reason,
                corrected_rule=corrected_rule,
                evidence_refs=evidence_refs or [],
                severity=severity,
                applies_to=applies_to or [],
                actor="mcp",
                operator_confirmed=False,
            )
        return result

    @mcp.tool()
    async def get_known_traps(project_key: str) -> list[dict[str, Any]]:
        """Return decisions proven wrong on this project + the corrected rule.

        Read these BEFORE acting so you do not repeat a known mistake.
        Newest first; each carries the corrected_rule (the lesson), severity,
        and evidence_refs.

        Returns
        -------
        list of dicts with keys: ``decision_id``, ``statement``, ``outcome``,
        ``corrected_rule``, ``severity``, ``evidence_refs``, ``marked_at``.
        """
        await _ensure_initialized()
        async with factory() as db:
            result = await decisions_svc.get_known_traps(db, project_key)
        return result

    # -----------------------------------------------------------------------
    # Findings
    # -----------------------------------------------------------------------

    @mcp.tool()
    async def propose_finding(
        project_key: str,
        finding_class: str,
        category: str,
        statement: str,
        severity: str,
        impact: str | None = None,
        resolution_condition: str | None = None,
        source_session_id: str | None = None,
    ) -> dict[str, Any]:
        """Propose a gap or caveat finding.

        PROPOSING IS FREE — a proposed finding is never surfaced in the trusted
        context bundle.  It is a candidate until confirmed with evidence.

        ``finding_class`` ∈ {gap, caveat}.
        ``severity`` ∈ {low, medium, high, critical}.

        Fingerprint dedup: re-proposing an active finding returns the existing
        id (no duplicate write).

        Returns
        -------
        dict with keys: ``finding_id``, ``status``, ``finding_class``,
        ``category``, ``statement``, ``severity``.
        """
        await _ensure_initialized()
        async with factory() as db:
            result = await findings_svc.propose_finding(
                db,
                project_key=project_key,
                finding_class=finding_class,
                category=category,
                statement=statement,
                severity=severity,
                impact=impact,
                resolution_condition=resolution_condition,
                source_session_id=source_session_id,
            )
        return result

    @mcp.tool()
    async def confirm_finding(
        finding_id: str,
        evidence_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        """Confirm a proposed finding so it enters the trusted bundle.

        GATED: requires ``evidence_refs`` pointing to independently
        evidence-bearing records.  A bare assertion, another finding, or a
        self-referential loop is rejected.

        Returns
        -------
        dict with keys: ``finding_id``, ``status``, ``confirmed_at``.
        """
        await _ensure_initialized()
        async with factory() as db:
            result = await findings_svc.confirm_finding(
                db, finding_id=finding_id,
                evidence_refs=evidence_refs or [], operator_confirmed=False,
            )
        return result

    @mcp.tool()
    async def resolve_finding(
        finding_id: str,
        evidence_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        """Mark a finding resolved (its resolution_condition was met).

        GATED exactly like ``confirm_finding``.

        Returns
        -------
        dict with keys: ``finding_id``, ``status``, ``resolved_at``.
        """
        await _ensure_initialized()
        async with factory() as db:
            result = await findings_svc.resolve_finding(
                db, finding_id=finding_id,
                evidence_refs=evidence_refs or [], operator_confirmed=False,
            )
        return result

    @mcp.tool()
    async def dismiss_finding(
        finding_id: str,
        reason: str | None = None,
        evidence_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        """Dismiss a finding as invalid/irrelevant.

        GATED exactly like ``confirm_finding``.

        Returns
        -------
        dict with keys: ``finding_id``, ``status``, ``dismissed_at``.
        """
        await _ensure_initialized()
        async with factory() as db:
            result = await findings_svc.dismiss_finding(
                db, finding_id=finding_id, reason=reason,
                evidence_refs=evidence_refs or [], operator_confirmed=False,
            )
        return result

    # -----------------------------------------------------------------------
    # Evidence
    # -----------------------------------------------------------------------

    @mcp.tool()
    async def get_evidence(
        refs: list[str],
        max_chars: int = 3000,
    ) -> dict[str, Any]:
        """Resolve evidence refs (``'<kind>:<id>'``) to bounded content snippets.

        Returns bounded snippets only — never full transcript bodies.  Bad refs
        are reported in ``unresolved`` without failing the whole call.

        Returns
        -------
        dict with keys: ``evidence`` (list), ``unresolved`` (list),
        ``truncated_refs`` (list).
        """
        await _ensure_initialized()
        async with factory() as db:
            result = await decisions_svc.get_evidence(db, refs, max_chars=max_chars)
        return result

    # -----------------------------------------------------------------------
    # Search
    # -----------------------------------------------------------------------

    @mcp.tool()
    async def search_memory(
        query: str,
        project_key: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Full-text search across all stored memory.

        Searches events, decisions, open loops, daily notes, and compiled
        context fragments.  Results are ranked by BM25 relevance.

        Parameters
        ----------
        query:
            Natural-language search string.
        project_key:
            When provided, restricts results to that project.
        limit:
            Maximum results to return (clamped to 50 server-side).

        Returns
        -------
        list of dicts with keys: ``kind``, ``ref_id``, ``project_key``,
        ``title``, ``snippet``.
        """
        await _ensure_initialized()
        import dataclasses
        async with factory() as db:
            hits = await search_svc.search_memory(db, query, project_key=project_key, limit=limit)
        return [dataclasses.asdict(h) for h in hits]

    return mcp


def main() -> None:
    """Entrypoint for the ``threadline-core-mcp`` console script."""
    mcp = build_server()
    mcp.run()
