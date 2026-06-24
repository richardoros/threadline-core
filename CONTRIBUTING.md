# Contributing to threadline-core

threadline-core is the open substrate layer of the Threadline project. Contributions that improve the public API surface, storage layer, MCP server, connectors, documentation, or test coverage are welcome.

## What belongs here

- Bug fixes to anything in the 16-tool public MCP surface
- Improvements to the HTTP API, CLI, event ingestion, or FTS search
- New connectors (Claude Code hooks, OpenAI, Cursor, etc.)
- Storage / schema improvements that are additive and backward-compatible
- Documentation, examples, and test coverage

## What does not belong here

- Features that depend on a local LLM (Morning Pulse, memory compilation); those live in the private Threadline product
- Dashboard or visualisation UI
- Cloud integrations or remote storage backends

## Development setup

```bash
git clone https://github.com/richardoros/threadline-core
cd threadline-core
uv sync --all-groups

# Run the test suite
uv run pytest -q

# Lint
uv run ruff check .
```

## Before opening a pull request

1. **Run the full test suite:** `uv run pytest -q` must pass with zero failures.
2. **Run Ruff:** `uv run ruff check .` must report no errors.
3. **Respect the private boundary:** `src/threadline_core/` must import nothing from the private `threadline` package. `tests/test_import_boundary.py` enforces this automatically.
4. **Keep the MCP surface stable:** changes to the 16 registered tools (names, argument types, semantics) are breaking changes and require a major version bump.
5. **Add tests** for any new behaviour that could regress silently.

## Commit style

Plain imperative subject line. No scope prefix required.

```
Fix sanitize_query to strip file extensions before FTS5 tokenisation
Add get_project_state MCP tool with evidence-ids field
```

## License

By contributing you agree that your contributions will be licensed under the [Apache 2.0 License](LICENSE).
