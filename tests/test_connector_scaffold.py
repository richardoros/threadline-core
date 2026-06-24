"""The connector scaffold must produce config that works from a clean pip/uv
install — installed console-script entry points only, never a path into the build
machine's source checkout. This is the public-launch contract: a wheel user runs
`threadline-core connect claude-code` and gets a working Claude Code config.
"""
from __future__ import annotations

import json

import pytest

from threadline_core.services.connector_scaffold import (
    ClaudeCodeConnectorOptions,
    connector_files,
    write_connector_files,
)

# Substrings that would only appear if generated config leaked a source checkout or
# the private monorepo entry point. The user's own data_dir/project_root are legitimate
# absolute paths, so we do NOT blanket-ban "/home/" or "src/" here — the clean-room test
# asserts the build-machine path is absent against a real install.
_LEAK_MARKERS = (
    "uv run",
    "--directory",
    "threadline-mcp",  # the wrong entrypoint name — note: NOT a substring of threadline-core-mcp
    "connectors/claude-code",
    "site-packages",
    "/.venv/",
)


def _opts(tmp_path, **kw) -> ClaudeCodeConnectorOptions:
    return ClaudeCodeConnectorOptions(
        project_root=tmp_path,
        project_key="demo",
        data_dir=tmp_path / "data",
        **kw,
    )


def _file(options, path: str) -> str:
    return next(f.content for f in connector_files(options) if f.path == path)


def test_no_checkout_or_private_references(tmp_path):
    text = "\n".join(f.content for f in connector_files(_opts(tmp_path)))
    for marker in _LEAK_MARKERS:
        assert marker not in text, f"generated config leaks {marker!r}:\n{text}"


def test_mcp_uses_installed_console_script(tmp_path):
    server = json.loads(_file(_opts(tmp_path), ".mcp.json"))["mcpServers"]["threadline"]
    assert server["command"] == "threadline-core-mcp"
    assert server["args"] == []


def test_hooks_use_installed_console_scripts(tmp_path):
    hooks = json.loads(_file(_opts(tmp_path), ".claude/settings.json"))["hooks"]
    assert hooks["SessionStart"][0]["hooks"][0]["command"] == "threadline-core-session-start"
    assert hooks["SessionEnd"][0]["hooks"][0]["command"] == "threadline-core-session-end"


def test_refuses_overwrite_without_force_then_backs_up_with_force(tmp_path):
    write_connector_files(_opts(tmp_path))  # first run succeeds

    with pytest.raises(FileExistsError):  # idempotent + safe: no clobber without force
        write_connector_files(_opts(tmp_path))

    (tmp_path / ".mcp.json").write_text("OLD", encoding="utf-8")
    written = write_connector_files(_opts(tmp_path, force=True))
    assert (tmp_path / ".mcp.json.bak").read_text(encoding="utf-8") == "OLD"  # backed up
    assert any(str(p).endswith(".mcp.json.bak") for p in written)
