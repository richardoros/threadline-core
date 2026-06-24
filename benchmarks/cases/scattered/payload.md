# Resume payload — events-pipeline

Composed from threadline core records. Handed to the next session instead of the transcript.

**Objective:** Build the events ingestion pipeline (clickstream to warehouse) for analytics.

**Decisions in force:**
- Orchestrate with Airflow (one DAG per source), not ad-hoc cron scripts.
- Land raw events as Parquet in the lake, partitioned by day.
- Deduplicate on event_id; last-write-wins within a partition.

**Open loops:**
- Historical backfill of pre-launch events is not done yet.
- PII scrub on the warehouse export is deferred.

**Known traps (do not repeat):**
- A naive timestamp (no timezone) once broke day partition assignment; always use tz-aware UTC.

**Active caveats:**
- The staging bucket has no lifecycle policy, so storage cost grows unbounded until one is set.
