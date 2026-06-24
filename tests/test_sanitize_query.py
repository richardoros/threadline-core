"""TDD red phase: document the sanitize_query bug before fixing it.

Tests in this file are grouped by what they test:
  - sanitize_query() output (pure function, no DB)
  - FTS5 round-trip: index a document, search for it using a real query

The FTS5 round-trip tests reveal whether the sanitized expression actually
finds what we'd expect in SQLite's unicode61 tokenizer.
"""
from __future__ import annotations

import pytest

from threadline_core.models import Project
from threadline_core.services.fts import fts_insert
from threadline_core.services.search import sanitize_query, search_memory

# ---------------------------------------------------------------------------
# Unit: sanitize_query output
# ---------------------------------------------------------------------------

class TestSanitizeQueryOutput:
    """Verify the expression sanitize_query produces for various inputs."""

    def test_plain_word(self):
        assert sanitize_query("hello") == '"hello"'

    def test_underscore_identifier(self):
        # `_` is a \w char — expect it kept in one token
        result = sanitize_query("record_store")
        assert result == '"record_store"'

    def test_dotted_filename(self):
        # Extension stripped — only the stem is a required AND term
        result = sanitize_query("DataTableHeader.tsx")
        assert result == '"DataTableHeader"'

    def test_py_filename(self):
        # .py stripped so searching record_store.py finds docs that say just "record_store"
        result = sanitize_query("record_store.py")
        assert result == '"record_store"'

    def test_path(self):
        # Path → basename, then extension stripped → single meaningful stem
        result = sanitize_query("app/api/routes/record_store.py")
        assert result == '"record_store"'

    def test_hyphenated_identifier(self):
        # Hyphens are separators — split into three AND tokens
        result = sanitize_query("feature-flag-rollout")
        assert result == '"feature" "flag" "rollout"'

    def test_empty(self):
        assert sanitize_query("") == ""

    def test_only_operators(self):
        assert sanitize_query("AND OR NOT") == '"AND" "OR" "NOT"'

    def test_fts5_injection(self):
        # Should never leak raw FTS5 syntax
        result = sanitize_query('NEAR("foo" "bar", 5)')
        assert "NEAR" in result
        assert "(" not in result

    def test_mixed_separators(self):
        # Path → basename "baz-qux", no extension → ["baz", "qux"]
        result = sanitize_query("foo.bar/baz-qux")
        assert result == '"baz" "qux"'


# ---------------------------------------------------------------------------
# FTS5 round-trip: does the query actually find the indexed document?
# ---------------------------------------------------------------------------

@pytest.fixture
async def indexed_db(db):
    """DB with a handful of representative documents indexed."""
    db.add(Project(key="fts-test", name="FTS Test"))
    await db.commit()

    docs = [
        ("Modified DataTableHeader.tsx to fix animation",
         "DataTableHeader.tsx"),
        ("Updated record_store.py with new validator logic",
         "record_store.py"),
        ("Changed app/api/routes/record_store.py route handler",
         "app/api/routes/record_store.py"),
        ("Introduced feature-flag-rollout feature flag",
         "feature-flag-rollout"),
        ("Added tests for login_form component",
         "login_form"),
        ("Refactored the auth module",
         "auth module"),
    ]
    for i, (body, title) in enumerate(docs):
        await fts_insert(
            db,
            title=title[:80],
            body=body,
            kind="event",
            ref_id=f"fts-ev-{i}",
            project_key="fts-test",
        )
    await db.commit()
    return db


@pytest.mark.asyncio
async def test_fts_underscore_identifier(indexed_db):
    """record_store (underscore) should find the record_store.py document."""
    hits = await search_memory(indexed_db, "record_store")
    ref_ids = {h.ref_id for h in hits}
    # Should find BOTH the standalone record_store.py AND the path document
    assert "fts-ev-1" in ref_ids, "record_store query should match 'record_store.py' doc"
    assert "fts-ev-2" in ref_ids, "record_store query should match path doc"


@pytest.mark.asyncio
async def test_fts_dotted_filename(indexed_db):
    """DataTableHeader.tsx should find the document that mentions it."""
    hits = await search_memory(indexed_db, "DataTableHeader.tsx")
    ref_ids = {h.ref_id for h in hits}
    assert "fts-ev-0" in ref_ids, (
        "DataTableHeader.tsx query should match the animation doc"
    )


@pytest.mark.asyncio
async def test_fts_py_filename(indexed_db):
    """record_store.py should find documents mentioning record_store.py."""
    hits = await search_memory(indexed_db, "record_store.py")
    ref_ids = {h.ref_id for h in hits}
    assert "fts-ev-1" in ref_ids, "record_store.py query should find the validator doc"


@pytest.mark.asyncio
async def test_fts_path_query(indexed_db):
    """app/api/routes/record_store.py should find the document that mentions it."""
    hits = await search_memory(indexed_db, "app/api/routes/record_store.py")
    ref_ids = {h.ref_id for h in hits}
    assert "fts-ev-2" in ref_ids, (
        "Path query should find the route handler doc — "
        "sanitize_query reduces to basename stem 'record_store' before querying"
    )


@pytest.mark.asyncio
async def test_fts_hyphenated_identifier(indexed_db):
    """feature-flag-rollout should find the feature flag document."""
    hits = await search_memory(indexed_db, "feature-flag-rollout")
    ref_ids = {h.ref_id for h in hits}
    assert "fts-ev-3" in ref_ids, (
        "Hyphenated identifier query should find the feature flag doc"
    )


@pytest.mark.asyncio
async def test_fts_no_false_positives(indexed_db):
    """A specific filename query should NOT match unrelated documents."""
    hits = await search_memory(indexed_db, "DataTableHeader.tsx")
    ref_ids = {h.ref_id for h in hits}
    assert "fts-ev-3" not in ref_ids, "Should not match the feature doc"
    assert "fts-ev-4" not in ref_ids, "Should not match the login_form doc"
