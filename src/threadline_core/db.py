"""Database engine and session factory for Threadline."""
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from threadline_core.models import Base

_FTS_DDL = """\
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts
USING fts5(title, body, kind UNINDEXED, ref_id UNINDEXED, project_key UNINDEXED)
"""


def create_engine_for(db_path: Path) -> AsyncEngine:
    """Create an async SQLite engine for ``db_path``, creating the parent
    directory first so a fresh install never fails on a missing folder.

    Foreign key enforcement is enabled on every new connection: SQLite
    defaults to OFF, which would let rows silently reference missing parents.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_fks(dbapi_conn, _record) -> None:
        # PRAGMA is per-connection in SQLite, so it must run on every connect.
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build the session factory used for all DB work.

    ``expire_on_commit=False`` means object attributes stay readable after
    ``commit()`` instead of being expired. With the default (True), touching
    an attribute after commit triggers a lazy SELECT, which raises in an
    async context. The trade-off: attributes may be stale after commit —
    call ``session.refresh(obj)`` when you need fresh data from the DB.
    """
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    """Create all model tables plus the ``memory_fts`` full-text index.

    Idempotent: ``create_all`` and the FTS DDL both no-op if the tables
    already exist, so this is safe to run on every startup.

    IMPORTANT — why ``create_all`` alone is insufficient:
    ``Base.metadata.create_all`` creates tables that are missing entirely, but
    it does NOT add new columns to tables that already exist in the database.
    The live SQLite file was created by an earlier version of the schema, so
    any newly added columns are invisible until explicitly ``ALTER TABLE``'d in.
    ``_migrate_agent_sessions`` fills this gap by detecting missing columns via
    ``PRAGMA table_info`` and issuing one ``ADD COLUMN`` per missing column.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text(_FTS_DDL))
        await _migrate_agent_sessions(conn)


async def _migrate_agent_sessions(conn) -> None:
    """Add any new columns to ``agent_sessions`` that are missing from the live DB.

    ``create_all`` will not ALTER an existing table, so columns added after the
    initial deploy must be detected and applied here on every startup.  This
    function is idempotent: it inspects the current table schema via
    ``PRAGMA table_info`` and only issues ``ALTER TABLE ... ADD COLUMN`` for
    columns that are absent. Columns already present are left untouched.

    New columns and their SQLite DDL fragments. The list must stay in sync with
    the AgentSession model; add a new entry here whenever a new nullable column
    is added to AgentSession.  Non-nullable columns with no DEFAULT cannot be
    safely added to an existing table via ALTER — always add nullable or
    DEFAULT-bearing columns only.
    """
    # Maps column name -> SQLite DDL fragment to use in ALTER TABLE.
    # All new columns are nullable (no NOT NULL), which SQLite requires for
    # ADD COLUMN (a non-nullable column without a default cannot be added to an
    # existing table).
    _NEW_COLUMNS: dict[str, str] = {
        "external_session_id": "TEXT",
        "cwd": "TEXT",
        "repo": "TEXT",
        "branch": "TEXT",
        "source": "TEXT",
    }

    # PRAGMA table_info returns one row per column: (cid, name, type, notnull, dflt_value, pk)
    result = await conn.execute(text("PRAGMA table_info(agent_sessions)"))
    existing_columns = {row[1] for row in result.fetchall()}

    for col_name, col_type in _NEW_COLUMNS.items():
        if col_name not in existing_columns:
            await conn.execute(
                text(f"ALTER TABLE agent_sessions ADD COLUMN {col_name} {col_type}")
            )
