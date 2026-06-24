"""First-class findings — durable gaps and caveats (Slice 2 of the retrieval redesign).

A finding is something the trusted context must carry beyond live decisions and
known traps:

- **gap** — something required is MISSING, unknown, or unproven   → ``critical_gaps``
- **caveat** — something is true/usable only under a LIMITING CONDITION → ``active_caveats``

Everything else (deferred-verification, operational-drift, security-constraint, …)
is a ``category``, not a class. *Risk is not a class* — it is the ``impact`` of a gap
or caveat. See ``docs/plans/2026-06-17-slice2-findings-gaps-caveats-design.md``.

Hybrid zero-migration storage (mirrors ``decisions.py`` / ``pulse_store`` /
``research_store``): each finding is a ``project_memories`` row, ``kind="finding"``.
A lifecycle transition writes a NEW row and supersedes the prior one for the same
``finding_id`` — exactly one ``ProjectMemory.status == "active"`` row per finding_id
is its current state; the body's ``status`` carries the semantic lifecycle state.

THE TRUST GATE + ADMISSIBILITY (the load-bearing rule)
------------------------------------------------------
Agents may PROPOSE freely (``status="proposed"``; never surfaced in the trusted
bundle). A finding may be CONFIRMED / RESOLVED / DISMISSED only with admissible
same-project evidence OR an explicit operator action (CLI). "Admissible" is STRICTER
than the decision gate: the cited record must be INDEPENDENTLY evidence-bearing — not
a restatement of the claim. This closes the circular path
``propose gap → write a loop repeating it → cite that loop → self-confirm``.
The MCP wrapper hard-codes ``operator_confirmed=False``.

KNOWN RESIDUAL (do not overstate): a structurally evidence-bearing ``agent_event``
(non-empty ``verification`` metadata) does not PROVE its content is independently
true. Slice 2 prevents the obvious self-reference path; semantic evidence validation
is future work.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from threadline_core.models import AgentEvent, AuditLog, ProjectMemory
from threadline_core.services.decisions import (
    DECISION_OUTCOME_KIND,
    _resolve_evidence_ref,
    get_decision_outcome,
)
from threadline_core.services.sanitize import detect_secret as _detect_secret
from threadline_core.services.sanitize import redact_pii
from threadline_core.utils.ids import new_id
from threadline_core.utils.time import iso_now

# ponytail: _objective_overlap inlined — avoid pulling in the private ranking engine
MORNING_PULSE_KIND = "morning_pulse"  # record kind constant, producer is private
RESEARCH_BRIEF_KIND = "research_brief"  # record kind constant, producer is private

FINDING_KIND = "finding"
SCHEMA_VERSION = 1

ALLOWED_CLASSES = {"gap", "caveat"}
ALLOWED_SEVERITIES = {"low", "medium", "high", "critical"}
ALLOWED_STATUSES = {"proposed", "confirmed", "resolved", "dismissed", "superseded"}
# Transitions that assert a hard judgement → need admissible evidence or an operator.
GATED_TRANSITIONS = {"confirmed", "resolved", "dismissed"}
# Only gaps at/above this floor enter critical_gaps (a medium gap may surface only
# when strongly relevant to a task_hint; see ``bundle_sections``).
CRITICAL_GAP_FLOOR = {"high", "critical"}

_SEVERITY_RANK = {"critical": 3, "high": 2, "medium": 1, "low": 0}

# Controlled-but-extensible category vocabulary. A novel category is accepted (so the
# vocabulary grows deliberately) but callers should prefer these.
GAP_CATEGORIES = {
    "deferred-verification", "missing-capability", "missing-evidence",
    "unverified-claim", "incomplete-implementation", "data-quality",
    "unknown-root-cause",
}
CAVEAT_CATEGORIES = {
    "operational-drift", "undocumented-dependency", "external-configuration",
    "environment-assumption", "partial-coverage", "known-limitation",
    "security-constraint",
}


# --- fingerprint / serialization ------------------------------------------- #


_TOKENS_RE = re.compile(r"[\w]+")


def _objective_overlap(text: str, objective: str | None) -> float:
    """Fraction of objective tokens present in text (0..1); 0 when no objective set."""
    if not objective:
        return 0.0
    obj_tokens = set(_TOKENS_RE.findall(objective.lower()))
    if not obj_tokens:
        return 0.0
    text_tokens = set(_TOKENS_RE.findall(text.lower()))
    return len(text_tokens & obj_tokens) / len(obj_tokens)


def _normalize(text: str) -> str:
    """Lowercase, collapse whitespace, strip surrounding punctuation (deterministic)."""
    return re.sub(r"\s+", " ", (text or "").strip().lower()).strip(".,;:!?\"'-—  ")


def _fingerprint(project_key: str, finding_class: str, category: str, statement: str) -> str:
    """Stable dedup key over normalized project|class|category|statement.

    Limitation: exact-after-normalization — a reworded statement will NOT dedup.
    """
    raw = "|".join((_normalize(project_key), finding_class, _normalize(category),
                    _normalize(statement)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _serialize_finding(body: dict[str, Any]) -> str:
    """First line is the machine-matchable header (supersede/lookup key); the rest is
    the full finding dict as JSON (mirrors ``decisions._serialize_outcome``)."""
    header = (
        f"source_ref=finding:{body['finding_id']} status={body['status']} "
        f"class={body['class']} severity={body.get('severity') or ''} "
        f"fingerprint={body['fingerprint']}"
    )
    return header + "\n" + json.dumps(body, ensure_ascii=False)


def _parse_finding(row: ProjectMemory) -> dict[str, Any]:
    body = row.content.split("\n", 1)[1] if "\n" in row.content else "{}"
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        data = {}
    data["memory_id"] = row.id
    data.setdefault("created_at", row.created_at)
    return data


# --- row access ------------------------------------------------------------- #

async def _active_rows(db: AsyncSession, project_key: str) -> list[ProjectMemory]:
    return list((await db.execute(
        select(ProjectMemory).where(
            ProjectMemory.project_key == project_key,
            ProjectMemory.kind == FINDING_KIND,
            ProjectMemory.status == "active",
        ).order_by(ProjectMemory.created_at.desc())
    )).scalars().all())


async def _current_row(db: AsyncSession, finding_id: str) -> ProjectMemory | None:
    """The single active row for a finding_id (its current state), or None."""
    needle = f"source_ref=finding:{finding_id} "
    rows = (await db.execute(
        select(ProjectMemory).where(
            ProjectMemory.kind == FINDING_KIND,
            ProjectMemory.status == "active",
        ).order_by(ProjectMemory.created_at.desc())
    )).scalars().all()
    for row in rows:
        if (row.content or "").startswith(needle):
            return row
    return None


# --- admissibility (the anti-circular-evidence tier) ------------------------ #

def _research_brief_has_sources(row: ProjectMemory) -> bool:
    return "\nsources:" in (row.content or "")


async def _is_admissible_evidence(db: AsyncSession, kind: str, rid: str) -> bool:
    """True iff the (already-resolved, same-project) ref is INDEPENDENTLY
    evidence-bearing — not a restatement of the claim it would confirm.

    Resolvability / same-project / shape are enforced upstream by
    ``decisions._resolve_evidence_ref``; this adds the content-bearing tier.
    """
    if kind in ("agent_event", "event"):
        row = await db.get(AgentEvent, rid)
        if row is None:
            return False
        try:
            details = json.loads(row.details_json or "{}")
        except json.JSONDecodeError:
            return False
        # Evidence-bearing iff it captured a verification / command / test / CI result.
        return bool(
            details.get("verification") or details.get("output")
            or details.get("test_results") or details.get("captured_output")
            or details.get("ci")
        )
    if kind in ("open_loop", "loop"):
        return False  # a loop restates a claim; it never proves one
    if kind == "decision":
        outcome = await get_decision_outcome(db, rid)
        return bool(outcome and outcome.get("evidence_refs"))  # evidence-backed only
    if kind == "pulse":
        return True  # _resolve_evidence_ref already forced kind == morning_pulse
    if kind == "project_memory":
        row = await db.get(ProjectMemory, rid)
        if row is None:
            return False
        if row.kind == FINDING_KIND:
            return False  # no finding-confirms-finding (incl. itself) — breaks the chain
        if row.kind == MORNING_PULSE_KIND:
            return True
        if row.kind == RESEARCH_BRIEF_KIND:
            return _research_brief_has_sources(row)
        if row.kind == DECISION_OUTCOME_KIND:
            try:
                b = json.loads(row.content.split("\n", 1)[1])
            except (IndexError, json.JSONDecodeError):
                return False
            return bool(b.get("evidence_refs"))
        return False  # free-form note kinds are inadmissible
    return False


async def _gate(
    db: AsyncSession, *, transition: str, evidence_refs: list[str] | None,
    operator_confirmed: bool, project_key: str,
) -> list[str]:
    """Enforce the trust gate + admissibility tier. Returns the cleaned refs."""
    refs = [r.strip() for r in (evidence_refs or []) if r and r.strip()]
    if transition in GATED_TRANSITIONS and not operator_confirmed:
        if not refs:
            raise ValueError(
                f"a finding may be marked {transition!r} only with admissible same-project "
                "evidence (a record that independently bears it out) or explicit operator "
                "confirmation; the model must never self-confirm from confidence alone. Pass "
                "evidence_refs like ['agent_event:<id>'] (verification-bearing), or use the "
                "`threadline finding` CLI."
            )
        for ref in refs:
            await _resolve_evidence_ref(db, ref, project_key=project_key, decision_id="")
            kind, _, rid = ref.partition(":")
            if not await _is_admissible_evidence(db, kind.strip(), rid.strip()):
                raise ValueError(
                    f"evidence_ref {ref!r} is not independently evidence-bearing — an open "
                    "loop, a bare agent_event, or another finding (incl. itself) cannot "
                    "confirm a finding. Cite a verification-bearing event, an evidence-backed "
                    "pulse/decision, or a sourced research_brief."
                )
    else:
        # Operator path or ungated: still validate that any supplied refs resolve.
        for ref in refs:
            await _resolve_evidence_ref(db, ref, project_key=project_key, decision_id="")
    return refs


# --- lifecycle -------------------------------------------------------------- #

async def propose_finding(
    db: AsyncSession, *, project_key: str, finding_class: str, category: str,
    statement: str, severity: str, impact: str | None = None,
    resolution_condition: str | None = None, source_session_id: str | None = None,
    actor: str = "agent", now: str | None = None,
) -> dict[str, Any]:
    """Create a proposed finding (free; never surfaced in the trusted bundle).

    Fingerprint dedup: a match against an ACTIVE proposed/confirmed finding returns
    that finding's id and writes nothing; a match against a RESOLVED finding creates
    a NEW finding with ``regression=True``; otherwise a new finding is created.
    Rejects an unknown class/severity or a statement that carries an obvious secret.
    """
    finding_class = (finding_class or "").strip().lower()
    if finding_class not in ALLOWED_CLASSES:
        raise ValueError(f"class must be one of {sorted(ALLOWED_CLASSES)} (got {finding_class!r}).")
    severity = (severity or "").strip().lower()
    if severity not in ALLOWED_SEVERITIES:
        raise ValueError(
            f"severity must be one of {sorted(ALLOWED_SEVERITIES)} (got {severity!r}).")
    category = (category or "").strip()
    statement = (statement or "").strip()
    if not statement:
        raise ValueError("statement must be non-empty.")

    iso = now or iso_now()
    fp = _fingerprint(project_key, finding_class, category, statement)

    matches = [m for m in (_parse_finding(r) for r in await _active_rows(db, project_key))
               if m.get("fingerprint") == fp]
    live = next((m for m in matches if m.get("status") in ("proposed", "confirmed")), None)
    if live is not None:
        return {
            "finding_id": live["finding_id"], "status": live["status"],
            "class": finding_class, "category": category,
            "severity": live.get("severity"), "fingerprint": fp,
            "regression": live.get("regression", False), "created": False,
        }
    regression = any(m.get("status") == "resolved" for m in matches)

    finding_id = new_id()
    body = {
        "schema_version": SCHEMA_VERSION, "finding_id": finding_id, "class": finding_class,
        "category": category, "statement": statement, "impact": impact,
        "resolution_condition": resolution_condition, "severity": severity,
        "status": "proposed", "evidence_refs": [], "fingerprint": fp,
        "regression": regression, "source_session_id": source_session_id,
        "created_by": actor, "confirmed_by": None, "supersedes_id": None,
        "created_at": iso, "updated_at": iso,
    }
    content = _serialize_finding(body)
    if _detect_secret(content) is not None:
        raise ValueError(
            "finding appears to contain a secret/credential and was rejected (durable "
            "memory). Record the lesson, never the secret itself."
        )
    content, _ = redact_pii(content)  # deterministic PII redaction (after secret reject)
    db.add(ProjectMemory(id=new_id(), project_key=project_key, kind=FINDING_KIND,
                         content=content, status="active", created_at=iso, updated_at=iso))
    db.add(AuditLog(actor=actor, action="propose_finding",
                    detail=f"{finding_id} {finding_class}/{category}"))
    await db.commit()
    return {
        "finding_id": finding_id, "status": "proposed", "class": finding_class,
        "category": category, "severity": severity, "fingerprint": fp,
        "regression": regression, "created": True,
    }


async def _apply_transition(
    db: AsyncSession, *, finding_id: str, new_status: str, evidence_refs: list[str] | None,
    operator_confirmed: bool, actor: str, now: str | None, extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = await _current_row(db, finding_id)
    if row is None:
        raise LookupError(f"Finding not found: {finding_id!r}")
    body = _parse_finding(row)
    project_key = row.project_key
    refs = await _gate(db, transition=new_status, evidence_refs=evidence_refs,
                       operator_confirmed=operator_confirmed, project_key=project_key)
    iso = now or iso_now()

    new_body = {k: v for k, v in body.items() if k != "memory_id"}
    new_body["status"] = new_status
    new_body["evidence_refs"] = refs or new_body.get("evidence_refs", [])
    if new_status == "confirmed":
        new_body["confirmed_by"] = "operator" if operator_confirmed else actor
    new_body["updated_at"] = iso
    if extra:
        new_body.update(extra)
    content = _serialize_finding(new_body)
    if _detect_secret(content) is not None:
        raise ValueError("finding outcome appears to contain a secret/credential and was rejected.")

    content, _ = redact_pii(content)  # deterministic PII redaction (after secret reject)
    row.status = "superseded"
    row.updated_at = iso
    db.add(ProjectMemory(id=new_id(), project_key=project_key, kind=FINDING_KIND,
                         content=content, status="active", created_at=iso, updated_at=iso))
    db.add(AuditLog(actor=actor, action=f"{new_status}_finding", detail=finding_id))
    await db.commit()
    return {"finding_id": finding_id, "status": new_status, "evidence_refs": refs,
            "marked_by": actor, "updated_at": iso}


async def confirm_finding(
    db: AsyncSession, *, finding_id: str, evidence_refs: list[str] | None = None,
    operator_confirmed: bool = False, actor: str = "agent", now: str | None = None,
) -> dict[str, Any]:
    """Promote a finding to ``confirmed`` (surfaced in the trusted bundle). Gated."""
    return await _apply_transition(
        db, finding_id=finding_id, new_status="confirmed", evidence_refs=evidence_refs,
        operator_confirmed=operator_confirmed, actor=actor, now=now)


async def resolve_finding(
    db: AsyncSession, *, finding_id: str, evidence_refs: list[str] | None = None,
    operator_confirmed: bool = False, actor: str = "agent", now: str | None = None,
) -> dict[str, Any]:
    """Mark a finding ``resolved`` (its resolution_condition was met). Gated; drops it
    out of the trusted bundle."""
    return await _apply_transition(
        db, finding_id=finding_id, new_status="resolved", evidence_refs=evidence_refs,
        operator_confirmed=operator_confirmed, actor=actor, now=now)


async def dismiss_finding(
    db: AsyncSession, *, finding_id: str, evidence_refs: list[str] | None = None,
    reason: str | None = None, operator_confirmed: bool = False,
    actor: str = "agent", now: str | None = None,
) -> dict[str, Any]:
    """Mark a finding ``dismissed`` (invalid/irrelevant). Gated; drops it out of the bundle."""
    return await _apply_transition(
        db, finding_id=finding_id, new_status="dismissed", evidence_refs=evidence_refs,
        operator_confirmed=operator_confirmed, actor=actor, now=now,
        extra={"dismiss_reason": reason} if reason else None)


async def supersede_finding(
    db: AsyncSession, *, old_finding_id: str, new_finding_id: str,
    operator_confirmed: bool = False, actor: str = "agent", now: str | None = None,
) -> dict[str, Any]:
    """Replace ``old`` with ``new``. INVARIANT: a proposed replacement may never hide a
    confirmed finding — ``new`` must already be ``confirmed`` unless an operator approves.
    """
    old = await _current_row(db, old_finding_id)
    if old is None:
        raise LookupError(f"Finding not found: {old_finding_id!r}")
    new = await _current_row(db, new_finding_id)
    if new is None:
        raise LookupError(f"Finding not found: {new_finding_id!r}")
    new_body = _parse_finding(new)
    if new_body.get("status") != "confirmed" and not operator_confirmed:
        raise ValueError(
            "a proposed/unconfirmed finding may not supersede another finding unless it is "
            "itself confirmed or an operator approves — a proposed replacement must never hide "
            "a confirmed finding."
        )
    iso = now or iso_now()

    old_body = {k: v for k, v in _parse_finding(old).items() if k != "memory_id"}
    old_body["status"] = "superseded"
    old_body["superseded_by"] = new_finding_id
    old_body["updated_at"] = iso
    old.status = "superseded"
    old.updated_at = iso
    db.add(ProjectMemory(id=new_id(), project_key=old.project_key, kind=FINDING_KIND,
                         content=_serialize_finding(old_body), status="active",
                         created_at=iso, updated_at=iso))

    linked = {k: v for k, v in new_body.items() if k != "memory_id"}
    linked["supersedes_id"] = old_finding_id
    linked["updated_at"] = iso
    new.status = "superseded"
    new.updated_at = iso
    db.add(ProjectMemory(id=new_id(), project_key=new.project_key, kind=FINDING_KIND,
                         content=_serialize_finding(linked), status="active",
                         created_at=iso, updated_at=iso))
    db.add(AuditLog(actor=actor, action="supersede_finding",
                    detail=f"{old_finding_id} -> {new_finding_id}"))
    await db.commit()
    return {"old_finding_id": old_finding_id, "new_finding_id": new_finding_id,
            "status": "superseded"}


# --- retrieval -------------------------------------------------------------- #

def _rank_findings(items: list[dict[str, Any]], task_hint: str | None) -> list[dict[str, Any]]:
    """Rank by task-hint relevance (if any), then severity, then recency."""
    scored: list[tuple[dict[str, Any], int, str, float]] = []
    for i in items:
        text = " ".join(x for x in (i.get("statement"), i.get("impact"),
                                    i.get("category")) if x)
        scored.append((
            i, _SEVERITY_RANK.get((i.get("severity") or "").lower(), -1),
            i.get("created_at") or "",
            _objective_overlap(text, task_hint) if task_hint else 0.0,
        ))
    scored.sort(key=lambda t: t[2], reverse=True)   # recency
    scored.sort(key=lambda t: t[1], reverse=True)   # severity
    if task_hint:
        scored.sort(key=lambda t: t[3], reverse=True)  # overlap dominates
    return [t[0] for t in scored]


async def get_finding(db: AsyncSession, finding_id: str) -> dict[str, Any] | None:
    """The current state of one finding (parsed body), or None."""
    row = await _current_row(db, finding_id)
    return _parse_finding(row) if row else None


async def get_findings(
    db: AsyncSession, project_key: str, *, finding_class: str | None = None,
    status: str | list[str] | None = None, minimum_severity: str | None = None,
    task_hint: str | None = None,
) -> list[dict[str, Any]]:
    """List current findings, filtered and ranked. Progressive-disclosure surface."""
    items = [_parse_finding(r) for r in await _active_rows(db, project_key)]
    if finding_class:
        items = [i for i in items if i.get("class") == finding_class]
    if status is not None:
        wanted = {status} if isinstance(status, str) else set(status)
        items = [i for i in items if i.get("status") in wanted]
    if minimum_severity:
        floor = _SEVERITY_RANK.get(minimum_severity.lower(), 0)
        items = [i for i in items
                 if _SEVERITY_RANK.get((i.get("severity") or "").lower(), -1) >= floor]
    return _rank_findings(items, task_hint)


def _render(i: dict[str, Any]) -> dict[str, Any]:
    return {"summary": i.get("statement"), "finding_id": i.get("finding_id"),
            "severity": i.get("severity")}


def _render_pulse(i: dict[str, Any]) -> dict[str, Any]:
    """Richer render for the Morning Pulse: ``_render`` plus ``impact`` (the practical
    implication — rule #4). Still summaries + ids only; the full statement/evidence stay
    behind ``get_finding``/``get_evidence`` (progressive disclosure)."""
    return {"summary": i.get("statement"), "finding_id": i.get("finding_id"),
            "severity": i.get("severity"), "impact": i.get("impact")}


def _select_confirmed(
    items: list[dict[str, Any]], task_hint: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Shared selection for BOTH the context bundle and the Morning Pulse so the Slice 2
    severity floor + task-hint rule lives in exactly one place.

    Returns ``(surfaced_gaps, surfaced_caveats, proposed)``. A gap surfaces at the
    high|critical floor, or when it is medium AND its statement overlaps ``task_hint``;
    a caveat surfaces once confirmed. Proposed findings are returned separately (callers
    only ever count them — never render their text).
    """
    confirmed = [i for i in items if i.get("status") == "confirmed"]
    proposed = [i for i in items if i.get("status") == "proposed"]
    gaps = [
        i for i in confirmed if i.get("class") == "gap" and (
            (i.get("severity") or "").lower() in CRITICAL_GAP_FLOOR
            or (task_hint and (i.get("severity") or "").lower() == "medium"
                and _objective_overlap(i.get("statement") or "", task_hint) > 0)
        )
    ]
    caveats = [i for i in confirmed if i.get("class") == "caveat"]
    return gaps, caveats, proposed


def _proposed_counts(proposed: list[dict[str, Any]]) -> dict[str, int]:
    return {"gaps": sum(1 for i in proposed if i.get("class") == "gap"),
            "caveats": sum(1 for i in proposed if i.get("class") == "caveat")}


async def bundle_sections(
    db: AsyncSession, project_key: str, *, task_hint: str | None = None, cap: int = 3,
) -> dict[str, Any]:
    """Pre-shaped finding sections for ``build_context_bundle``:

    - ``critical_gaps``: confirmed gaps at high|critical (a medium gap surfaces only
      when ``task_hint`` overlaps it), ranked, capped.
    - ``active_caveats``: all confirmed caveats, ranked, capped.
    - ``proposed_findings_count``: ``{"gaps": n, "caveats": m}`` (count only — proposed
      statements never enter the trusted bundle).
    """
    items = [_parse_finding(r) for r in await _active_rows(db, project_key)]
    gaps, caveats, proposed = _select_confirmed(items, task_hint)
    return {
        "critical_gaps": [_render(i) for i in _rank_findings(gaps, task_hint)[:cap]],
        "active_caveats": [_render(i) for i in _rank_findings(caveats, task_hint)[:cap]],
        "proposed_findings_count": _proposed_counts(proposed),
    }


async def pulse_findings(
    db: AsyncSession, project_key: str, *, task_hint: str | None = None, cap: int = 3,
) -> dict[str, Any]:
    """Confirmed findings shaped for the Morning Pulse render — the read-time overlay
    source. Same selection, severity floor, task-hint ranking and caps as
    ``bundle_sections`` (the Slice 2 contract), plus ``impact``.

    Read-only: never mutates a finding (the Pulse RENDERS confirmed structured findings,
    it does not invent or re-judge them). Proposed findings contribute only to
    ``proposed_findings_count`` — their statements never enter the rendered Pulse.
    """
    items = [_parse_finding(r) for r in await _active_rows(db, project_key)]
    gaps, caveats, proposed = _select_confirmed(items, task_hint)
    return {
        "critical_gaps": [_render_pulse(i) for i in _rank_findings(gaps, task_hint)[:cap]],
        "active_caveats": [_render_pulse(i) for i in _rank_findings(caveats, task_hint)[:cap]],
        "proposed_findings_count": _proposed_counts(proposed),
    }
