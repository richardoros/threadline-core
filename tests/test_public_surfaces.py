"""Smoke tests for the 10 required clean-room properties.

These tests prove the properties using the installed package only — no private
modules, no private operational paths.
"""
from __future__ import annotations

import json

import pytest

from threadline_core.config import Settings
from threadline_core.models import Project
from threadline_core.protocol import AgentEventIn
from threadline_core.services.export import EXPORT_MANIFEST, export_all
from threadline_core.services.ingest import ingest_event
from threadline_core.services.project_state import get_project_state
from threadline_core.services.search import search_memory

# ---------------------------------------------------------------------------
# 1. Loopback-only default
# ---------------------------------------------------------------------------

def test_default_host_is_loopback():
    """Property 1: default host must be 127.0.0.1 — not 0.0.0.0."""
    s = Settings()
    assert s.host == "127.0.0.1", f"Expected 127.0.0.1, got {s.host!r}"


# ---------------------------------------------------------------------------
# 2. API authentication boundary
# ---------------------------------------------------------------------------

def test_api_requires_auth(tmp_path):
    """Property 2: require_token rejects when a token is configured."""
    from fastapi.testclient import TestClient

    from threadline_core.api.app import create_app

    # api_token="" is a no-op by design for localhost; configure one to test auth
    guarded = Settings(data_dir=str(tmp_path), api_token="secret-test-key")
    app = create_app(guarded)
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/api/projects")  # no Authorization header
        assert r.status_code in (401, 403), f"Expected 401/403, got {r.status_code}"
        r2 = client.get("/api/projects",
                        headers={"Authorization": "Bearer secret-test-key"})
        assert r2.status_code == 200, f"Expected 200 with correct token, got {r2.status_code}"


# ---------------------------------------------------------------------------
# 3. Session / event ingestion
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_event_ingestion(db, settings):
    """Property 3: start_session + log_agent_event round-trip."""
    db.add(Project(key="smoke", name="Smoke"))
    await db.commit()

    start_json = json.dumps({
        "event_type": "session_started",
        "project_key": "smoke",
        "agent": {"name": "test"},
        "summary": "session started",
    })
    result = await ingest_event(db, settings, AgentEventIn.model_validate_json(start_json))
    assert result.event_id

    chk_json = json.dumps({
        "event_type": "checkpoint",
        "project_key": "smoke",
        "session_id": result.session_id,
        "agent": {"name": "test"},
        "summary": "work done",
        "details": {
            "decisions": ["Use SQLite"],
            "open_loops": ["rate limiting"],
        },
    })
    result2 = await ingest_event(db, settings, AgentEventIn.model_validate_json(chk_json))
    assert result2.event_id
    assert result2.decisions_created == 1
    assert result2.open_loops_created == 1


# ---------------------------------------------------------------------------
# 4. Project-state retrieval
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_project_state_retrieval(db, settings):
    """Property 4: get_project_state returns structured state with no synthesis."""
    db.add(Project(key="statetest", name="StateTest", current_objective="build it"))
    await db.commit()

    state = await get_project_state(db, "statetest")
    assert state.project_key == "statetest"
    assert state.objective == "build it"
    assert isinstance(state.open_loops, list)
    assert isinstance(state.decisions, list)
    assert isinstance(state.evidence_ids, list)

    with pytest.raises(LookupError):
        await get_project_state(db, "nonexistent")


# ---------------------------------------------------------------------------
# 5. Evidence-gated lifecycle transitions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_evidence_gated_transitions(db, settings):
    """Property 5: confirm_finding rejects without evidence (non-operator path)."""
    from threadline_core.services.findings import confirm_finding, propose_finding

    db.add(Project(key="gatetest", name="GateTest"))
    await db.commit()

    result = await propose_finding(
        db,
        project_key="gatetest",
        finding_class="gap",
        category="test",
        statement="missing test coverage",
        severity="high",
    )
    fid = result["finding_id"]
    assert fid

    with pytest.raises(ValueError, match="[Ee]vidence|admissib"):
        await confirm_finding(db, finding_id=fid, evidence_refs=[])


# ---------------------------------------------------------------------------
# 6. Search
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search(db, settings):
    """Property 6: search_memory returns hits or an empty list (no crash)."""
    db.add(Project(key="searchtest", name="SearchTest"))
    await db.commit()

    hits = await search_memory(db, "SQLite", project_key=None, limit=5)
    assert isinstance(hits, list)


# ---------------------------------------------------------------------------
# 7. Raw governed-state export
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_raw_export(db, settings, tmp_path):
    """Property 7: export writes governed_state manifest and no private output."""
    db.add(Project(key="exporttest", name="ExportTest"))
    await db.commit()

    report = await export_all(db, tmp_path)
    assert report.files_written > 0

    manifest_path = tmp_path / "projects" / "exporttest" / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["export_mode"] == "governed_state"
    assert manifest["compiled_memory_included"] is False
    assert manifest["morning_pulse_included"] is False
    assert manifest["continuation_included"] is False


# ---------------------------------------------------------------------------
# 8. No local LLM required
# ---------------------------------------------------------------------------

def test_no_llm_dependency():
    """Property 8: package imports cleanly with no LLM client dependency."""
    import threadline_core.api.app  # noqa: F401
    import threadline_core.cli  # noqa: F401
    import threadline_core.mcp_server  # noqa: F401


# ---------------------------------------------------------------------------
# 9. No private module present in the wheel
# ---------------------------------------------------------------------------

def test_no_private_module_in_package():
    """Property 9: private modules are not importable from threadline_core."""
    private = ["pulse", "pulse_store", "memory", "prompts", "next_steps",
               "research_recommendations", "research_store"]
    for mod in private:
        try:
            import importlib
            importlib.import_module(f"threadline_core.services.{mod}")
            assert False, f"Private module threadline_core.services.{mod} is importable"
        except ModuleNotFoundError:
            pass  # correct


# ---------------------------------------------------------------------------
# 10. No private operational path (EXPORT_MANIFEST confirms stripped export)
# ---------------------------------------------------------------------------

def test_export_manifest_is_stripped():
    """Property 10: EXPORT_MANIFEST confirms no private paths are active."""
    assert EXPORT_MANIFEST["compiled_memory_included"] is False
    assert EXPORT_MANIFEST["morning_pulse_included"] is False
    assert EXPORT_MANIFEST["research_recommendations_included"] is False
    assert EXPORT_MANIFEST["continuation_included"] is False
