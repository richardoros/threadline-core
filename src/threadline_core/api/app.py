"""FastAPI application factory for threadline-core.

Mounts only the public event-ingestion and project-management API routes.
The dashboard (web.py) and static assets are private-product features and are
not included in this package.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import Depends, FastAPI

from threadline_core.api.deps import require_token
from threadline_core.api.routes_events import router as events_router
from threadline_core.api.routes_projects import router as projects_router
from threadline_core.config import Settings, get_settings
from threadline_core.db import create_engine_for, init_db
from threadline_core.db import session_factory as build_session_factory


def create_app(settings: Settings | None = None) -> FastAPI:
    """Return a configured FastAPI application.

    Parameters
    ----------
    settings:
        Override the process-wide settings singleton (useful in tests).
    """
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
        engine = create_engine_for(cfg.db_path)
        await init_db(engine)
        _app.state.engine = engine
        _app.state.factory = build_session_factory(engine)
        _app.state.settings = cfg
        yield
        await engine.dispose()

    app = FastAPI(
        title="Threadline Core",
        description="Public event-ingestion and lifecycle API.",
        lifespan=lifespan,
    )

    token_dep = Depends(require_token)
    app.include_router(events_router, prefix="/api", dependencies=[token_dep])
    app.include_router(projects_router, prefix="/api", dependencies=[token_dep])

    return app
