"""Deterministic open-loop classification — v1.1 "work-feed hygiene".

The Next Steps Engine should recommend *work to do*, not session bookkeeping,
approval gates, or "it's deployed" status notes. Those still belong in the store
(they are honest history), but surfacing them in "Continue where you stopped"
makes Threadline read like a log viewer instead of an execution engine.

This module answers one pure question — given a loop's text, what KIND of loop
is it? — with **no new schema, no LLM, no DB**. Classification is derived from
the description string the store already holds, so it is fully deterministic and
auditable: every result carries a human-readable ``reason`` naming the trigger.

Single label per loop, by a fixed **precedence** (first match wins):

    blocker → approval_gate → deployed_or_resolved_status
            → bookkeeping → verification → actionable_work

Why this order:
- ``blocker`` (the ``[blocker]`` prefix) is the strongest, most explicit signal
  and must never be overridden by an incidental keyword in the body.
- status is checked **before** verification on purpose: "the migration is
  *deployed*" (completion → hide) shares the substring "deploy" with the
  imperative "*deploy* the migration" (work → keep). Past-tense, word-boundary
  status words win the completion case; the bare verb falls through to
  verification. Tense is the discriminator.
- ``actionable_work`` is the default — when nothing matched, assume real work.
  We would rather show a borderline loop than silently hide genuine work.

Only ``EXCLUDED_FROM_FEED`` classes are hidden from ``threadline next`` by
default; everything else (including verification) stays visible.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from threadline_core.services.ingest import BLOCKER_PREFIX

# --------------------------------------------------------------------------- #
# Category labels (string constants so callers and the dashboard can match).   #
# --------------------------------------------------------------------------- #
BLOCKER = "blocker"
APPROVAL_GATE = "approval_gate"
DEPLOYED_OR_RESOLVED_STATUS = "deployed_or_resolved_status"
BOOKKEEPING = "bookkeeping"
VERIFICATION = "verification"
ACTIONABLE_WORK = "actionable_work"

# The three "noise" classes hidden from the work feed by default. Everything
# else — blocker, verification, actionable_work — is real work and stays.
EXCLUDED_FROM_FEED = frozenset({APPROVAL_GATE, DEPLOYED_OR_RESOLVED_STATUS, BOOKKEEPING})

# Stems that mark a loop as likely to need verification before it is "done".
# Owned here (single source of truth); next_steps reuses ``verification_hit``
# for its ``verification_needed`` flag so the two never drift.
#
# Matched at a WORD BOUNDARY (``\b<stem>``), not as a bare substring: the stem
# still catches its inflections ("migrat" → migrate/migration, "test" →
# test/tests/testing) but no longer trips when it is buried inside an unrelated
# word — critically, "test" must NOT match "la*test*"/"fa*stest*", or the
# canonical research loop "research the latest X" would be mislabeled
# verification (and, in v2, wrongly excluded from research recommendations).
VERIFICATION_KEYWORDS = ("test", "migrat", "verify", "regress", "deploy")
_VERIFICATION_RE = re.compile(r"\b(" + "|".join(VERIFICATION_KEYWORDS) + r")", re.IGNORECASE)

# --------------------------------------------------------------------------- #
# Triggers. Multi-word phrases are far less likely to appear incidentally than #
# bare words; single risky words use \b word boundaries so "investigated" does #
# not trip "gated" and "disclosed" does not trip "closed".                     #
# --------------------------------------------------------------------------- #
_APPROVAL_GATE_RE = re.compile(
    r"awaiting approval|waiting for approval|your go|say go|\bgated\b",
    re.IGNORECASE,
)
# Past-tense / state words = completion, not an instruction. Word-bounded.
_STATUS_RE = re.compile(
    r"\b(deployed|merged|shipped|resolved|closed)\b",
    re.IGNORECASE,
)
_BOOKKEEPING_RE = re.compile(
    r"branch awaiting|pr ready|ci green|report back|show output",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Classification:
    """One loop's derived kind, with an explainable reason. Never hidden."""

    category: str
    reason: str

    @property
    def in_feed(self) -> bool:
        """True if this loop should appear in the default Next Steps feed."""
        return self.category not in EXCLUDED_FROM_FEED


def _verification_hit(low: str) -> str | None:
    """Return the first verification stem present (word-bounded), or None.

    ``low`` must already be lowercased; the match is case-insensitive regardless.
    """
    m = _VERIFICATION_RE.search(low)
    return m.group(1).lower() if m else None


def verification_hit(text: str) -> str | None:
    """Public word-bounded verification check — reused by next_steps so the
    ``verification_needed`` flag and this classifier can never disagree."""
    return _verification_hit(text.lower())


def classify_loop(description: str) -> Classification:
    """Classify a loop by its text alone — pure, deterministic, explainable.

    Precedence (first match wins): blocker → approval_gate → status →
    bookkeeping → verification → actionable_work. The blocker check runs on the
    raw description (the ``[blocker]`` prefix lives there); all other matches run
    case-insensitively over the lowercased text.
    """
    text = description or ""
    low = text.lower()

    if low.startswith(BLOCKER_PREFIX.strip()):  # "[blocker]" — strongest signal
        return Classification(BLOCKER, "blocker prefix")

    if (m := _APPROVAL_GATE_RE.search(low)) is not None:
        return Classification(APPROVAL_GATE, f"approval-gate phrase: {m.group(0)!r}")

    if (m := _STATUS_RE.search(low)) is not None:
        return Classification(
            DEPLOYED_OR_RESOLVED_STATUS, f"completion status: {m.group(0)!r}"
        )

    if (m := _BOOKKEEPING_RE.search(low)) is not None:
        return Classification(BOOKKEEPING, f"bookkeeping phrase: {m.group(0)!r}")

    if (kw := _verification_hit(low)) is not None:
        return Classification(VERIFICATION, f"verification keyword: {kw!r}")

    return Classification(ACTIONABLE_WORK, "no noise signal — treated as work")
