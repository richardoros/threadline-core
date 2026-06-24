"""Threadline Core command-line interface.

Entry point declared in pyproject.toml:
    threadline-core = "threadline_core.cli:app"

Excluded from the public surface (private-product features):
    - ``prompt``  — requires the private compiled-memory + pulse stack
    - ``next``    — requires the private next-steps ranker
    - ``research`` — requires the private research-recommendations service
    - ``pulse``   — requires the private local-LLM Morning Pulse pipeline

Design decisions
----------------
- Every command builds a fresh ``Settings()`` at call time — module-level
  ``get_settings()`` singleton is intentionally NOT used so CLI tests can
  inject ``THREADLINE_DATA_DIR`` via ``env=``.

- ``_with_db(settings)`` opens the engine, runs ``init_db`` (idempotent),
  yields a session, then disposes the engine on exit.

- ``_run(coro)`` is a minimal ``asyncio.run`` wrapper.

- Service modules are imported lazily inside command bodies so
  ``threadline-core --help`` stays fast.
"""
from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

import typer
from sqlalchemy.ext.asyncio import AsyncSession

from threadline_core.config import Settings
from threadline_core.db import create_engine_for, init_db, session_factory

app = typer.Typer(
    help="Threadline Core — self-hosted continuity memory for AI agents.",
    no_args_is_help=True,
)

projects_app = typer.Typer(
    help="Manage projects (add / list / set-objective).",
    no_args_is_help=True,
)
app.add_typer(projects_app, name="projects")

sessions_app = typer.Typer(
    help="Manage agent sessions (reap stale ones whose SessionEnd never fired).",
    no_args_is_help=True,
)
app.add_typer(sessions_app, name="sessions")

decision_app = typer.Typer(
    help="Decision-quality ledger — mark outcomes of past decisions (operator path).",
    no_args_is_help=True,
)
app.add_typer(decision_app, name="decision")

finding_app = typer.Typer(
    help="Findings ledger — confirm/resolve/dismiss gaps & caveats (operator path).",
    no_args_is_help=True,
)
app.add_typer(finding_app, name="finding")

loop_app = typer.Typer(
    help="Open-loop operator actions — resolve with admissible evidence or --operator.",
    no_args_is_help=True,
)
app.add_typer(loop_app, name="loop")

connect_app = typer.Typer(
    help="Scaffold agent connectors for existing projects.",
    no_args_is_help=True,
)
app.add_typer(connect_app, name="connect")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


@asynccontextmanager
async def _with_db(settings: Settings) -> AsyncGenerator[AsyncSession, None]:
    """Open one ``AsyncSession`` for a CLI command, then dispose the engine.

    CLI processes are short-lived — explicit disposal releases the aiosqlite
    file handle before the process exits rather than relying on the GC.
    """
    engine = create_engine_for(settings.db_path)
    await init_db(engine)
    factory = session_factory(engine)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# threadline-core init
# ---------------------------------------------------------------------------


@app.command()
def init() -> None:
    """Initialise the Threadline data directory and database.

    Creates the data directory, events directory, and SQLite database if they
    do not already exist.  Safe to run multiple times — fully idempotent.
    """

    async def _init() -> None:
        settings = Settings()
        settings.events_dir.mkdir(parents=True, exist_ok=True)
        engine = create_engine_for(settings.db_path)
        try:
            await init_db(engine)
        finally:
            await engine.dispose()
        typer.echo(f"db:         {settings.db_path}")
        typer.echo(f"events dir: {settings.events_dir}")
        typer.echo("Threadline Core initialized.")

    _run(_init())


# ---------------------------------------------------------------------------
# threadline-core serve
# ---------------------------------------------------------------------------


@app.command()
def serve(
    host: str | None = typer.Option(
        None, help="Bind host.  Defaults to settings.host (127.0.0.1)."
    ),
    port: int | None = typer.Option(
        None, help="Bind port.  Defaults to settings.port (8400)."
    ),
) -> None:
    """Start the Threadline Core HTTP API server with uvicorn.

    Binds to 127.0.0.1 by default.  For Tailscale access pass your tailnet IP
    address (e.g. --host 100.x.y.z) or --host 0.0.0.0 to bind all interfaces.
    Do this consciously — 0.0.0.0 on an internet-reachable machine exposes the
    unauthenticated dashboard.
    """
    import uvicorn

    from threadline_core.api.app import create_app

    settings = Settings()
    resolved_host = host if host is not None else settings.host
    resolved_port = port if port is not None else settings.port
    uvicorn.run(create_app(settings), host=resolved_host, port=resolved_port)


# ---------------------------------------------------------------------------
# threadline-core mcp
# ---------------------------------------------------------------------------


@app.command()
def mcp() -> None:
    """Start the Threadline Core MCP server on stdio.

    Launched by an MCP client (e.g. Claude Code) — not intended for
    interactive use.  stdin receives JSON-RPC requests; stdout returns
    responses.
    """
    from threadline_core.mcp_server import main as mcp_main

    mcp_main()


# ---------------------------------------------------------------------------
# threadline-core projects add / list / set-objective
# ---------------------------------------------------------------------------


@projects_app.command("add")
def projects_add(
    key: str = typer.Argument(help="Project key (slug, e.g. 'my-project')."),
    name: str | None = typer.Option(None, "--name", help="Human-readable project name."),
    objective: str | None = typer.Option(None, "--objective", help="Current project objective."),
) -> None:
    """Create a new project.

    KEY is the slug used everywhere as the project identifier.
    Exits with code 1 if a project with that key already exists.
    """

    async def _add() -> None:
        from threadline_core.models import Project

        settings = Settings()
        async with _with_db(settings) as db:
            existing = await db.get(Project, key)
            if existing is not None:
                typer.echo(f"Error: project '{key}' already exists.", err=True)
                raise typer.Exit(code=1)
            project = Project(
                key=key,
                name=name if name is not None else key,
                current_objective=objective,
            )
            db.add(project)
            await db.commit()
            typer.echo(f"Created project '{key}'.")

    _run(_add())


@projects_app.command("list")
def projects_list() -> None:
    """List all projects with their status and current objective."""

    async def _list() -> None:
        from sqlalchemy import select

        from threadline_core.models import Project

        settings = Settings()
        async with _with_db(settings) as db:
            rows = (await db.execute(select(Project).order_by(Project.key))).scalars().all()
            if not rows:
                typer.echo("(no projects)")
                return
            for proj in rows:
                objective_str = proj.current_objective or "(no objective)"
                typer.echo(
                    f"{proj.key:20s}  {proj.name:30s}  {proj.status:8s}  {objective_str}"
                )

    _run(_list())


@projects_app.command("set-objective")
def projects_set_objective(
    key: str = typer.Argument(help="Key (slug) of an existing project."),
    objective: str = typer.Argument(help="The new current objective text."),
) -> None:
    """Set (or replace) the current objective of an existing project.

    This is the supported path to change a live project's objective — the API
    and MCP project surfaces are read-only.  Writes an audit row.
    Exits with code 1 if the project does not exist.
    """

    async def _set() -> None:
        from threadline_core.models import AuditLog, Project
        from threadline_core.utils.time import iso_now

        settings = Settings()
        async with _with_db(settings) as db:
            project = await db.get(Project, key)
            if project is None:
                typer.echo(f"Error: project '{key}' does not exist.", err=True)
                raise typer.Exit(code=1)
            project.current_objective = objective
            project.updated_at = iso_now()
            db.add(AuditLog(
                actor="cli",
                action="set_objective",
                detail=f"{key}: {objective}",
            ))
            await db.commit()
            typer.echo(f"Set objective for '{key}'.")

    _run(_set())


# ---------------------------------------------------------------------------
# threadline-core sessions reap / backfill-orphans
# ---------------------------------------------------------------------------


@sessions_app.command("reap")
def sessions_reap(
    max_idle_hours: float = typer.Option(
        24.0,
        "--max-idle-hours",
        help="Sessions idle longer than this are reaped.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="List what WOULD be reaped without changing anything.",
    ),
) -> None:
    """Close sessions stuck 'active' because their SessionEnd never fired.

    SessionEnd hooks do not run on crash, ``kill -9``, or reboot, so a session
    can linger as 'active' forever.  This sweeps any whose last activity is
    older than --max-idle-hours and marks them ended.  Run with --dry-run first.
    """

    async def _reap() -> None:
        from threadline_core.services.sessions import reap_stale_sessions

        settings = Settings()
        async with _with_db(settings) as db:
            ids = await reap_stale_sessions(
                db,
                max_idle_seconds=int(max_idle_hours * 3600),
                dry_run=dry_run,
            )
            if not dry_run:
                await db.commit()
            verb = "Would reap" if dry_run else "Reaped"
            typer.echo(f"{verb} {len(ids)} stale session(s).")
            for sid in ids:
                typer.echo(f"  {sid}")

    _run(_reap())


@sessions_app.command("backfill-orphans")
def sessions_backfill_orphans(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="List what WOULD be relabeled without changing anything.",
    ),
) -> None:
    """Relabel pre-#9 historical orphans (session_ended rows with no session).

    Idempotent and label-only.  Run with --dry-run first.
    """

    async def _backfill() -> None:
        from threadline_core.services.sessions import backfill_orphan_ends

        settings = Settings()
        async with _with_db(settings) as db:
            ids = await backfill_orphan_ends(db, dry_run=dry_run)
            if not dry_run:
                await db.commit()
            verb = "Would relabel" if dry_run else "Relabeled"
            typer.echo(f"{verb} {len(ids)} orphan-end event(s).")
            for eid in ids:
                typer.echo(f"  {eid}")

    _run(_backfill())


# ---------------------------------------------------------------------------
# threadline-core progress
# ---------------------------------------------------------------------------


@app.command()
def progress(
    project: str = typer.Argument(help="Project key to show momentum for."),
) -> None:
    """Show a project's momentum: open work, 7-day velocity, sessions, and noise.

    Pure aggregation over existing memory — no synthesis.
    Exits 1 if the project does not exist.
    """
    from threadline_core.services.progress import project_progress

    async def _progress() -> None:
        settings = Settings()
        async with _with_db(settings) as db:
            try:
                p = await project_progress(db, project)
            except LookupError:
                typer.echo(f"Unknown project: {project}", err=True)
                raise typer.Exit(code=1)

            sign = "+" if p.momentum >= 0 else ""
            typer.echo(p.project)
            typer.echo(
                f"  open loops: {p.open_loops}  (blockers: {p.blockers}, "
                f"hidden from feed: {p.bookkeeping_loops})"
            )
            typer.echo(
                f"  momentum (7d): {p.resolved_7d} closed / {p.opened_7d} opened"
                f"  (net {sign}{p.momentum})"
            )
            typer.echo(
                f"  sessions: {p.active_sessions} active, {p.ended_sessions} ended"
                f"  (incomplete: {p.incomplete_sessions})"
            )
            typer.echo(f"  orphan-end events: {p.orphan_ends}")
            typer.echo(f"  last activity: {p.last_activity or '—'}")

    _run(_progress())


# ---------------------------------------------------------------------------
# threadline-core log
# ---------------------------------------------------------------------------


@app.command()
def log(
    file: str | None = typer.Argument(
        None,
        help="Path to a JSON file (one event object or array).  "
             "Pass '-' or omit to read from stdin.",
    ),
) -> None:
    """Ingest one or more agent events from a JSON file or stdin.

    Accepts a single event object OR a JSON array of events.  Each event is
    validated against the AgentEventIn schema before being persisted.

    Exit code 1 on JSON parse errors or schema validation failures.
    """
    from pydantic import ValidationError

    from threadline_core.protocol import AgentEventIn
    from threadline_core.services.ingest import ingest_event

    if file is None or file == "-":
        raw = sys.stdin.read()
    else:
        path = Path(file)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            typer.echo(f"Error reading file: {exc}", err=True)
            raise typer.Exit(code=1) from exc

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        typer.echo(f"JSON parse error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if isinstance(parsed, dict):
        items = [parsed]
    elif isinstance(parsed, list):
        items = parsed
    else:
        typer.echo("Error: top-level JSON must be an object or array.", err=True)
        raise typer.Exit(code=1)

    events: list[AgentEventIn] = []
    for i, item in enumerate(items):
        try:
            events.append(AgentEventIn.model_validate(item))
        except ValidationError as exc:
            typer.echo(f"Validation error (event {i}): {exc}", err=True)
            raise typer.Exit(code=1) from exc

    async def _log() -> None:
        settings = Settings()
        async with _with_db(settings) as db:
            for event in events:
                result = await ingest_event(db, settings, event, actor="cli")
                typer.echo(
                    f"  id: {result.event_id}  "
                    f"decisions: {result.decisions_created}  "
                    f"open_loops: {result.open_loops_created}"
                )

    _run(_log())


# ---------------------------------------------------------------------------
# threadline-core note
# ---------------------------------------------------------------------------


@app.command()
def note(
    date: str | None = typer.Option(
        None,
        "--date",
        help="Date in YYYY-MM-DD format.  Defaults to today (UTC).",
    ),
) -> None:
    """Generate (or regenerate) the daily note for a date.

    Outputs raw Markdown to stdout — pipe-friendly.
    """
    from threadline_core.services.daily import generate_daily_note
    from threadline_core.utils.time import iso_now

    date_str: str = date if date is not None else iso_now()[:10]

    async def _note() -> None:
        settings = Settings()
        async with _with_db(settings) as db:
            try:
                daily = await generate_daily_note(db, date_str)
            except ValueError as exc:
                typer.echo(f"Error: {exc}", err=True)
                raise typer.Exit(code=1) from exc
            typer.echo(daily.markdown, nl=False)

    _run(_note())


# ---------------------------------------------------------------------------
# threadline-core search
# ---------------------------------------------------------------------------


@app.command()
def search(
    query: str = typer.Argument(help="Full-text search query."),
    project: str | None = typer.Option(
        None, "--project", help="Restrict results to this project key."
    ),
    limit: int = typer.Option(20, "--limit", help="Maximum results."),
    json_out: bool = typer.Option(False, "--json", help="Emit results as JSON."),
) -> None:
    """Full-text search across everything Threadline remembers.

    Searches events, decisions, open loops, daily notes, and context fragments.
    Results are ranked by BM25 relevance (best match first).
    """
    from threadline_core.services.search import search_memory

    async def _search() -> None:
        settings = Settings()
        async with _with_db(settings) as db:
            hits = await search_memory(db, query, project_key=project, limit=limit)

        if json_out:
            typer.echo(json.dumps([h.__dict__ for h in hits], indent=2))
            return

        if not hits:
            typer.echo("No results.")
            return

        for h in hits:
            scope = f"[{h.project_key}]" if h.project_key else ""
            typer.echo(f"{h.kind:16s} {scope:20s} {h.title}")
            typer.echo(f"  {h.snippet}")

    _run(_search())


# ---------------------------------------------------------------------------
# threadline-core export
# ---------------------------------------------------------------------------


@app.command()
def export(
    vault: str | None = typer.Option(
        None,
        "--vault",
        help=(
            "Output directory for the exported file tree.  "
            "Falls back to THREADLINE_VAULT_DIR env var.  "
            "Required if that var is not set."
        ),
    ),
) -> None:
    """Export Threadline Core memory as a static file tree (Obsidian-compatible).

    Writes raw governed-state only — no synthesis, no compiled memory, no
    Morning Pulse output.  An EXPORT_MANIFEST is written alongside every file
    so consumers know exactly what is and is not present.

    Idempotent — re-running overwrites existing files.
    Exit code 1 if no vault directory is configured.
    """
    from threadline_core.services.export import export_all

    async def _export() -> None:
        settings = Settings()
        out_dir: Path | None
        if vault is not None:
            out_dir = Path(vault)
        else:
            import os
            raw = os.environ.get("THREADLINE_VAULT_DIR")
            out_dir = Path(raw) if raw else None

        if out_dir is None:
            typer.echo(
                "Error: no vault directory configured.  "
                "Pass --vault or set THREADLINE_VAULT_DIR.",
                err=True,
            )
            raise typer.Exit(code=1)

        async with _with_db(settings) as db:
            report = await export_all(db, out_dir)

        typer.echo(f"root:     {report.root}")
        typer.echo(f"files:    {report.files_written}")
        typer.echo(f"projects: {report.projects_exported}")
        typer.echo(f"notes:    {report.daily_notes_exported}")

    _run(_export())


# ---------------------------------------------------------------------------
# threadline-core loop resolve
# ---------------------------------------------------------------------------


@loop_app.command("resolve")
def loop_resolve(
    loop_id: str = typer.Argument(..., help="Open-loop id to resolve."),
    evidence: list[str] = typer.Option(
        [], "--evidence", help="'<kind>:<id>' admissible evidence (repeatable)."
    ),
    operator: bool = typer.Option(
        False, "--operator", help="Operator override: resolve without evidence."
    ),
) -> None:
    """Operator: resolve an open loop with admissible evidence or --operator override."""
    from threadline_core.services import ingest as ingest_svc

    settings = Settings()

    async def _go() -> None:
        async with _with_db(settings) as db:
            try:
                loop = await ingest_svc.resolve_open_loop(
                    db, loop_id, actor="operator:cli",
                    evidence_refs=list(evidence), operator_confirmed=operator,
                )
            except (LookupError, ValueError) as e:
                typer.echo(f"Error: {e}", err=True)
                raise typer.Exit(code=1) from e
            await db.commit()
            typer.echo(json.dumps(loop, indent=2))

    _run(_go())


# ---------------------------------------------------------------------------
# threadline-core decision mark-wrong / mark / traps
# ---------------------------------------------------------------------------


def _mark_outcome_cli(
    decision_id: str, outcome: str, *, reason, corrected_rule,
    recurrence_guard, severity, evidence, applies_to,
) -> None:
    """Shared operator-path runner for the decision mark commands."""
    from threadline_core.services import decisions as decisions_svc

    settings = Settings()

    async def _go() -> None:
        async with _with_db(settings) as db:
            try:
                res = await decisions_svc.mark_decision_outcome(
                    db, decision_id=decision_id, outcome=outcome, reason=reason,
                    corrected_rule=corrected_rule, recurrence_guard=recurrence_guard,
                    severity=severity, evidence_refs=list(evidence),
                    applies_to=list(applies_to),
                    actor="operator:cli", operator_confirmed=True,
                )
            except (LookupError, ValueError) as e:
                typer.echo(f"Error: {e}", err=True)
                raise typer.Exit(code=1) from e
            typer.echo(json.dumps(res, indent=2))

    _run(_go())


@decision_app.command("mark-wrong")
def decision_mark_wrong(
    decision_id: str = typer.Argument(..., help="Decision id to mark incorrect."),
    reason: str | None = typer.Option(None, "--reason", help="Why it was wrong."),
    corrected_rule: str | None = typer.Option(None, "--corrected-rule", help="The lesson."),
    recurrence_guard: str | None = typer.Option(None, "--recurrence-guard"),
    severity: str | None = typer.Option(None, "--severity", help="low|medium|high|critical"),
    evidence: list[str] = typer.Option([], "--evidence", help="'<kind>:<id>' (repeatable)."),
    applies_to: list[str] = typer.Option([], "--applies-to", help="Where it applies (repeatable)."),
) -> None:
    """Operator: mark a decision INCORRECT (no evidence required on the operator path)."""
    _mark_outcome_cli(
        decision_id, "incorrect", reason=reason, corrected_rule=corrected_rule,
        recurrence_guard=recurrence_guard, severity=severity,
        evidence=evidence, applies_to=applies_to,
    )


@decision_app.command("mark")
def decision_mark(
    decision_id: str = typer.Argument(..., help="Decision id."),
    outcome: str = typer.Argument(..., help="validated|reverted|unresolved|accepted|incorrect"),
    reason: str | None = typer.Option(None, "--reason"),
    corrected_rule: str | None = typer.Option(None, "--corrected-rule"),
    recurrence_guard: str | None = typer.Option(None, "--recurrence-guard"),
    severity: str | None = typer.Option(None, "--severity"),
    evidence: list[str] = typer.Option([], "--evidence", help="'<kind>:<id>' (repeatable)."),
    applies_to: list[str] = typer.Option([], "--applies-to"),
) -> None:
    """Operator: mark a decision outcome (validated / reverted / unresolved / accepted)."""
    _mark_outcome_cli(
        decision_id, outcome, reason=reason, corrected_rule=corrected_rule,
        recurrence_guard=recurrence_guard, severity=severity,
        evidence=evidence, applies_to=applies_to,
    )


@decision_app.command("traps")
def decision_traps(
    project_key: str = typer.Argument(..., help="Project slug."),
) -> None:
    """Print known traps (decisions proven wrong) as JSON."""
    from threadline_core.services import decisions as decisions_svc

    settings = Settings()

    async def _go() -> None:
        async with _with_db(settings) as db:
            typer.echo(
                json.dumps(await decisions_svc.get_known_traps(db, project_key), indent=2)
            )

    _run(_go())


# ---------------------------------------------------------------------------
# threadline-core finding confirm / resolve / dismiss / list
# ---------------------------------------------------------------------------


def _finding_transition_cli(
    fn_name: str, finding_id: str, *, evidence, reason: str | None = None,
) -> None:
    """Shared operator-path runner for finding confirm/resolve/dismiss."""
    from threadline_core.services import findings as findings_svc

    settings = Settings()

    async def _go() -> None:
        async with _with_db(settings) as db:
            fn = getattr(findings_svc, fn_name)
            kwargs: dict = {
                "finding_id": finding_id,
                "evidence_refs": list(evidence),
                "actor": "operator:cli",
                "operator_confirmed": True,
            }
            if reason is not None:
                kwargs["reason"] = reason
            try:
                res = await fn(db, **kwargs)
            except (LookupError, ValueError) as e:
                typer.echo(f"Error: {e}", err=True)
                raise typer.Exit(code=1) from e
            typer.echo(json.dumps(res, indent=2))

    _run(_go())


@finding_app.command("confirm")
def finding_confirm(
    finding_id: str = typer.Argument(..., help="Finding id to confirm."),
    evidence: list[str] = typer.Option([], "--evidence", help="'<kind>:<id>' (repeatable)."),
) -> None:
    """Operator: confirm a finding (no evidence required on the operator path)."""
    _finding_transition_cli("confirm_finding", finding_id, evidence=evidence)


@finding_app.command("resolve")
def finding_resolve(
    finding_id: str = typer.Argument(..., help="Finding id to resolve."),
    evidence: list[str] = typer.Option([], "--evidence", help="'<kind>:<id>' (repeatable)."),
) -> None:
    """Operator: resolve a finding (its resolution condition was met)."""
    _finding_transition_cli("resolve_finding", finding_id, evidence=evidence)


@finding_app.command("dismiss")
def finding_dismiss(
    finding_id: str = typer.Argument(..., help="Finding id to dismiss."),
    reason: str | None = typer.Option(None, "--reason", help="Why it's invalid/irrelevant."),
    evidence: list[str] = typer.Option([], "--evidence", help="'<kind>:<id>' (repeatable)."),
) -> None:
    """Operator: dismiss a finding as invalid/irrelevant."""
    _finding_transition_cli("dismiss_finding", finding_id, evidence=evidence, reason=reason)


@finding_app.command("list")
def finding_list(
    project_key: str = typer.Argument(..., help="Project slug."),
    finding_class: str | None = typer.Option(None, "--class", help="gap|caveat"),
    status: list[str] = typer.Option([], "--status", help="proposed|confirmed|… (repeatable)."),
) -> None:
    """Print findings as JSON (filter by --class and --status)."""
    from threadline_core.services import findings as findings_svc

    settings = Settings()

    async def _go() -> None:
        async with _with_db(settings) as db:
            res = await findings_svc.get_findings(
                db, project_key, finding_class=finding_class,
                status=list(status) or None,
            )
            typer.echo(json.dumps(res, indent=2))

    _run(_go())


# ---------------------------------------------------------------------------
# threadline-core connect claude-code
# ---------------------------------------------------------------------------


@connect_app.command("claude-code")
def connect_claude_code(
    project_root: Path = typer.Argument(..., help="Project root to scaffold into."),
    project_key: str = typer.Option(
        ..., "--project-key", help="Threadline project key for this project."
    ),
    data_dir: Path = typer.Option(
        None, "--data-dir", help="THREADLINE_DATA_DIR override."
    ),
    endpoint: str | None = typer.Option(
        None,
        "--endpoint",
        help="Threadline API endpoint.  Defaults to http://{host}:{port}/api/events.",
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite existing connector files."
    ),
    allow_gh_cli: bool = typer.Option(
        False,
        "--allow-gh-cli",
        help="Add read-only gh subcommand approvals to settings.json (gh auth stays blocked).",
    ),
) -> None:
    """Scaffold a Claude Code project connector for Threadline.

    Writes ``.mcp.json``, ``.claude/settings.json``, and
    ``.threadline/CLAUDE-snippet.md`` into the project root.
    """
    from threadline_core.services.connector_scaffold import (
        ClaudeCodeConnectorOptions,
        write_connector_files,
    )

    settings = Settings()
    resolved_data_dir = data_dir if data_dir is not None else settings.data_dir
    resolved_endpoint = (
        endpoint or f"http://{settings.host}:{settings.port}/api/events"
    )
    options = ClaudeCodeConnectorOptions(
        project_root=project_root,
        project_key=project_key,
        data_dir=resolved_data_dir,
        endpoint=resolved_endpoint,
        force=force,
        allow_gh_cli=allow_gh_cli,
    )
    try:
        written = write_connector_files(options)
    except FileExistsError as exc:
        typer.echo(f"Error: {exc}.  Re-run with --force to replace it.", err=True)
        raise typer.Exit(code=1) from exc
    except NotADirectoryError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Scaffolded Claude Code connector in {project_root}")
    for path in written:
        typer.echo(f"+ {path.as_posix()}")
