# Resume payload — auth-service

Composed from threadline core records (objective + in-force decisions + open loops + known traps + active caveats). This is what the next session is handed instead of the transcript.

**Objective:** Ship the v1 authentication service (login, signup, session handling) for the API.

**Decisions in force:**
- Use JWT (stateless) for auth, not server-side session cookies.
- Store users in Postgres (not MySQL); migrations via Alembic.
- Hash passwords with bcrypt at cost factor 12.

**Open loops:**
- Rate limiting on the login endpoint is not implemented yet.
- Email verification flow is deferred until after v1.

**Known traps (do not repeat):**
- Do not use datetime.utcnow() for token expiry; the naive value caused a timezone bug. Use tz-aware datetimes.

**Active caveats:**
- In dev mode the /admin routes bypass the auth middleware. Must be fixed before production.
