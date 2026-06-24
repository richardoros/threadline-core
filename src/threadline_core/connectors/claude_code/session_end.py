"""Threadline SessionEnd hook for Claude Code.

This script is registered as a Claude Code SessionEnd hook.  Every time a
Claude Code session ends, Claude Code calls it with a JSON payload on stdin.
The script builds a Threadline event and POSTs it to the local Threadline API
so the session is captured in long-term memory.

HOW IT IS CALLED
----------------
Claude Code invokes this script as a subprocess, piping JSON on stdin:

    python3 /path/to/threadline_session_end.py

stdin JSON shape (Claude Code SessionEnd payload):
    {
        "session_id": "...",
        "cwd": "/absolute/path/to/project",
        "transcript_path": "/path/to/session.jsonl",  # path string, not contents
        "reason": "user_exit"   # optional
    }

DESIGN DECISIONS
----------------
- STDLIB ONLY (json, os, sys, pathlib, urllib.request, re) — this script
  must never require a pip install; it runs inside whatever Python Claude Code
  finds on the system.
- Exit code is ALWAYS 0.  A non-zero exit would surface an error inside the
  user's Claude Code session.  Any failure here is logged to stderr and
  silently swallowed so the user never sees a hook failure.
- Short HTTP timeout (3 s).  A hook must not hang Claude Code.  If the
  Threadline server is down, the POST fails silently and Claude Code carries on.
- Privacy: only session_id, cwd-derived project key, and the TRANSCRIPT PATH
  (a string — not the transcript contents) are sent to Threadline.

CONFIGURATION (environment variables)
--------------------------------------
THREADLINE_ENDPOINT      Full URL of the events API.
                         Default: http://127.0.0.1:8400/api/events
THREADLINE_API_TOKEN     Bearer token for the Threadline API.
                         Leave unset or empty for localhost (no-auth default).
THREADLINE_PROJECT_MAP   Comma-separated path=key pairs for explicit project
                         mapping.  Format: "/path/to/project=project-key,...".
                         Longest-prefix match wins.  If no entry matches, the
                         project key is derived from the basename of cwd.
                         Example: "/home/user/acme-api=acme-api,/home/user=home"
"""

import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Optional

DEFAULT_ENDPOINT = "http://127.0.0.1:8400/api/events"


# ---------------------------------------------------------------------------
# Project key helpers
# ---------------------------------------------------------------------------


def slugify_project_key(name: str) -> str:
    """Convert an arbitrary string into a valid Threadline project key.

    Rules (must produce a string matching ^[a-z0-9][a-z0-9-_]*$):
    1. Lowercase everything.
    2. Replace any character that is not [a-z0-9-_] with a hyphen.
    3. Collapse consecutive hyphens into one.
    4. Strip leading characters that are not [a-z0-9] (project_key must
       start with a letter or digit).
    5. If the result is empty after all that, return "unknown-project".

    Examples:
        "My Project!" -> "my-project"
        "Acme API"   -> "acme-api"
        "---"         -> "unknown-project"
        "acme-api"   -> "acme-api"
    """
    # Step 1: lowercase
    slug = name.lower()
    # Step 2: replace non-allowed chars with hyphens
    slug = re.sub(r"[^a-z0-9\-_]", "-", slug)
    # Step 3: collapse repeated hyphens
    slug = re.sub(r"-{2,}", "-", slug)
    # Step 4: strip leading characters that are not [a-z0-9]
    slug = re.sub(r"^[^a-z0-9]+", "", slug)
    # Step 4b: strip trailing hyphens (e.g. "my-project!" → "my-project-" → "my-project")
    slug = slug.rstrip("-")
    # Step 5: fallback if empty
    if not slug:
        return "unknown-project"
    return slug


def resolve_project_key(cwd: str) -> str:
    """Derive the Threadline project key for the given working directory.

    Resolution order:
    1. THREADLINE_PROJECT_MAP env var — comma-separated "path=key" pairs.
       The longest path prefix that matches cwd wins.  This lets you map
       nested repos independently (e.g. "/home/x/acme-api=acme-api" wins over
       "/home/x=home" when cwd starts with "/home/x/acme-api").
    2. Fallback: slugify(basename(cwd)).

    Args:
        cwd: Absolute path to the project directory.

    Returns:
        A valid project key string (matches ^[a-z0-9][a-z0-9-_]*$).
    """
    project_map_raw = os.environ.get("THREADLINE_PROJECT_MAP", "")
    if project_map_raw.strip():
        # Parse "path=key,path=key" into a list of (path, key) pairs
        entries: list[tuple[str, str]] = []
        for entry in project_map_raw.split(","):
            entry = entry.strip()
            if "=" not in entry:
                continue
            path_part, _, key_part = entry.partition("=")
            path_part = path_part.strip()
            key_part = key_part.strip()
            if path_part and key_part:
                entries.append((path_part, key_part))

        # Longest-prefix match: sort by path length descending so the most
        # specific path wins when multiple prefixes match.
        entries.sort(key=lambda pair: len(pair[0]), reverse=True)
        for mapped_path, mapped_key in entries:
            # Ensure we match on path boundaries, not mid-directory-name
            if cwd == mapped_path or cwd.startswith(mapped_path.rstrip("/") + "/"):
                return mapped_key

    # Fallback: derive from basename
    return slugify_project_key(Path(cwd).name)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _git_repo_and_branch(cwd: str) -> tuple[Optional[str], Optional[str]]:
    """Return (repo_basename, branch_name) for the repo at ``cwd``, best-effort.

    Uses ``git -C <cwd>`` so it works regardless of the current process cwd.
    Any failure (not a git repo, git not on PATH, timeout, etc.) returns
    ``(None, None)`` silently — missing repo/branch metadata is non-fatal.

    Args:
        cwd: Absolute path to the project directory.

    Returns:
        A tuple (repo, branch) where either may be None on failure.
    """
    try:
        toplevel = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        repo: Optional[str] = None
        if toplevel.returncode == 0:
            repo = Path(toplevel.stdout.strip()).name or None
    except Exception:
        repo = None

    try:
        branch_result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        branch: Optional[str] = None
        if branch_result.returncode == 0:
            branch = branch_result.stdout.strip() or None
    except Exception:
        branch = None

    return (repo, branch)


# ---------------------------------------------------------------------------
# Event construction
# ---------------------------------------------------------------------------


def build_event(payload: dict) -> dict:
    """Build an AgentEventIn-shaped dict from a Claude Code SessionEnd payload.

    The top-level ``session_id`` is set to None — that field is reserved for
    Threadline's own internal AgentSession primary key, which hooks do not
    know.  The client's Claude Code session id is carried in
    ``details.client_session_id`` instead, where the ingest service can use it
    to look up and close the matching AgentSession via
    ``AgentSession.external_session_id``.

    Args:
        payload: The JSON object Claude Code sends on stdin for the SessionEnd
                 hook.  Expected keys: session_id, cwd, transcript_path,
                 reason (optional).

    Returns:
        A dict that satisfies AgentEventIn's schema — ready to POST to the API.
    """
    cwd: str = payload.get("cwd", "")
    transcript_path: Optional[str] = payload.get("transcript_path")
    reason: Optional[str] = payload.get("reason")
    # Claude Code's own session id — carried in details, NOT as top-level session_id.
    # The top-level session_id is Threadline's internal AgentSession pk; hooks don't
    # know it.  The ingest service matches via details.client_session_id instead.
    client_session_id: Optional[str] = payload.get("session_id")

    project_key = resolve_project_key(cwd) if cwd else "unknown-project"
    repo, branch = _git_repo_and_branch(cwd) if cwd else (None, None)

    # Build the human-readable summary.  The reason is appended when present
    # so daily notes show why the session ended without requiring details lookup.
    summary = f"Claude Code session ended in {cwd or 'unknown'}"
    if reason:
        summary = f"{summary} ({reason})"

    return {
        "event_type": "session_ended",
        "project_key": project_key,
        "agent": {"name": "claude_code", "type": "coding_agent"},
        # Top-level session_id is None: Threadline's internal id is unknown at hook time.
        # The client id lives in details.client_session_id for the ingest service to match.
        "session_id": None,
        "summary": summary,
        "details": {
            "client_session_id": client_session_id,
            "transcript_path": transcript_path,
            "repo": repo,
            "branch": branch,
            "hook": "SessionEnd",
        },
        "privacy": {
            "contains_secrets": False,
            "allow_cloud_processing": False,
        },
    }


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------


def post_event(event: dict, endpoint: str, token: str, timeout: float = 3.0) -> bool:
    """POST the event dict to the Threadline HTTP API.

    Uses only urllib (stdlib) — no requests, no httpx.

    The timeout MUST stay short.  This function is called from a hook that
    runs synchronously while Claude Code is shutting down.  A long timeout
    would stall the user's terminal for several seconds every session exit.
    3 seconds is generous; the local Threadline server should respond in <50 ms.

    Args:
        event:    AgentEventIn-shaped dict to POST as JSON.
        endpoint: Full URL, e.g. "http://127.0.0.1:8400/api/events".
        token:    Bearer token.  Pass empty string "" for no-auth deployments.
        timeout:  Seconds before the POST is abandoned.

    Returns:
        True if the server responded with any HTTP response (even non-2xx
        — let the server validate the payload), False if a network error
        prevented the request from reaching the server.  Never raises.
    """
    body = json.dumps(event).encode("utf-8")
    req = urllib.request.Request(
        url=endpoint,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    # Only attach Authorization when a token is configured.  Localhost
    # deployments run without auth (api_token="") — don't send an empty header.
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception:
        # Swallow all errors — network failures, HTTP errors, timeouts.
        # A hook must never crash Claude Code by raising an exception.
        return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """Read the Claude Code SessionEnd hook payload from stdin, post to Threadline.

    EXIT CODE IS ALWAYS 0.
    ----------------------
    Claude Code interprets a non-zero hook exit code as an error and may
    display a warning to the user or abort the shutdown sequence.  Even if
    the Threadline server is unreachable or the payload is malformed, we
    must exit 0 to keep the user's session clean.  Failures are logged to
    stderr only — they are informational and do not require user action.

    Returns:
        Always 0.
    """
    endpoint = os.environ.get("THREADLINE_ENDPOINT", DEFAULT_ENDPOINT)
    token = os.environ.get("THREADLINE_API_TOKEN", "")

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
    except Exception as exc:
        print(f"threadline-hook: could not parse stdin: {exc}", file=sys.stderr)
        return 0

    try:
        event = build_event(payload)
    except Exception as exc:
        print(f"threadline-hook: could not build event: {exc}", file=sys.stderr)
        return 0

    success = post_event(event, endpoint, token)
    if not success:
        print(
            f"threadline-hook: could not POST to {endpoint} (server down or unreachable)",
            file=sys.stderr,
        )

    # Always return 0 — see docstring above.
    return 0


if __name__ == "__main__":
    sys.exit(main())
