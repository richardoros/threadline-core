# Threadline Agent Event Protocol

## What an agent event is

An agent event is a structured, explicit report that an AI agent sends to Threadline when something meaningful happens during its work session. The agent always decides what to send: a commit landing, a decision made, a blocker found, a session starting or ending. Threadline never reads transcripts, monitors the filesystem, or infers activity from system state — it only knows what agents choose to report. This is the design: explicit reporting, not surveillance.

---

## Event types

All 11 valid values for the `event_type` field:

| `event_type` | Meaning |
|---|---|
| `session_started` | Agent is beginning a new work session on a project. Carry the session goal and any inherited context here. |
| `checkpoint` | Work is progressing normally; batch files changed or a logical unit of work is complete. This is the most common event type. |
| `decision` | A significant architectural or design choice was made that future agents should know about (e.g. "we chose SQLite over Postgres"). |
| `blocker` | The agent is stuck and cannot continue without human input or resolution of an external dependency. |
| `open_loop` | Something noticed but intentionally deferred — a TODO that should surface in the next session's context. |
| `file_change_summary` | A structured summary of file-level changes, used when a session touched many files and a higher-level diff is more useful than a wall of filenames. |
| `verification_result` | The outcome of running tests, linters, or other automated checks. Captures pass/fail and the commands run. |
| `research_request` | The agent needs information from outside its context window — signals that a Threadline research sub-task is needed. |
| `session_ended` | The agent is wrapping up. Carry the session summary and any unresolved open loops here. |
| `handoff_requested` | The current agent wants another agent to continue the work. Includes enough context for the next agent to start cleanly. |
| `handoff_generated` | Threadline has assembled a continuation prompt ready for the next agent to consume. |

---

## AgentEventIn fields

The full ingest payload that agents POST to `POST /api/events`:

| Field | Type | Required | Notes |
|---|---|---|---|
| `event_type` | string (EventType enum) | yes | One of the 11 values in the table above. |
| `project_key` | string | yes | Lowercase slug identifying the project. Pattern: `^[a-z0-9][a-z0-9\-_]*$`. Examples: `"acme-api"`, `"my-project_2"`. |
| `agent.name` | string | yes | Stable identifier for the agent, used to group events across sessions. Examples: `"claude_code"`, `"codex"`, `"cursor"`. |
| `agent.type` | string | no (default: `"coding_agent"`) | Broad category of the agent. Use `"coding_agent"` for agents that write code. |
| `session_id` | string or null | no | The agent's current session ID if it tracks sessions. When provided, Threadline groups events under that session. `null` is fine for one-off events. |
| `summary` | string (min 1 char) | yes | One or two plain-language sentences describing what happened. This is the primary text that appears in daily notes and continuation prompts; it should be self-contained and human-readable without the `details` payload. |
| `details` | object | no (default: `{}`) | Free-form key/value payload for structured metadata. See well-known keys below. Additional keys are stored as-is; unknown keys do not cause validation errors. |
| `details.files_changed` | list[string] | no | Paths of files created or modified. |
| `details.decisions` | list[string] | no | Choices made during this unit of work. Each string becomes a `Decision` row in the database (deduplicated). |
| `details.open_loops` | list[string] | no | Deferred items for a future session. Each string becomes an `OpenLoop` row (deduplicated). |
| `details.verification` | list[string] | no | Commands run or assertions checked. Surfaces in continuation prompts under the verification section. |
| `occurred_at` | ISO-8601 datetime or null | no | When the event happened, according to the agent's clock. **Must be timezone-aware** — naive datetimes are rejected with a 422. Threadline normalises all timestamps to UTC on ingest. `null` means "use the ingest time". |
| `privacy.contains_secrets` | boolean | no (default: `false`) | Set `true` if the event payload may contain secrets. Controls cloud-forwarding (reserved for future use). |
| `privacy.allow_cloud_processing` | boolean | no (default: `false`) | Set `true` to allow Threadline to forward this event to a cloud service. Default is off; everything stays local. |

**Forward compatibility:** unknown top-level fields are silently ignored. Send extra fields freely — the server will not break.

---

## Canonical example

```json
{
  "event_type": "checkpoint",
  "project_key": "acme-api",
  "agent": {
    "name": "claude_code",
    "type": "coding_agent"
  },
  "summary": "Added rate limiting to the public API gateway.",
  "details": {
    "files_changed": [
      "src/gateway/limiter.py",
      "tests/test_limiter.py"
    ],
    "decisions": [
      "Use a token-bucket limiter over a fixed-window counter."
    ],
    "open_loops": [
      "Add a burst-traffic load test for the limiter."
    ],
    "verification": [
      "pytest tests/test_limiter.py"
    ]
  },
  "privacy": {
    "contains_secrets": false,
    "allow_cloud_processing": false
  }
}
```

This is the canonical example from `docs/example-event.json`. The protocol test suite validates this exact payload on every run.

---

## Derivation rules

When Threadline ingests an event it derives structured memory rows from the payload. The rules are applied in two passes:

**Primary derivation** — driven by `event_type`:

| `event_type` | Row created |
|---|---|
| `decision` | One `Decision` row with `statement = event.summary` and `rationale = details.rationale` (if present). |
| `open_loop` | One `OpenLoop` row with `description = event.summary`. |
| `blocker` | One `OpenLoop` row with `description = "[blocker] " + event.summary`. The `[blocker] ` prefix is fixed; consumers use it to distinguish blockers from plain open loops. |
| All other types | No primary row created. |

**Secondary derivation** — applies to every `event_type`:

- Each string in `details["decisions"]` creates one `Decision` row.
- Each string in `details["open_loops"]` creates one `OpenLoop` row.

**Deduplication:** a `Decision` is skipped if an active `Decision` with the identical `statement` already exists for the same project. An `OpenLoop` is skipped if an open `OpenLoop` with the identical `description` already exists. Both checks are exact, case-sensitive string matches. If the wording changes even slightly, a new row is created — slightly different wording often means a genuinely different thought.

---

## Transport

### HTTP — single event

```bash
curl -X POST http://127.0.0.1:8400/api/events \
  -H "Content-Type: application/json" \
  -d @docs/example-event.json
```

Response (201 Created):

```json
{"id": "...", "session_id": null, "derived": {"decisions": 1, "open_loops": 1}}
```

### HTTP — batch

```bash
curl -X POST http://127.0.0.1:8400/api/events/batch \
  -H "Content-Type: application/json" \
  -d '[<event1>, <event2>]'
```

All items are validated before any event is written. One bad item rejects the whole request (422).

### Authentication

When `THREADLINE_API_TOKEN` is set, include `Authorization: Bearer <token>` on every `/api/*` request. When the token is empty (the default), auth is disabled — Threadline trusts the network layer (localhost or Tailscale).

### MCP — log_agent_event tool

From inside a Claude Code session with the Threadline MCP server configured:

```
log_agent_event({
  "event_type": "checkpoint",
  "project_key": "myproject",
  "agent": {"name": "claude_code", "type": "coding_agent"},
  "summary": "Finished the auth module.",
  "details": {"files_changed": ["src/auth.py"], "decisions": ["Use JWT, not sessions."]}
})
```

The MCP tool accepts the same JSON shape as the HTTP endpoint.

---

## Versioning

The protocol follows additive-only evolution: new optional fields may be added in future versions; existing field names and types will not change. Unknown fields sent by newer agents to older servers are ignored. Unknown fields sent by older agents to newer servers are also ignored (Pydantic `extra="ignore"`). There is no version field in the payload; version negotiation is out of scope for v0.1.
