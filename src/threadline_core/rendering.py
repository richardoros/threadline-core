"""Shared rendering helpers — the Jinja2 environment factory and markdown utilities.

WHY THIS MODULE EXISTS
----------------------
Multiple features (daily notes, memory compilers, dashboards, continuation
prompts) all need to render Jinja2 templates from the same ``templates/``
directory, and several of them also assemble markdown by hand. Centralising
both here means:

- One place controls autoescape, trim/lstrip settings — no scattered env
  creation that could forget autoescape on one rendering path.
- ``lru_cache`` ensures we create the Environment once per process; Jinja2
  environments are thread-safe after creation so re-use is correct and fast.
- One markdown section helper (``append_md_section``) instead of a private
  copy per service, so bullet formatting can never drift between outputs.

AUTOESCAPE NOTE
---------------
``autoescape=True`` is non-negotiable: agent summaries are free-text strings
typed (or generated) by AI agents, and they will sometimes contain characters
like ``<``, ``>``, ``&`` that would otherwise break HTML or enable injection.
Jinja2 with autoescape converts those to safe entities (``&lt;``, ``&gt;``,
``&amp;``) automatically. Never disable autoescape for any template that
renders user/agent-supplied text into HTML output.
"""

import hashlib
from functools import lru_cache
from pathlib import Path

import jinja2

# Canonical location of all Jinja2 templates — same package directory.
_TEMPLATES_DIR: Path = Path(__file__).parent / "templates"

# Restrained, visually-distinct accent hues for per-project identity. This is
# PRESENTATION metadata only — derived deterministically from the project key on
# every render, never stored. Used by the ``proj_hue`` Jinja filter as
# ``style="--proj: <hue>"``; the CSS supplies saturation/lightness per theme.
_PROJECT_HUES: tuple[int, ...] = (211, 262, 158, 24, 199, 286, 340, 132, 45, 178)


def project_hue(key: str) -> int:
    """Return a stable accent hue (0-359) for a project key — presentation only.

    Uses sha256 (NOT the builtin ``hash()``, which is salted per-process and would
    give a different colour after every restart). Picks from a curated palette so
    colours stay restrained and legible rather than landing on muddy hues.
    """
    digest = int(hashlib.sha256((key or "").encode("utf-8")).hexdigest(), 16)
    return _PROJECT_HUES[digest % len(_PROJECT_HUES)]


@lru_cache(maxsize=1)
def get_env() -> jinja2.Environment:
    """Return the shared Jinja2 Environment, creating it on first call.

    Contract:
    - Loads templates from ``src/threadline/templates/``
    - ``autoescape=True``: all variables are HTML-escaped by default
    - ``trim_blocks=True``: strips the first newline after a block tag
    - ``lstrip_blocks=True``: strips leading spaces/tabs from block lines
    - Cached: safe to call on every request; only one Environment is built

    Returns
    -------
    jinja2.Environment
        Ready-to-use environment. Call ``.get_template(path)`` with a path
        relative to the templates directory (e.g. ``"fragments/daily_note.html"``).
    """
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["proj_hue"] = project_hue
    return env


def append_md_section(
    lines: list[str], heading: str, items: list[str], *, level: int = 2
) -> None:
    """Append a markdown heading + bullet list to ``lines`` when ``items`` is non-empty.

    ``level`` controls the heading depth: 2 produces ``## Heading`` (used by
    the memory compilers), 3 produces ``### Heading`` (used by daily notes,
    where sections nest under per-project ``##`` headings).

    Empty ``items`` appends nothing — callers that need an explicit
    "(none recorded)" sentinel add it themselves. Items are stripped:
    agent text may carry leading/trailing whitespace or newlines, and an
    embedded leading newline would break the bullet out of the list structure.
    """
    if not items:
        return
    lines.append(f"{'#' * level} {heading}")
    for item in items:
        lines.append(f"- {item.strip()}")
    lines.append("")
