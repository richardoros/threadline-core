"""Event ingestion routes — the primary write path for agent events.

Routes
------
POST /api/events
    Ingest a single AgentEventIn payload.  FastAPI validates the body against
    the AgentEventIn schema; any validation failure returns 422 automatically.
    On success, returns 201 with the new event's id, its resolved session_id,
    and the counts of derived decisions and open loops.

POST /api/events/batch
    Ingest a list of AgentEventIn payloads in one call.  Designed for the
    "manual paste" path (e.g. dumping a ChatGPT session into Threadline).
    Events are processed sequentially in the order they appear in the list.
    See the route docstring for the validation and atomicity contracts.

Both routes delegate to ``ingest_event`` from the ingest service.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from threadline_core.api.deps import get_db, get_settings_dep
from threadline_core.config import Settings
from threadline_core.protocol import AgentEventIn
from threadline_core.services.ingest import IngestResult, ingest_event

router = APIRouter()


def _result_to_dict(result: IngestResult) -> dict[str, Any]:
    """Convert an IngestResult to the standard response shape.

    Having this in one place means the single-event and batch endpoints
    produce identical per-event objects.
    """
    return {
        "id": result.event_id,
        "session_id": result.session_id,
        "derived": {
            "decisions": result.decisions_created,
            "open_loops": result.open_loops_created,
        },
    }


@router.post("/events", status_code=201)
async def ingest_single_event(
    event: AgentEventIn,
    settings: Annotated[Settings, Depends(get_settings_dep)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Ingest a single agent event and return the derived memory summary.

    Body: AgentEventIn (validated by FastAPI; 422 on bad input).

    Response 201:
        {
            "id": "<event_id>",
            "session_id": "<session_id or null>",
            "derived": {"decisions": n, "open_loops": n}
        }
    """
    result = await ingest_event(db, settings, event, actor="api")
    return _result_to_dict(result)


@router.post("/events/batch", status_code=201)
async def ingest_event_batch(
    events: list[AgentEventIn],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Ingest a list of agent events sequentially and return per-event summaries.

    Designed for the manual-paste workflow: copy a full session history from
    ChatGPT or another agent and POST it in one call.

    Events are processed in the order they appear in the list.  If two events
    in the batch would produce the same Decision or OpenLoop, the second is
    deduplicated just as it would be if the events arrived one at a time.

    Whole-batch validation: FastAPI validates every item before any event is
    ingested.  A single invalid item rejects the whole request (422).
    An empty list is valid and returns 201 with ``{"results": []}``.

    Atomicity: the batch is NOT atomic.  Each event commits individually as
    it is ingested.  If ingestion fails partway through, every earlier event
    is already durably persisted and the request returns 500 — callers
    should retry only the events that did not make it, not the whole batch
    (re-sending succeeded events is mostly harmless thanks to dedup, but
    creates duplicate AgentEvent rows).

    Response 201:
        {"results": [<single-event shape>, ...]}
    """
    results = []
    for event in events:
        result = await ingest_event(db, settings, event, actor="api")
        results.append(_result_to_dict(result))
    return {"results": results}
