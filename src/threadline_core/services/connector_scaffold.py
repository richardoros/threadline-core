"""Project connector scaffolding.

Threadline's connector docs are precise, but manual copy/paste is easy to get
wrong. This module turns the Claude Code connector contract into a small file
manifest that can be rendered, tested, and written atomically enough for a local
developer tool. It deliberately writes only project-local files and refuses to
replace existing files unless the caller passes ``force=True``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_GH_READONLY_RULES = [
    "Bash(gh pr view:*)",
    "Bash(gh pr list:*)",
    "Bash(gh pr diff:*)",
    "Bash(gh pr checks:*)",
    "Bash(gh issue view:*)",
    "Bash(gh issue list:*)",
    "Bash(gh repo view:*)",
    "Bash(gh run view:*)",
    "Bash(gh run list:*)",
    "Bash(gh search:*)",
    "Bash(gh status:*)",
]


@dataclass(frozen=True)
class ClaudeCodeConnectorOptions:
    """Inputs needed to scaffold a Claude Code project connector."""

    project_root: Path
    project_key: str
    data_dir: Path
    endpoint: str = "http://127.0.0.1:8400/api/events"
    force: bool = False
    # ponytail: off by default; gh runs outside bubblewrap/Seatbelt for TLS
    allow_gh_cli: bool = False


@dataclass(frozen=True)
class ConnectorFile:
    """One generated connector file, relative to the project root."""

    path: str
    content: str


def _json(data: object) -> str:
    """Return stable human-readable JSON for generated connector files."""
    return json.dumps(data, indent=2) + "\n"


def _hook(command: str) -> list[dict[str, object]]:
    """Claude Code hook wrapper shape for one command."""
    return [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                    "timeout": 15,
                }
            ],
        }
    ]


def _mcp_json(options: ClaudeCodeConnectorOptions) -> str:
    """Render project-scoped MCP registration for the Threadline stdio server.

    Invokes the installed ``threadline-core-mcp`` console script (a project entry
    point), so the generated config works from a ``pip``/``uv`` install with no source
    checkout present — never a checkout-relative ``uv run --directory`` command.
    """
    return _json(
        {
            "mcpServers": {
                "threadline": {
                    "command": "threadline-core-mcp",
                    "args": [],
                    "env": {
                        "THREADLINE_DATA_DIR": str(options.data_dir),
                    },
                }
            }
        }
    )


def _settings_json(options: ClaudeCodeConnectorOptions) -> str:
    """Render project-local Claude Code hook settings for Threadline session capture.

    Hooks invoke the installed ``threadline-core-session-start`` / ``-session-end``
    console scripts (project entry points on ``PATH``), never a checkout-relative file
    path — so a ``pip``/``uv``-installed user gets a working config with no source tree.
    """
    project_root = options.project_root.expanduser().resolve()

    data: dict[str, object] = {
        "env": {
            "THREADLINE_ENDPOINT": options.endpoint,
            "THREADLINE_DATA_DIR": str(options.data_dir),
            "THREADLINE_PROJECT_MAP": f"{project_root}={options.project_key}",
        },
        "hooks": {
            "SessionStart": _hook("threadline-core-session-start"),
            "SessionEnd": _hook("threadline-core-session-end"),
        },
    }
    if options.allow_gh_cli:
        data["permissions"] = {
            "allow": _GH_READONLY_RULES,
            # Block 'gh auth' to prevent --show-token leaking credentials.
            "deny": ["Bash(gh auth:*)"],
        }
    return _json(data)


def _claude_snippet(options: ClaudeCodeConnectorOptions) -> str:
    """Render the agent-facing Threadline instructions for a project."""
    key = options.project_key
    return f"""# Threadline integration instructions

Paste this block into your project's CLAUDE.md to enable live memory reporting.

---

## Threadline - session reporting protocol

Threadline is running at {options.endpoint} and tracks continuity for this project.
Report events through the `threadline` MCP server.

Project key: `{key}`

**Session lifecycle is automatic.**
Session open and close are handled by the SessionStart and SessionEnd hooks.
Do not call `start_session` just to open a session; the hook already did that.
Use `log_agent_event` for rich checkpoints: decisions, blockers, verification
results, objective changes, and handoffs.

**At session start:**
1. Call `get_context_bundle(project_key="{key}")` for summary-first orientation.
2. Drill in only when needed with `get_open_loops`, `get_findings`,
   `get_known_traps`, `get_decision_detail`, or `get_evidence`.

**While working:**
- Call `log_agent_event(event_json="<AgentEventIn JSON as a string>")` at
  meaningful checkpoints, not after every file edit.
- Set `project_key` to `{key}` in every event.
- Include structured `details` where useful:
  - `decisions`: choices made during this unit of work.
  - `open_loops`: things noticed but deliberately deferred.
  - `files_changed`: files created or modified.
  - `verification`: commands run and their outcomes.

**When the user makes a decision:**
- Log a `decision` event while the context is fresh. Include the why, not just
  the what.

**When blocked:**
- Log a `blocker` event with what is missing and what would unblock the next
  agent.

**Before handing off:**
- Call `generate_continuation_prompt(project_key="{key}")` to produce a ready
  context block for the next agent.

**Privacy:**
- Never include raw secrets, passwords, API keys, or tokens in summaries or
  details.
- If a summary might contain a secret, set `privacy.contains_secrets = true`.
"""


def connector_files(options: ClaudeCodeConnectorOptions) -> list[ConnectorFile]:
    """Return the complete project-local Claude Code connector manifest."""
    return [
        ConnectorFile(".mcp.json", _mcp_json(options)),
        ConnectorFile(".claude/settings.json", _settings_json(options)),
        ConnectorFile(".threadline/CLAUDE-snippet.md", _claude_snippet(options)),
    ]


def write_connector_files(options: ClaudeCodeConnectorOptions) -> list[Path]:
    """Write the connector manifest under ``project_root``.

    Raises ``FileExistsError`` before writing anything if any target exists and
    ``force`` is false.
    """
    root = options.project_root.expanduser()
    if not root.is_dir():
        raise NotADirectoryError(f"Project root is not an existing directory: {root}")

    manifest = connector_files(options)
    targets = [(item, root / item.path) for item in manifest]
    existing = [item.path for item, target in targets if target.exists()]
    if existing and not options.force:
        joined = ", ".join(existing)
        raise FileExistsError(f"Connector file already exists: {joined}")

    written: list[Path] = []
    for item, target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            # We only reach here with force=True (otherwise we raised above). Back the
            # existing file up before overwriting so prior Claude config is never lost.
            backup = target.parent / (target.name + ".bak")
            backup.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
            written.append(Path(item.path + ".bak"))
        target.write_text(item.content, encoding="utf-8")
        written.append(Path(item.path))
    return written
