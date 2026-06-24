"""Shared fixtures for the threadline-core test suite."""
from __future__ import annotations

import pytest
import pytest_asyncio

from threadline_core.config import Settings
from threadline_core.db import create_engine_for, init_db, session_factory


@pytest.fixture
def settings(tmp_path):
    """Settings pointing at a temporary data directory."""
    return Settings(data_dir=str(tmp_path))


@pytest_asyncio.fixture
async def db(settings):
    """AsyncSession backed by a fresh in-memory-style SQLite DB."""
    engine = create_engine_for(settings.db_path)
    await init_db(engine)
    factory = session_factory(engine)
    async with factory() as session:
        yield session
    await engine.dispose()
