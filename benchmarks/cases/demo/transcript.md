# Session transcript — auth-service (raw, lightly cleaned)

> This is the kind of raw session log the next agent would otherwise re-read to figure
> out where things were left. The load-bearing facts are in here exactly once each,
> surrounded by the normal back-and-forth of a working session. The point of the
> benchmark is what survives when you only have a small token budget.

## Opening

Morning. Pulled `main`, the working tree is clean. Let me get the dev server up before
we start so we're not waiting on it later. There's a stale lockfile from yesterday's
crash, removing it. Okay, server is listening on 8080.

Quick recap of where we are: the project skeleton is in place, the routing layer is
wired, and the health check endpoint returns 200. The test suite has 41 tests and they
all pass locally, though one of them is flaky on CI for reasons that look like a timing
issue in the fixture teardown rather than anything real. We should come back to that but
it isn't blocking.

Before the substantive work, some housekeeping. The linter config drifted from the rest
of the org's repos, so I aligned it: line length back to 100, import sorting on, and the
pre-commit hook now runs the formatter. I also bumped two dev dependencies that had
advisories against them; nothing in the runtime path changed. The Makefile target names
were inconsistent (`make test` vs `make run-tests`) so I collapsed them to the short
forms. None of this changes behaviour, it's just hygiene so the diff is readable.

One more thing on environment: the container base image moved to the slim variant, which
shaved about 200MB off the build, and the CI cache now keys on the lockfile hash so cold
builds are rare. Good. That's the warm-up done, let's get into the actual design.

## Design discussion (the substance)

Okay, authentication. The core question is how we represent a logged-in user across
requests. We went back and forth. Server-side session cookies are simple and easy to
revoke, but they need a shared session store and they make horizontal scaling annoying.
The decision: use JWT for auth, and keep it stateless — no server-side session table,
the token carries the claims and we verify the signature. We accept that revocation is
harder and we'll deal with it with short expiries plus a deny-list only if we actually
need one.

For storage: we're putting users in Postgres. We looked at MySQL since ops already runs
it, but we want the JSONB columns and stricter typing, so Postgres it is, with Alembic
for migrations. The users table is minimal for now: id, email, password hash, created_at.

Password hashing: we'll use bcrypt at cost factor 12. We benchmarked 10, 12, and 14 on
the target hardware; 12 lands around 250ms which is the sweet spot between brute-force
resistance and not melting the login path under load.

Two things we are explicitly NOT doing in v1, so write them down as open work. First,
there is no rate limiting on the login endpoint yet — that's a real gap before we expose
this to the internet, but it's out of scope for the first cut. Second, the email
verification flow is deferred until after v1; signup will create the account as active
for now and we'll bolt verification on later.

A bug we just hit and need to remember: token expiry was computed with
datetime.utcnow(), which returns a naive datetime, and comparing it against tz-aware
timestamps from the database silently produced wrong expiries — tokens looked valid for
an extra hour. Do not use utcnow() here; use tz-aware datetimes everywhere on the token
path. That one cost us an afternoon.

Last thing to flag while it's fresh: right now, in dev mode, the /admin routes bypass the
auth middleware entirely so we can poke at them without logging in. That is fine locally
but it is a loaded gun — it must be fixed before this goes anywhere near production.

## Wrap-up

That's a good chunk of progress. Let me summarise what landed in code versus what's just
decided. Landed: the login handler skeleton, the user model, and the migration that
creates the table. Decided-but-not-yet-built: the throttling, the post-signup flow, and
the production hardening of the routes we loosened for local work.

Next session I'd start by writing the handler tests properly — table-driven, covering the
happy path and the obvious failure modes — then circle back to the gap we flagged. I'll
also open a tracking note for the flaky CI test so it doesn't get lost.

Logistics: standup moved to 10:30 tomorrow, the staging environment is being rebuilt this
evening so it may be down for a bit, and the design review for the notifications service
is Thursday, unrelated to us but worth knowing. I pushed a branch with the hygiene
changes separately so they can be reviewed on their own. Calling it here for today.
