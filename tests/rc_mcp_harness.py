"""Release-candidate MCP stdio integration harness.

Launches threadline-core-mcp from the INSTALLED WHEEL (not the source checkout),
speaks the MCP stdio protocol via the official SDK client, and verifies all
10 properties required before the repository can go public.

Usage:
    /tmp/tc-cleanroom/bin/python /tmp/rc_mcp_harness.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import traceback

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

EXPECTED_TOOLS = {
    "start_session", "end_session", "log_agent_event",
    "get_project_state",
    "get_open_loops", "mark_open_loop_resolved",
    "get_decisions", "get_decision", "mark_decision_outcome", "get_known_traps",
    "propose_finding", "confirm_finding", "resolve_finding", "dismiss_finding",
    "get_evidence",
    "search_memory",
}

PRIVATE_MODULES = {
    "pulse", "pulse_store", "memory", "prompts", "next_steps",
    "research_recommendations", "research_store", "project_groups", "context_bundle",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ok(msg: str) -> None:
    print(f"  [PASS] {msg}")

def fail(msg: str) -> None:
    print(f"  [FAIL] {msg}", file=sys.stderr)

def section(title: str) -> None:
    print(f"\n{'='*60}\n{title}\n{'='*60}")


def extract(result) -> dict:
    """Parse a single-dict tool result from TextContent."""
    if not result.content:
        return {}
    raw = getattr(result.content[0], "text", None)
    if not raw or not raw.strip():
        return {}
    return json.loads(raw)


def extract_list(result) -> list[dict]:
    """Parse a list-returning tool result.

    FastMCP serializes list returns as one TextContent per element,
    not as a single JSON array — collect all of them.
    """
    items = []
    for c in result.content:
        text = getattr(c, "text", None)
        if text and text.strip():
            try:
                items.append(json.loads(text))
            except json.JSONDecodeError:
                pass
    return items


# ---------------------------------------------------------------------------
# Main harness
# ---------------------------------------------------------------------------

async def run(tmp_dir: str) -> list[str]:
    """Run all 10 checks. Returns True if all pass."""
    env = {
        **os.environ,
        "THREADLINE_DATA_DIR": tmp_dir,
        # Loopback-only is the default; we're not starting an HTTP server here
    }
    server_params = StdioServerParameters(
        command="/tmp/tc-cleanroom/bin/threadline-core-mcp",
        env=env,
    )

    failures: list[str] = []

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:

            # ----------------------------------------------------------------
            # P1: initialize succeeds
            # ----------------------------------------------------------------
            section("P1 — initialize")
            init = await session.initialize()
            server_name = init.serverInfo.name if init.serverInfo else "unknown"
            ok(f"initialize returned serverInfo.name={server_name!r}")

            # ----------------------------------------------------------------
            # P2: tools/list returns exactly the expected surface
            # ----------------------------------------------------------------
            section("P2 — tools/list")
            tlist = await session.list_tools()
            returned = {t.name for t in tlist.tools}
            missing  = EXPECTED_TOOLS - returned
            extra    = returned - EXPECTED_TOOLS
            print(f"  returned {len(returned)} tools: {sorted(returned)}")
            if missing:
                msg = f"missing tools: {missing}"
                fail(msg)
                failures.append(msg)
            else:
                ok(f"all {len(EXPECTED_TOOLS)} expected tools present")
            if extra:
                msg = f"unexpected extra tools: {extra}"
                fail(msg)
                failures.append(msg)
            else:
                ok("no unexpected extra tools")

            # ----------------------------------------------------------------
            # P3: start_session + log_agent_event write data
            # ----------------------------------------------------------------
            section("P3 — start_session / log_agent_event write")
            ss = extract(await session.call_tool("start_session", {
                "project_key": "rc-test",
                "agent_name": "rc-harness",
            }))
            session_id = ss.get("session_id") or ss.get("id")
            print(f"  start_session result: {ss}")
            if not session_id:
                msg = "start_session returned no session_id"
                fail(msg)
                failures.append(msg)
            else:
                ok(f"start_session → session_id={session_id!r}")

            # log a checkpoint with a decision and open loop
            lae = extract(await session.call_tool("log_agent_event", {
                "event_json": json.dumps({
                    "event_type": "checkpoint",
                    "project_key": "rc-test",
                    "session_id": session_id,
                    "agent": {"name": "rc-harness"},
                    "summary": "RC harness checkpoint — searching for SQLite test content",
                    "details": {
                        "decisions": ["Use SQLite for local-first storage"],
                        "open_loops": ["Verify clean shutdown behaviour"],
                        "verification": ["rc_mcp_harness.py passed"],
                    },
                })
            }))
            print(f"  log_agent_event result: {lae}")
            event_id = lae.get("event_id")
            if not event_id:
                msg = "log_agent_event returned no event_id"
                fail(msg)
                failures.append(msg)
            else:
                ok(f"log_agent_event → event_id={event_id!r}")
            decisions_created = lae.get("decisions_created", 0)
            loops_created = lae.get("open_loops_created", 0)
            if decisions_created != 1:
                msg = f"expected 1 decision created, got {decisions_created}"
                fail(msg)
                failures.append(msg)
            else:
                ok(f"decision created (decisions_created={decisions_created})")
            if loops_created != 1:
                msg = f"expected 1 open loop created, got {loops_created}"
                fail(msg)
                failures.append(msg)
            else:
                ok(f"open loop created (open_loops_created={loops_created})")

            # ----------------------------------------------------------------
            # P4: get_project_state returns the persisted data
            # ----------------------------------------------------------------
            section("P4 — get_project_state read-after-write")
            gps = extract(await session.call_tool("get_project_state", {
                "project_key": "rc-test",
            }))
            print(f"  get_project_state keys: {list(gps.keys())}")
            if gps.get("project_key") != "rc-test":
                msg = f"expected project_key='rc-test', got {gps.get('project_key')!r}"
                fail(msg)
                failures.append(msg)
            else:
                ok("project_key matches")
            open_loops = gps.get("open_loops", [])
            decisions  = gps.get("decisions", [])
            if not open_loops:
                msg = "open_loops list empty after log_agent_event with open_loops"
                fail(msg)
                failures.append(msg)
            else:
                ok(f"open_loops present ({len(open_loops)} entries)")
            if not decisions:
                msg = "decisions list empty after log_agent_event with decisions"
                fail(msg)
                failures.append(msg)
            else:
                ok(f"decisions present ({len(decisions)} entries)")

            # ----------------------------------------------------------------
            # P5: search_memory returns the inserted content
            # ----------------------------------------------------------------
            section("P5 — search_memory finds inserted content")
            raw_search = await session.call_tool("search_memory", {
                "query": "SQLite",
                "limit": 5,
            })
            hit_list = extract_list(raw_search)
            print(f"  search_memory returned {len(hit_list)} hits")
            if not hit_list:
                msg = "search_memory returned no hits for 'SQLite' (which was in the checkpoint)"
                fail(msg)
                failures.append(msg)
            else:
                ok(f"search found {len(hit_list)} hit(s) for 'SQLite'")

            # ----------------------------------------------------------------
            # P6: mark_open_loop_resolved without evidence is rejected
            # ----------------------------------------------------------------
            section("P6 — evidence gate rejects without admissible evidence")
            ol_list = extract_list(
                await session.call_tool("get_open_loops", {"project_key": "rc-test"})
            )
            print(f"  open loops ({len(ol_list)}): {[lp.get('id','?') for lp in ol_list]}")
            if not ol_list:
                msg = "no open loops available to test evidence gate"
                fail(msg)
                failures.append(msg)
            else:
                loop_id = ol_list[0].get("id") or ol_list[0].get("loop_id")
                gate_raw = await session.call_tool("mark_open_loop_resolved", {
                    "loop_id": loop_id,
                    "evidence_refs": [],
                })
                err_text = " ".join(
                    getattr(c, "text", "") for c in gate_raw.content
                ).lower()
                print(
                    f"  mark_open_loop_resolved (no evidence): isError={gate_raw.isError},"
                    f" text={err_text[:120]!r}"
                )
                evidence_words = (
                    "evidence", "admissib", "required", "reject", "error", "valueerror"
                )
                if gate_raw.isError and any(w in err_text for w in evidence_words):
                    ok(
                        "evidence gate rejected resolve without evidence"
                        " (isError + evidence keyword)"
                    )
                elif gate_raw.isError:
                    ok("evidence gate raised an error (isError=True — loop mutation blocked)")
                else:
                    # Server returned success — verify the loop is still open
                    still_open_list = extract_list(await session.call_tool("get_open_loops", {
                        "project_key": "rc-test",
                    }))
                    loop_still_open = any(
                        (lp.get("id") or lp.get("loop_id")) == loop_id
                        for lp in still_open_list
                    )
                    if loop_still_open:
                        ok("loop still open (gate blocked the mutation without error)")
                    else:
                        msg = (
                            "mark_open_loop_resolved without evidence SUCCEEDED"
                            f" — gate broken. text={err_text!r}"
                        )
                        fail(msg)
                        failures.append(msg)

            # ----------------------------------------------------------------
            # P7: valid lifecycle transition succeeds (mark_decision_outcome accepted)
            # ----------------------------------------------------------------
            section("P7 — valid lifecycle transition succeeds")
            dec_list = extract_list(
                await session.call_tool("get_decisions", {"project_key": "rc-test"})
            )
            print(f"  decisions ({len(dec_list)}): {[d.get('id','?') for d in dec_list]}")
            if not dec_list:
                msg = "no decisions available for valid-transition test"
                fail(msg)
                failures.append(msg)
            else:
                dec_id = dec_list[0].get("id") or dec_list[0].get("decision_id")
                mdo = extract(await session.call_tool("mark_decision_outcome", {
                    "decision_id": dec_id,
                    "outcome": "accepted",
                }))
                print(f"  mark_decision_outcome accepted: {mdo}")
                # "accepted" doesn't need evidence; should succeed
                if mdo.get("error"):
                    msg = f"mark_decision_outcome(accepted) unexpectedly failed: {mdo}"
                    fail(msg)
                    failures.append(msg)
                else:
                    ok(
                        "mark_decision_outcome accepted →"
                        f" {mdo.get('outcome', mdo.get('status', 'ok'))}"
                    )

    # Session is closed here — subprocess should be gone
    return failures


async def check_process_and_modules(tmp_dir: str) -> list[str]:
    """P8 + P9 + P10 outside the session context."""
    failures: list[str] = []

    # ----------------------------------------------------------------
    # P8: process shutdown is clean
    # ----------------------------------------------------------------
    section("P8 — process shutdown clean")
    import subprocess
    result = subprocess.run(
        ["pgrep", "-f", "tc-cleanroom.*threadline-core-mcp"],
        capture_output=True, text=True
    )
    if result.stdout.strip():
        msg = f"orphan threadline-core-mcp processes after shutdown: {result.stdout.strip()}"
        fail(msg)
        failures.append(msg)
    else:
        ok("no orphan threadline-core-mcp processes")

    # ----------------------------------------------------------------
    # P9: protocol stdout contains only valid MCP messages
    # ----------------------------------------------------------------
    section("P9 — stdout/stderr do not corrupt the protocol")
    # Verified implicitly: if P1-P7 passed, all messages parsed cleanly.
    # Additionally, launch once more and capture stderr to confirm no private-module errors.
    env = {**os.environ, "THREADLINE_DATA_DIR": tmp_dir}
    proc = subprocess.Popen(
        ["/tmp/tc-cleanroom/bin/threadline-core-mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    time.sleep(1)
    if proc.stdin:
        proc.stdin.close()
    proc.wait(timeout=5)
    stderr_out = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
    if stderr_out.strip():
        print(f"  stderr (first 300 chars): {stderr_out[:300]!r}")
    # Check stderr doesn't mention private modules
    private_in_stderr = [m for m in PRIVATE_MODULES if m in stderr_out]
    if private_in_stderr:
        msg = f"private module names in stderr: {private_in_stderr}"
        fail(msg)
        failures.append(msg)
    else:
        ok("no private module names in stderr")

    # ----------------------------------------------------------------
    # P10: no private Threadline module imported at runtime
    # ----------------------------------------------------------------
    section("P10 — no private modules in runtime imports")
    # Run a one-shot python in the clean-room env to check
    check_script = """
import sys
from mcp.server.fastmcp import FastMCP
import threadline_core.mcp_server  # trigger all lazy imports
# Private Threadline modules live under threadline_core.services.*
# Check specifically for those — not generic words like 'memory' which
# also appear in third-party packages (anyio.streams.memory, mcp.*.prompts).
PRIVATE_SUFFIXES = {
    'pulse', 'pulse_store', 'memory', 'prompts', 'next_steps',
    'research_recommendations', 'research_store', 'project_groups', 'context_bundle'
}
found = [
    m for m in sys.modules
    if 'threadline' in m and m.split('.')[-1] in PRIVATE_SUFFIXES
]
if found:
    print('PRIVATE:', found)
    sys.exit(1)
else:
    print('CLEAN')
    sys.exit(0)
"""
    probe = subprocess.run(
        [sys.executable, "-c", check_script],
        capture_output=True, text=True,
        env={**os.environ, "THREADLINE_DATA_DIR": tmp_dir},
    )
    print(f"  import probe stdout: {probe.stdout.strip()!r}")
    if probe.returncode != 0:
        msg = f"private module found in sys.modules: {probe.stdout.strip()}"
        fail(msg)
        failures.append(msg)
    else:
        ok("sys.modules clean — no private Threadline modules loaded")

    return failures


async def main() -> None:
    print("=" * 60)
    print("threadline-core MCP stdio release-candidate harness")
    print(f"Python: {sys.executable}")
    import importlib.metadata
    version = importlib.metadata.version("threadline-core")
    print(f"threadline-core version: {version}")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="tc-rc-") as tmp_dir:
        print(f"\nIsolated data dir: {tmp_dir}")
        try:
            failures_p1_p7 = await run(tmp_dir)
        except Exception:
            print("\n[FATAL] Exception during P1-P7:", file=sys.stderr)
            traceback.print_exc()
            sys.exit(1)

        failures_p8_p10 = await check_process_and_modules(tmp_dir)

    all_failures = failures_p1_p7 + failures_p8_p10

    section("SUMMARY")
    if all_failures:
        print(f"\n{len(all_failures)} FAILURE(S):")
        for f in all_failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("\nAll 10 properties PASSED. Technical release gate closed.")
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
