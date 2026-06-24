# Session transcript — events-pipeline (raw, lightly cleaned)

Kicking off the ingestion work. To recap the two architecture calls we locked at kickoff
so nobody relitigates them: we orchestrate with Airflow, one DAG per source, rather than
the pile of cron scripts the old system used. We want retries, dependency edges, and a
real scheduler we can reason about. And we land raw events as Parquet in the lake,
partitioned by day, because the analysts query by date range and columnar scans there are
an order of magnitude cheaper than the line-delimited JSON we started with. Those two are
settled and not up for debate today.

## Today's work

Spent the first hour just getting a local environment that resembles prod: the scheduler,
a local object store, and a warehouse sandbox. The scheduler container needed a bumped
memory limit or the scheduler loop got OOM-killed under even a trivial DAG, which was a
fun thirty minutes. Got there. Then wired the first source end-to-end as a smoke test:
read a fixture batch, write it out, confirm the warehouse sees it. Green.

Talked to the warehouse team about the table layout. They'd prefer wide tables over a
star schema for the first cut because the BI tool they use chokes on too many joins, and
honestly for the query patterns we have that's fine. We can normalise later if it hurts.
They also asked for column-level descriptions in the catalog, which is reasonable; I'll
add a docs step to the DAG that publishes the schema with comments.

## Correctness discussion (the substance)

On idempotency: re-delivered events are a fact of life with the upstream queue's
at-least-once guarantee, so we deduplicate on the event_id, last-write-wins within a day
partition. That keeps the merge logic simple and the result deterministic on replay.

We still owe the historical backfill. Pre-launch events were never loaded, so any
dashboard that looks further back than launch day is simply wrong right now. It's a known
gap, not started, and it'll be its own DAG with careful rate control so we don't melt the
warehouse during a catch-up run.

And the bug from last week, worth burning into memory: a naive timestamp with no timezone
got compared against the UTC partition boundaries, and a whole afternoon of events landed
in the wrong day partition. The fix was to make everything tz-aware UTC on the partition
key, but the lesson is the general one. Never let a naive datetime near a partitioning
decision.

## More plumbing

Refactored the writer so the Parquet page size and compression codec are config, not
hardcoded, after the first batch produced thousands of tiny files. Compaction job is a
follow-up but the immediate knobs are exposed. Added basic data-quality assertions: row
count sanity, null-rate ceilings on a few critical columns, and a freshness check that
pages if the latest partition is older than expected. Cheap insurance.

## Wrap-up

Two things to flag before wrapping. The warehouse export still ships raw user fields; the
PII scrub on that export is deferred, and we must not point any external or third-party
consumer at it until that work lands. Second, ops noticed the staging bucket has no
lifecycle policy, so every intermediate file lives forever and the storage bill is
creeping upward — we need an expiry rule on that bucket before it becomes real money.
That's it for today; next session I'd start the backfill DAG.
