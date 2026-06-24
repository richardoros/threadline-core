"""Probe harder cases: search query contains separators, document does not.

These tests reveal the actual UX breakage:
- User searches for `record_store.py` but the event only says "fixed record_store"
- User searches for `feature-flag-rollout` but doc says "feature flag rollout"
- User searches with a full path when doc mentions only the filename

These tests are EXPECTED TO FAIL with the current sanitize_query implementation
and define the exact contract the fix must satisfy.
"""
from __future__ import annotations

import pytest

from threadline_core.models import Project
from threadline_core.services.fts import fts_insert
from threadline_core.services.search import search_memory


@pytest.fixture
async def sparse_db(db):
    """DB with documents that DON'T contain the full identifier string."""
    db.add(Project(key="sparse", name="Sparse"))
    await db.commit()

    docs = [
        # Doc 0: mentions record_store without extension — user searches record_store.py
        ("Refactored record_store to extract validation logic", "record_store refactor"),
        # Doc 1: mentions the class without extension — user searches DataTableHeader.tsx
        ("Moved DataTableHeader to shared components", "DataTableHeader"),
        # Doc 2: only the filename, not the path — user searches app/api/routes/record_store.py
        ("Fixed the record_store route handler bug", "record_store route"),
        # Doc 3: underscore variant — user might search hyphenated
        ("Removed the feature_flag_rollout flag", "feature rollout"),
        # Doc 4: no relation — should not match
        ("Updated README with install instructions", "README"),
    ]
    for i, (body, title) in enumerate(docs):
        await fts_insert(
            db,
            title=title[:80],
            body=body,
            kind="event",
            ref_id=f"sp-{i}",
            project_key="sparse",
        )
    await db.commit()
    return db


@pytest.mark.asyncio
async def test_filename_finds_doc_without_extension(sparse_db):
    """Searching record_store.py should find a doc that mentions record_store.

    Current behavior (AND): requires BOTH "record_store" AND "py" — fails
    because doc 0 has no "py".
    Desired behavior: finds doc 0 since the base name matches.
    """
    hits = await search_memory(sparse_db, "record_store.py")
    ref_ids = {h.ref_id for h in hits}
    # Doc 0 mentions "record_store" without .py
    assert "sp-0" in ref_ids, (
        "Searching 'record_store.py' should find a doc that mentions 'record_store' — "
        "the .py extension should not be a required AND term"
    )


@pytest.mark.asyncio
async def test_class_file_finds_doc_without_extension(sparse_db):
    """Searching DataTableHeader.tsx should find doc mentioning only the class name."""
    hits = await search_memory(sparse_db, "DataTableHeader.tsx")
    ref_ids = {h.ref_id for h in hits}
    assert "sp-1" in ref_ids, (
        "Searching 'DataTableHeader.tsx' should find doc with just 'DataTableHeader'"
    )


@pytest.mark.asyncio
async def test_path_query_finds_filename_only_doc(sparse_db):
    """Searching the full path should find a doc that only mentions the filename."""
    hits = await search_memory(sparse_db, "app/api/routes/record_store.py")
    ref_ids = {h.ref_id for h in hits}
    # Doc 2 mentions "record_store route" without the full path
    assert "sp-2" in ref_ids, (
        "Full path search should find doc that only mentions 'record_store' — "
        "currently fails because 'app', 'api', 'routes' are all AND-required"
    )


@pytest.mark.asyncio
async def test_hyphenated_query_finds_space_separated_doc(sparse_db):
    """feature-flag-rollout → should find a doc with those words space-separated."""
    hits = await search_memory(sparse_db, "feature-flag-rollout")
    ref_ids = {h.ref_id for h in hits}
    # Doc 3 has "feature_flag_rollout" — underscore variant; when indexed,
    # FTS5 tokenizes it based on its own tokenizer rules
    # This is also a valid test: the words appear but not hyphenated
    assert "sp-3" in ref_ids, (
        "Hyphenated query should find doc containing those words in any form"
    )


@pytest.mark.asyncio
async def test_no_false_positives_on_sparse(sparse_db):
    """An unrelated doc (README) should not match any of our identifier queries."""
    for query in ["record_store.py", "DataTableHeader.tsx",
                  "app/api/routes/record_store.py", "feature-flag-rollout"]:
        hits = await search_memory(sparse_db, query)
        ref_ids = {h.ref_id for h in hits}
        assert "sp-4" not in ref_ids, f"README doc should not match query: {query!r}"
