# resume-bench

Does threadline's structured **resume payload** actually let an agent pick up where it
left off, or is it just *smaller*? This benchmark measures **recall of load-bearing facts
at an equal token budget** — because size alone is a trivial win (anything smaller is
cheaper; that proves nothing).

## The question

When a new agent session starts it needs the *state* of the project: the decisions in
force, the open work, the traps not to repeat. Two ways to get it:

1. **Re-read the raw session transcript** — what happens with no memory layer.
2. **Read threadline's resume payload** — a small, bounded set of structured records
   (objective + open loops + decisions + known traps + caveats), composed from the open
   core.

At an *equal token budget*, which one actually recovers the facts you need to continue?

## Method

`resume_bench.py <case>` reads a case directory containing:

- `payload.md` — the threadline resume payload (structured state).
- `transcript.md` — the raw session log it replaces.
- `facts.json` — independently authored "must-know-to-resume" facts. A fact is
  *recovered* from a text if **all** of its `requires` substrings appear
  (whitespace-normalised, case-insensitive).

It sets the budget **B = the payload's own token count**, then measures fact recall from
(a) the full payload, (b) the first B tokens of the transcript, (c) the last B tokens of
the transcript. Tokens are counted with `tiktoken` (`cl100k_base`). Fully deterministic —
no API key, no network.

```
uv run --with tiktoken python resume_bench.py cases        # all cases, summary table
uv run --with tiktoken python resume_bench.py cases/demo   # one case, detailed
uv run --with tiktoken python resume_bench.py --selfcheck
```

## Results — committed `cases/` (reproducible by anyone)

Three synthetic cases with different fact placement. Recall is out of 7 facts; the budget
B is each payload's own token count:

| case | what it tests | payload | tx-head | tx-tail | tx-full |
|---|---|---:|---:|---:|---:|
| `demo` | facts mid-session | **7/7** | 0/7 | 0/7 | 7/7 |
| `long` | facts mid-session, ~7x longer transcript | **7/7** | 0/7 | 0/7 | 7/7 |
| `scattered` | facts spread across head / middle / tail | **7/7** | 2/7 | 3/7 | 7/7 |

At an equal token budget the structured payload recovers **every** load-bearing fact in
every case. The raw transcript, given the same budget, recovers **none** when the facts
are spread through a session (`demo`, `long`), and only **2–3 of 7** even when they happen
to sit near the ends (`scattered`) — and you need the *full* transcript (4–7x the budget)
to match what the payload delivers.

`scattered` is the honest one: facts at realistic, distributed positions are the rebuttal
to "you just hid them in the middle." `long` makes the scaling point — as the session
grows, an equal-budget head/tail slice becomes useless while the payload stays constant.

## Real-world scale — the `threadline` project's own dogfood data

Measured on this tool's actual development history (numbers only; the content is internal
and not committed):

- Raw transcripts: **55.0 MB across 16 sessions** (exact on-disk bytes); a single recent
  session ≈ **7.0 MB**.
- The resume payload that replaces re-reading them is **capped at ≤5 KB** by design.
- That is **~1,400x smaller** than re-reading just the last session — and, unlike the
  transcript, it **stays flat as the project grows** instead of accumulating without bound.

(MB→token figures are ~bytes/4 estimates; the `cases/demo` table above is exact tiktoken.)

## Honest caveats (please read before quoting numbers)

- **The payload is *expected* to score high** — it is built from those facts. The finding
  is **not** "payload wins"; it is *how little the raw transcript recovers at an equal
  budget*, which is what quantifies the value of structuring state up front.
- **It is lossy by design.** The payload is curated state, not a compression of the
  conversation. You are not meant to recover the chatter — only the facts needed to resume.
- **Labels are hand-authored**, independently of how threadline stores records, to avoid
  circularity. One synthetic case so far; more projects make the claim stronger.
- This measures the **open core** (structured records). The full product's LLM prose
  digest is out of scope — deliberately, so the benchmark runs on the open core with no
  key and anyone can reproduce it.
- A substring matcher is a **floor** on recall (it misses paraphrase). It is the same floor
  for both sources, so the comparison is fair.

## What this is *not*

This is **not** a conversational-QA / LoCoMo-style benchmark. threadline is not RAG over
chat logs; it stores structured state that agents explicitly write. Scoring it on semantic
recall over raw transcripts would measure something it does not claim to do — and would be
the wrong number to chase.
