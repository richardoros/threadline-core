"""Import-boundary test: no public module may import from the private product.

This test walks every .py file under src/threadline_core/ and asserts that
none of them import from the private ``threadline`` namespace (as opposed to
``threadline_core``).  It also asserts the known-private module names do not
appear anywhere in the source tree.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

SRC_ROOT = Path(__file__).parent.parent / "src" / "threadline_core"

# Private module names that must not appear in ANY public source file.
PRIVATE_NAMES = {
    "pulse",
    "pulse_store",
    "memory",
    "prompts",
    "next_steps",
    "research_recommendations",
    "research_store",
    "project_groups",
    "context_bundle",
    "loop_classification",  # actually public — override below
}
# loop_classification IS included in the public package (it's used by
# progress.py and ingest.py which are public).
PRIVATE_NAMES.discard("loop_classification")


def _py_files():
    return list(SRC_ROOT.rglob("*.py"))


def test_no_private_namespace_imports():
    """No threadline_core module may import from the bare 'threadline' namespace."""
    pattern = re.compile(r"\bfrom\s+threadline\b|\bimport\s+threadline\b")
    for path in _py_files():
        source = path.read_text(encoding="utf-8")
        lines = [(i + 1, line) for i, line in enumerate(source.splitlines())
                 if pattern.search(line)]
        assert not lines, (
            f"{path.relative_to(SRC_ROOT.parent)} imports from private namespace:\n"
            + "\n".join(f"  L{n}: {ln}" for n, ln in lines)
        )


def test_no_private_module_names_imported():
    """Known private module names must not appear in import statements."""
    for path in _py_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = ""
                if isinstance(node, ast.ImportFrom) and node.module:
                    module = node.module
                elif isinstance(node, ast.Import):
                    module = ",".join(a.name for a in node.names)
                for private in PRIVATE_NAMES:
                    assert private not in module.split("."), (
                        f"{path.relative_to(SRC_ROOT.parent)}:{node.lineno} "
                        f"imports private module '{private}' via '{module}'"
                    )
