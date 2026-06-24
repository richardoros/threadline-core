#!/usr/bin/env python3
"""resume-bench: does a structured "resume payload" beat raw transcript at EQUAL token budget?

Threadline's claim is *cost-to-restore-state*: hand the next agent a small, bounded
state payload instead of making it re-read the session transcript. Size alone is a
weak claim (smaller is trivially cheaper), so this measures the thing that actually
matters: RECALL OF LOAD-BEARING FACTS AT AN EQUAL TOKEN BUDGET. Given B tokens, how
many independently hand-labelled "must-know-to-resume" facts can you recover from the
structured payload versus from an equal-B slice of the raw transcript?

Deterministic. No API key, no network. Tokens via tiktoken (cl100k_base) so the
numbers line up with what everyone else reports.

    uv run --with tiktoken python resume_bench.py cases/demo   # one case, detailed
    uv run --with tiktoken python resume_bench.py cases        # all cases, summary
    uv run --with tiktoken python resume_bench.py --selfcheck

A fact is "recovered" from a text if ALL of its `requires` substrings appear
(whitespace-normalised, case-insensitive). Labels live in <case>/facts.json and are
authored independently of how threadline stores state -- see README.md for the honesty
caveats (the payload is *expected* to score high; the finding is how little the raw
transcript recovers at the same budget).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import tiktoken

ENC = tiktoken.get_encoding("cl100k_base")


def ntok(text: str) -> int:
    return len(ENC.encode(text))


def budget_slice(text: str, budget: int, where: str) -> str:
    """First (head) or last (tail) `budget` tokens of text, decoded back to a string."""
    toks = ENC.encode(text)
    toks = toks[:budget] if where == "head" else toks[-budget:]
    return ENC.decode(toks)


def _norm(s: str) -> str:
    """Lowercase and collapse whitespace runs to single spaces, so a fact that wraps
    across a line break in a transcript still matches (the fact is present either way)."""
    return " ".join(s.split()).lower()


def recall(text: str, facts: list[dict]) -> tuple[int, list[str]]:
    low = _norm(text)
    hits = [f["id"] for f in facts if all(_norm(s) in low for s in f["requires"])]
    return len(hits), hits


def run(case: Path) -> dict:
    payload = (case / "payload.md").read_text()
    transcript = (case / "transcript.md").read_text()
    facts = json.loads((case / "facts.json").read_text())["facts"]

    b = ntok(payload)  # equal budget = the payload's own size
    texts = [
        ("payload (full)", payload),
        (f"transcript HEAD ({b}t)", budget_slice(transcript, b, "head")),
        (f"transcript TAIL ({b}t)", budget_slice(transcript, b, "tail")),
        ("transcript (full ref)", transcript),
    ]
    rows = [(name, ntok(text), recall(text, facts)[0]) for name, text in texts]
    return {
        "name": case.name,
        "budget": b,
        "payload_tok": b,
        "transcript_tok": ntok(transcript),
        "total": len(facts),
        "rows": rows,  # order: payload, head, tail, full
    }


def fmt(res: dict) -> str:
    total = res["total"]
    x = res["transcript_tok"] / max(res["payload_tok"], 1)
    out = [
        f"[{res['name']}] budget B = {res['budget']}t (= payload size)   facts = {total}",
        f"payload {res['payload_tok']}t  vs  transcript {res['transcript_tok']}t ({x:.0f}x)",
        "",
        f"{'source':<26}{'tokens':>8}{'recall':>9}",
        "-" * 43,
    ]
    for name, tok, r in res["rows"]:
        out.append(f"{name:<26}{tok:>8}{f'{r}/{total}':>9}")
    return "\n".join(out)


def summary(results: list[dict]) -> str:
    head = f"{'case':<14}{'budget':>7}{'payload':>9}{'tx-head':>9}{'tx-tail':>9}{'tx-full':>9}"
    out = [head, "-" * len(head)]
    for res in results:
        tot = res["total"]
        p, h, t, f = (res["rows"][i][2] for i in range(4))
        out.append(
            f"{res['name']:<14}{res['budget']:>7}"
            f"{f'{p}/{tot}':>9}{f'{h}/{tot}':>9}{f'{t}/{tot}':>9}{f'{f}/{tot}':>9}"
        )
    return "\n".join(out)


def _selfcheck() -> None:
    facts = [
        {"id": "a", "requires": ["jwt", "stateless"]},
        {"id": "b", "requires": ["rate limit"]},
    ]
    assert recall("we chose JWT, it is Stateless", facts) == (1, ["a"])
    assert recall("JWT stateless and rate limit done", facts)[0] == 2
    assert recall("nothing relevant here", facts) == (0, [])
    # whitespace-normalised match: fact present across a line break still counts
    wrap = [{"id": "e", "requires": ["email verification"]}]
    assert recall("the email\nverification flow is deferred", wrap) == (1, ["e"])
    assert ntok("hello world") > 0
    assert ntok(budget_slice("a b c d e f g h", 3, "head")) <= 3
    assert ntok(budget_slice("a b c d e f g h", 3, "tail")) <= 3
    print("selfcheck OK")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["--selfcheck"]:
        _selfcheck()
    elif len(args) == 1:
        path = Path(args[0])
        if (path / "facts.json").exists():
            print(fmt(run(path)))
        else:
            cases = sorted(d for d in path.iterdir() if (d / "facts.json").exists())
            if not cases:
                print(f"no cases found under {path}")
                sys.exit(2)
            print(summary([run(c) for c in cases]))
    else:
        print(__doc__)
        sys.exit(2)
