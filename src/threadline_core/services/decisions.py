"""Decision-quality ledger — record the real-world OUTCOME of a past decision and
surface decisions proven wrong as "known traps" so the next agent does not repeat
them.

Hybrid, zero-migration storage (mirrors the ``project_memories`` spine used by
``research_store``/``pulse_store``):

- ``Decision.status`` (a free-text column) gains a richer vocabulary — beyond the
  legacy ``active``/``superseded`` it may now be ``accepted``, ``validated``,
  ``incorrect``, ``reverted``, or ``unresolved``. No schema change.
- The rich correction detail (why it was wrong, the corrected rule, severity,
  where the lesson applies, the contradicting evidence) lives in a new
  ``ProjectMemory`` row, ``kind="decision_outcome"``, JSON body carrying
  ``decision_id`` explicitly. One active outcome per decision (supersede on rerun).

THE STRICT GATE (the load-bearing rule)
---------------------------------------
A decision may be marked ``incorrect``/``reverted``/``validated`` ONLY with
evidence that resolves to a real record, OR via an explicit operator action. The
model must NEVER self-label a decision wrong from its own confidence. Because the
model is the caller of the MCP tool, this is enforced in the service, not a
docstring: the evidence path requires non-empty ``evidence_refs`` that each
resolve to an existing row of the named kind IN THE SAME PROJECT; the operator
path (``operator_confirmed=True``) is reachable only from the CLI, which the MCP
wrapper hard-codes to ``False``.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from threadline_core.models import AgentEvent, AuditLog, Decision, OpenLoop, ProjectMemory
from threadline_core.services.sanitize import (
    detect_secret as _detect_secret,  # moved from research_store in public extraction
)
from threadline_core.services.sanitize import redact_pii
from threadline_core.utils.ids import new_id
from threadline_core.utils.time import iso_now

DECISION_OUTCOME_KIND = "decision_outcome"

# Decision.status vocabulary. "active" stays valid (legacy/default live state).
ALLOWED_OUTCOMES = {"accepted", "validated", "incorrect", "reverted", "unresolved"}
# Outcomes that assert a hard judgement and therefore need evidence or an operator.
GATED_OUTCOMES = {"validated", "incorrect", "reverted"}
_OUTCOME_TO_STATUS = {o: o for o in ALLOWED_OUTCOMES}
ALLOWED_SEVERITIES = {"low", "medium", "high", "critical"}

# Statuses get_decisions treats as "live decisions to honour" (NOT traps/history).
LIVE_STATUSES = ("active", "accepted", "validated")

# Evidence-ref kinds -> (model, required ProjectMemory.kind or None).
_EVIDENCE_MODELS: dict[str, tuple[type, str | None]] = {
    "agent_event": (AgentEvent, None),
    "open_loop": (OpenLoop, None),
    "decision": (Decision, None),
    "project_memory": (ProjectMemory, None),
    "pulse": (ProjectMemory, "morning_pulse"),
}


async def _resolve_evidence_ref(
    db: AsyncSession, ref: str, *, project_key: str, decision_id: str
) -> None:
    """Raise ValueError unless ``ref`` is ``'<kind>:<id>'`` resolving to an existing
    row of that kind in the SAME project. Rejects a self-referential decision ref."""
    if not isinstance(ref, str) or ":" not in ref:
        raise ValueError(f"evidence_ref must be '<kind>:<id>' (got {ref!r}).")
    kind, _, rid = ref.partition(":")
    kind, rid = kind.strip(), rid.strip()
    if kind not in _EVIDENCE_MODELS:
        raise ValueError(
            f"evidence_ref kind {kind!r} unknown; allowed: {sorted(_EVIDENCE_MODELS)}."
        )
    if not rid:
        raise ValueError(f"evidence_ref {ref!r} is missing an id.")
    if kind == "decision" and rid == decision_id:
        raise ValueError("a decision cannot be its own contradicting evidence.")
    model, want_kind = _EVIDENCE_MODELS[kind]
    row = await db.get(model, rid)
    if row is None:
        raise ValueError(f"evidence_ref {ref!r} does not resolve to an existing record.")
    if want_kind is not None and getattr(row, "kind", None) != want_kind:
        raise ValueError(f"evidence_ref {ref!r} must reference a {want_kind} row.")
    if getattr(row, "project_key", None) != project_key:
        raise ValueError(f"evidence_ref {ref!r} belongs to a different project.")


async def _gate(
    db: AsyncSession, *, outcome: str, evidence_refs: list[str],
    operator_confirmed: bool, project_key: str, decision_id: str,
) -> None:
    """Enforce the anti-self-label gate. Raises ValueError on any violation."""
    if outcome in GATED_OUTCOMES and not operator_confirmed and not evidence_refs:
        raise ValueError(
            f"a decision may be marked {outcome!r} only with evidence (a record that "
            "contradicts it) or explicit operator confirmation; the model must never "
            "self-label from confidence alone. Pass evidence_refs like "
            "['agent_event:<id>'], or use the `threadline decision` CLI."
        )
    # Validate every supplied ref regardless of path (operator may still cite some).
    for ref in evidence_refs:
        await _resolve_evidence_ref(db, ref, project_key=project_key, decision_id=decision_id)


def _serialize_outcome(*, source_ref: str, outcome: str, severity: str | None,
                       marked_at: str, body: dict[str, Any]) -> str:
    """First line is the machine-matchable header (supersede/lookup key); the rest
    is the full outcome dict as JSON (mirrors ``pulse_store._serialize``)."""
    header = (
        f"source_ref={source_ref} outcome={outcome} "
        f"severity={severity or ''} marked_at={marked_at}"
    )
    return header + "\n" + json.dumps(body, ensure_ascii=False)


def _parse_outcome(row: ProjectMemory) -> dict[str, Any]:
    """Parse a decision_outcome row's JSON body; attach memory id + created_at."""
    body = row.content.split("\n", 1)[1] if "\n" in row.content else "{}"
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        data = {}
    data["memory_id"] = row.id
    data.setdefault("created_at", row.created_at)
    return data


async def mark_decision_outcome(
    db: AsyncSession,
    *,
    decision_id: str,
    outcome: str,
    reason: str | None = None,
    corrected_rule: str | None = None,
    recurrence_guard: str | None = None,
    severity: str | None = None,
    evidence_refs: list[str] | None = None,
    applies_to: list[str] | None = None,
    actor: str = "api",
    operator_confirmed: bool = False,
    now: str | None = None,
) -> dict[str, Any]:
    """Record the real-world outcome of a past decision.

    Atomically: validate inputs, load the Decision (LookupError if absent), enforce
    the gate, flip ``Decision.status`` to the outcome, supersede the prior active
    ``decision_outcome`` for this decision, insert the new one, write an AuditLog,
    and commit. Returns a summary dict. Raises ValueError on a bad outcome/severity,
    a failed gate, or apparent secret content.
    """
    outcome = (outcome or "").strip().lower()
    if outcome not in ALLOWED_OUTCOMES:
        raise ValueError(f"outcome must be one of {sorted(ALLOWED_OUTCOMES)} (got {outcome!r}).")
    if severity is not None:
        severity = severity.strip().lower()
        if severity not in ALLOWED_SEVERITIES:
            raise ValueError(
                f"severity must be one of {sorted(ALLOWED_SEVERITIES)} (got {severity!r})."
            )
    evidence_refs = [r.strip() for r in (evidence_refs or []) if r and r.strip()]
    applies_to = [a.strip() for a in (applies_to or []) if a and a.strip()]

    decision = await db.get(Decision, decision_id)
    if decision is None:
        raise LookupError(f"Decision not found: {decision_id!r}")

    await _gate(
        db, outcome=outcome, evidence_refs=evidence_refs,
        operator_confirmed=operator_confirmed,
        project_key=decision.project_key, decision_id=decision_id,
    )

    iso = now or iso_now()
    source_ref = f"decision:{decision_id}"
    body = {
        "decision_id": decision_id,
        "outcome": outcome,
        "reason_wrong": reason,
        "corrected_rule": corrected_rule,
        "recurrence_guard": recurrence_guard,
        "severity": severity,
        "applies_to": applies_to,
        "evidence_refs": evidence_refs,
        "marked_by": actor,
        "marked_at": iso,
    }
    content = _serialize_outcome(
        source_ref=source_ref, outcome=outcome, severity=severity, marked_at=iso, body=body,
    )
    if _detect_secret(content) is not None:
        raise ValueError(
            "decision outcome appears to contain a secret/credential and was rejected "
            "(durable memory). Record the lesson, never the secret itself."
        )

    content, _ = redact_pii(content)  # deterministic PII redaction (after secret reject)

    # Supersede the prior active outcome for THIS decision (one active per decision).
    needle = f"source_ref={source_ref} "
    prior = (await db.execute(
        select(ProjectMemory).where(
            ProjectMemory.project_key == decision.project_key,
            ProjectMemory.kind == DECISION_OUTCOME_KIND,
            ProjectMemory.status == "active",
        )
    )).scalars().all()
    superseded: list[str] = []
    for row in prior:
        if (row.content or "").startswith(needle):
            row.status = "superseded"
            row.updated_at = iso
            superseded.append(row.id)

    decision.status = _OUTCOME_TO_STATUS[outcome]
    mem = ProjectMemory(
        id=new_id(), project_key=decision.project_key, kind=DECISION_OUTCOME_KIND,
        content=content, status="active", created_at=iso, updated_at=iso,
    )
    db.add(mem)
    db.add(AuditLog(
        actor=actor, action="mark_decision_outcome", detail=f"{decision_id} → {outcome}",
    ))
    await db.commit()

    return {
        "id": mem.id,
        "decision_id": decision_id,
        "outcome": outcome,
        "status": decision.status,
        "severity": severity,
        "evidence_refs": evidence_refs,
        "superseded": superseded,
        "marked_by": actor,
        "marked_at": iso,
    }


async def get_decision_outcome(db: AsyncSession, decision_id: str) -> dict[str, Any] | None:
    """The latest active decision_outcome for one decision (parsed), or None."""
    decision = await db.get(Decision, decision_id)
    if decision is None:
        return None
    needle = f"source_ref=decision:{decision_id} "
    rows = (await db.execute(
        select(ProjectMemory).where(
            ProjectMemory.project_key == decision.project_key,
            ProjectMemory.kind == DECISION_OUTCOME_KIND,
            ProjectMemory.status == "active",
        ).order_by(ProjectMemory.created_at.desc())
    )).scalars().all()
    for row in rows:
        if (row.content or "").startswith(needle):
            return _parse_outcome(row)
    return None


async def get_known_traps(db: AsyncSession, project_key: str) -> list[dict[str, Any]]:
    """Decisions proven wrong (status='incorrect') joined to their corrected rule.

    Newest trap first. Each item carries the actionable lesson (``corrected_rule``)
    and ``severity`` so the next agent can avoid repeating the mistake.
    """
    decisions = (await db.execute(
        select(Decision).where(
            Decision.project_key == project_key,
            Decision.status == "incorrect",
        ).order_by(Decision.created_at.desc())
    )).scalars().all()
    if not decisions:
        return []

    outcomes = (await db.execute(
        select(ProjectMemory).where(
            ProjectMemory.project_key == project_key,
            ProjectMemory.kind == DECISION_OUTCOME_KIND,
            ProjectMemory.status == "active",
        )
    )).scalars().all()
    by_decision: dict[str, dict[str, Any]] = {}
    for row in outcomes:
        parsed = _parse_outcome(row)
        did = parsed.get("decision_id")
        if did and did not in by_decision:
            by_decision[did] = parsed

    traps: list[dict[str, Any]] = []
    for d in decisions:
        o = by_decision.get(d.id, {})
        traps.append({
            "decision_id": d.id,
            "statement": d.statement,
            "outcome": "incorrect",
            "reason_wrong": o.get("reason_wrong"),
            "corrected_rule": o.get("corrected_rule"),
            "recurrence_guard": o.get("recurrence_guard"),
            "severity": o.get("severity"),
            "evidence_refs": o.get("evidence_refs", []),
            "applies_to": o.get("applies_to", []),
            "marked_at": o.get("marked_at"),
            "created_at": d.created_at,
        })
    return traps
