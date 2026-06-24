"""Full-text search (FTS) helpers — the single owner of the memory_fts contract.

WHY THIS MODULE EXISTS
----------------------
``memory_fts`` is a SQLite FTS5 virtual table shared by the ingest service,
the daily-note generator, and future compilers. Keeping the insert/delete
helpers here means:

- One place knows the column names. If the FTS schema in ``db.py`` changes,
  only this file needs to update.
- Callers (``services/ingest.py``, ``services/daily.py``) import these helpers
  rather than duplicating the SQL, which removes a class of schema-drift bugs.

FTS COLUMN CONTRACT (mirrors db.py ``_FTS_DDL``)
-------------------------------------------------
``title``       — short label, shown in search results (max FTS_TITLE_MAX chars)
``body``        — full searchable text (not stored separately; FTS indexes it)
``kind``        — row type: ``"event"``, ``"decision"``, ``"open_loop"``,
                  ``"daily_note"``
``ref_id``      — primary key of the row this FTS entry describes
``project_key`` — project the row belongs to (UNINDEXED — for filtering)
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

FTS_TITLE_MAX = 80  # characters — truncation limit for FTS title field


async def fts_insert(
    db: AsyncSession,
    *,
    title: str,
    body: str,
    kind: str,
    ref_id: str,
    project_key: str,
) -> None:
    """Insert one row into ``memory_fts``.

    All parameters are keyword-only to prevent accidental positional mismatches.
    ``title`` is expected to be pre-truncated to FTS_TITLE_MAX by the caller if
    needed; this function does not truncate automatically.

    Contract: callers must ensure ``ref_id`` is unique for the given ``kind``
    before inserting, or delete stale FTS rows first (use ``fts_delete_for``).
    """
    await db.execute(
        text(
            "INSERT INTO memory_fts(title, body, kind, ref_id, project_key)"
            " VALUES (:title, :body, :kind, :ref_id, :project_key)"
        ),
        {
            "title": title,
            "body": body,
            "kind": kind,
            "ref_id": ref_id,
            "project_key": project_key,
        },
    )


async def fts_delete_for(db: AsyncSession, *, kind: str, ref_id: str) -> None:
    """Delete all FTS rows matching ``kind`` + ``ref_id``.

    WHY THIS IS NEEDED: FTS5 tables do not enforce uniqueness on any column.
    When a daily note is regenerated, the old FTS row would remain alongside
    the new one, causing duplicate results for any search that matches the
    note. Callers must invoke this before re-inserting to keep the FTS index
    clean. This is the only sanctioned way to remove FTS rows for a known
    ``ref_id``.
    """
    await db.execute(
        text(
            "DELETE FROM memory_fts WHERE kind=:kind AND ref_id=:ref_id"
        ),
        {"kind": kind, "ref_id": ref_id},
    )
