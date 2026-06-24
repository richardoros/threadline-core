"""Shared FastAPI dependencies for the Threadline API.

This module owns the reusable dependencies used across all API routers:

``get_db``
    Yields one AsyncSession per request.  The session factory is read from
    ``request.app.state.factory`` so it works with any app instance — the
    production singleton, a test app with an isolated DB, or a future
    multi-tenant variant.

``get_settings_dep``
    Returns the app's resolved Settings from ``request.app.state.settings``.
    Route handlers declare this instead of taking a raw ``Request`` just to
    reach app state — keeps handler signatures honest about what they use.

``require_token``
    Enforces Bearer token auth when ``api_token`` is configured on
    ``app.state.settings``.  When the token is the empty string (default),
    the dependency is a no-op — auth is deliberately disabled for
    localhost/Tailscale deployments where the network is the trust boundary.

WHY A SEPARATE MODULE
----------------------
Both ``app.py`` (which builds the FastAPI app) and the route modules need
access to these dependencies.  Defining them here breaks the circular import
that would otherwise arise from ``app.py → routes_*.py → app.py``.
"""

from __future__ import annotations

import secrets
from typing import AsyncGenerator

from fastapi import Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from threadline_core.config import Settings


async def get_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """Yield one AsyncSession per request from the app's session factory.

    The factory is on ``request.app.state.factory``, populated during
    lifespan startup.  Opening the session here (not at module level)
    ensures each request gets its own isolated transaction.
    """
    async with request.app.state.factory() as session:
        yield session


def get_settings_dep(request: Request) -> Settings:
    """Return the app's resolved Settings from ``request.app.state.settings``.

    This is the dependency-injection front door for settings: handlers that
    need configuration declare this dependency instead of accepting a raw
    ``Request`` and digging into ``app.state`` themselves.
    """
    return request.app.state.settings


async def require_token(
    request: Request,
    authorization: str | None = Header(None),
) -> None:
    """Enforce Bearer token auth when ``api_token`` is configured.

    Contract:
    - api_token == "" (default): always passes — auth is disabled.
      This is intentional for localhost and Tailscale deployments where
      the network layer is the trust boundary.
    - api_token set: expects ``Authorization: Bearer <token>``.  Any other
      value (missing header, wrong scheme, wrong token) raises HTTP 401.

    Scheme matching is case-insensitive per RFC 7235 ("bearer", "BEARER",
    "Bearer" are all accepted); the token itself stays case-sensitive.

    Timing attack note: ``secrets.compare_digest`` runs in constant time so
    an attacker cannot infer the token length from response latency.
    """
    token: str = request.app.state.settings.api_token
    if not token:
        # Auth deliberately disabled — no token configured.
        return
    if authorization is None:
        raise HTTPException(status_code=401, detail="Authorization header required")
    scheme, _, provided = authorization.partition(" ")
    if scheme.lower() != "bearer" or not provided:
        raise HTTPException(status_code=401, detail="Authorization header required")
    if not secrets.compare_digest(provided, token):
        raise HTTPException(status_code=401, detail="Invalid token")
