"""Open agent event protocol — the public contract for Threadline events.

This module defines the data shapes that agents use to report what they did.
It is the single source of truth for what a valid event looks like.

WHY THIS FILE EXISTS
--------------------
Multiple AI agents (Claude Code, Codex, Cursor, ChatGPT) need to send events
to Threadline without understanding its internal implementation. This module
is the bridge: agents import it, build an ``AgentEventIn`` payload, and POST
it. Threadline's ingest layer validates with these same schemas before
persisting anything.

NO INTERNAL IMPORTS RULE
-------------------------
This module intentionally imports ONLY from the Python standard library and
pydantic. It must never import anything from ``threadline.*``.

The reason: this file is destined to be extracted into a standalone public
package (``threadline-protocol``) so agents can depend on it without pulling
in the full Threadline server. If this file imported internal threadline
modules, that extraction would silently break. Any reviewer — human or AI —
should treat any line that imports the threadline package as a bug.
"""

from enum import Enum
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class EventType(str, Enum):
    """Every kind of moment an agent can report.

    Each member maps to one meaningful event in an agent's work session:

    - ``session_started``: the agent is beginning a new work session on a
      project. Carry the session goal and any inherited context here.
    - ``checkpoint``: work is progressing normally; a batch of files was
      changed or a logical unit of work completed. The most common event type.
    - ``decision``: a significant architectural or design choice was made that
      future agents should know about (e.g. "we chose SQLite over Postgres").
    - ``blocker``: the agent is stuck and cannot continue without human input
      or resolution of an external dependency.
    - ``open_loop``: something was noticed but intentionally deferred — a TODO
      that should surface in the next session's context.
    - ``file_change_summary``: a structured summary of file-level changes, used
      when a session touched many files and a higher-level diff is more useful
      than a wall of filenames.
    - ``verification_result``: the outcome of running tests, linters, or any
      other automated checks. Captures pass/fail and the commands run.
    - ``research_request``: the agent needs information from outside its
      context window — signals to Threadline that a research sub-task is needed.
    - ``session_ended``: the agent is wrapping up. Carry a session summary and
      any unresolved open loops here.
    - ``handoff_requested``: the current agent wants another agent to continue
      the work. Includes enough context for the next agent to start cleanly.
    - ``handoff_generated``: Threadline has assembled a continuation prompt
      that is ready for the next agent to consume.
    """

    session_started = "session_started"
    checkpoint = "checkpoint"
    decision = "decision"
    blocker = "blocker"
    open_loop = "open_loop"
    file_change_summary = "file_change_summary"
    verification_result = "verification_result"
    research_request = "research_request"
    session_ended = "session_ended"
    handoff_requested = "handoff_requested"
    handoff_generated = "handoff_generated"


class AgentInfo(BaseModel):
    """Identity of the agent that produced the event.

    ``name`` is the agent's stable identifier used to group events across
    sessions — e.g. ``"claude_code"``, ``"codex"``, ``"cursor"``.
    ``type`` is a broad category; use ``"coding_agent"`` for agents that write
    code, and other values as new agent roles emerge.
    """

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1)
    type: str = Field(default="coding_agent", min_length=1)


class PrivacyFlags(BaseModel):
    """Controls how Threadline may handle the event's content downstream.

    ``contains_secrets``: set ``True`` if the payload includes API keys,
    passwords, tokens, or any other sensitive credential. Threadline will
    refuse to forward such events to any cloud service.

    ``allow_cloud_processing``: set ``True`` only if the project owner has
    explicitly opted in to sending event data to external LLM APIs for
    summarisation or memory compilation. Defaults to ``False`` to err on the
    side of privacy.
    """

    model_config = ConfigDict(extra="ignore")

    contains_secrets: bool = False
    allow_cloud_processing: bool = False


class AgentEventIn(BaseModel):
    """A single event reported by an agent — the primary ingest payload.

    This is the shape that agents POST to ``POST /events``. Every field is
    validated on arrival; Threadline rejects events that do not conform.
    Unknown fields are silently ignored so newer agents can send fields this
    server version doesn't understand yet (forward compatibility).

    Fields
    ------
    event_type:
        What kind of moment this is. See ``EventType`` for the full vocabulary.
    project_key:
        Lowercase slug identifying the project. Must start with a letter or
        digit and contain only ``[a-z0-9-_]``. Examples: ``"acme-api"``,
        ``"my-project_2"``.
    agent:
        Who sent this event.
    session_id:
        The agent's current session ID, if the agent tracks sessions. ``None``
        is fine for one-off events. Threadline will group events by session
        when this is provided.
    summary:
        One or two plain-language sentences describing what happened. This is
        the primary text that appears in daily notes and continuation prompts,
        so it should be self-contained and human-readable without the ``details``
        payload.
    details:
        Free-form key/value payload for structured metadata. Well-known keys:

        - ``files_changed`` (list[str]): paths of files created or modified.
        - ``decisions`` (list[str]): choices made during this work unit.
        - ``open_loops`` (list[str]): deferred items for a future session.
        - ``verification`` (list[str]): commands run or assertions checked.

        Additional keys are allowed; unknown keys are stored as-is.
    occurred_at:
        When the event happened, according to the agent's clock. ``None``
        means "now" and Threadline will use its own ingest timestamp. When
        provided, the timestamp must be timezone-aware — naive datetimes are
        rejected, because events from multiple agents in different timezones
        could not be ordered correctly against each other otherwise.
    privacy:
        Controls whether Threadline may forward this event to cloud services.
    """

    model_config = ConfigDict(extra="ignore")

    event_type: EventType
    project_key: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9\-_]*$")
    agent: AgentInfo
    session_id: str | None = None
    summary: str = Field(min_length=1)
    details: dict[str, Any] = Field(default_factory=dict)
    occurred_at: AwareDatetime | None = None
    privacy: PrivacyFlags = Field(default_factory=PrivacyFlags)
