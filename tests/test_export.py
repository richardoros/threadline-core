"""Regression: exporting a vault that contains a daily note must not crash.

export._export_daily_notes referenced ``note.content``, but the DailyNote field
is ``note.markdown`` — so `threadline-core export` raised AttributeError whenever
any daily note existed. This guards that exact path.
"""
import pytest

from threadline_core.models import DailyNote
from threadline_core.services.export import export_all


@pytest.mark.asyncio
async def test_export_writes_daily_note_markdown(db, tmp_path):
    db.add(
        DailyNote(
            date="2026-06-23",
            markdown="# Daily Note — 2026-06-23\n\n- shipped the thing",
            html="<h1>Daily Note</h1>",
        )
    )
    await db.commit()

    # Must not raise (the bug raised AttributeError: 'DailyNote' has no 'content').
    await export_all(db, tmp_path)

    note_file = tmp_path / "daily" / "2026-06-23.md"
    assert note_file.exists(), "daily note was not exported"
    assert "shipped the thing" in note_file.read_text(encoding="utf-8")
