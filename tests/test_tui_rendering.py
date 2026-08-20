"""Regression tests for the TUI rendering bugs found in real terminal use:

1. Streamed deltas must be committed to the transcript as whole blocks —
   RichLog appends one line per write, so per-delta writes rendered the
   final answer one word per line.
2. The approval modal's feedback Input must not hold focus by default —
   when it did, pressing `y`/`a` typed into the field instead of answering,
   and Enter submitted that text as a rejection (the model then saw
   rejection messages like "a").
"""

from __future__ import annotations

import pytest
from textual.widgets import RichLog

from agents_codex.tui.app import ApprovalModal, CodexTui

from .mock_responses import MockResponsesServer, Stage
from .test_e2e_mock import SCENARIO, make_config

FINAL = "Created greeting.py with a hello() function."


@pytest.fixture
async def mock_server():
    server = MockResponsesServer(stages=[Stage(**vars(s)) for s in SCENARIO])
    base_url = await server.start()
    yield server, base_url
    await server.stop()


def transcript_lines(app: CodexTui) -> list[str]:
    log = app.query_one("#transcript", RichLog)
    return [line.text for line in log.lines]


async def test_answer_streams_as_one_block(tmp_path, monkeypatch, mock_server):
    _, base_url = mock_server
    monkeypatch.setenv("AGENTS_CODEX_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OPENAI_API_KEY", "mock-key")
    app = CodexTui(make_config(tmp_path, base_url))
    async with app.run_test(size=(100, 40)) as pilot:
        app.query_one("#composer").value = "Add a hello function in greeting.py"
        await pilot.press("enter")
        for _ in range(120):
            await pilot.pause(0.25)
            if not app._busy and (tmp_path / "greeting.py").exists():
                break
        await pilot.pause(0.5)
        lines = transcript_lines(app)
        # The whole final sentence must land on a single transcript line
        # (pre-fix, RichLog got one write per delta: one word per line).
        assert any(FINAL in line for line in lines), lines[-15:]
        # And the reasoning summaries commit as blocks, not word confetti.
        assert any("Inspecting the workspace" in line for line in lines)
        single_word_lines = [l for l in lines if l.strip() in ("Created", "greeting.py", "with")]
        assert not single_word_lines
        # Live cell is empty once the turn is done.
        assert app._answer_buf == "" and app._reasoning_buf == ""
    await app.session.close()


async def test_approval_modal_keys_answer_without_typing(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTS_CODEX_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OPENAI_API_KEY", "mock-key")
    app = CodexTui(make_config(tmp_path, "http://127.0.0.1:9/v1"))
    async with app.run_test(size=(100, 40)) as pilot:
        results = []
        app.push_screen(ApprovalModal("Approve shell?", "$ rm -rf build"), results.append)
        await pilot.pause()
        # `y` must approve immediately — not get typed into the feedback box.
        await pilot.press("y")
        await pilot.pause()
        assert results and results[0].approved is True

        app.push_screen(ApprovalModal("Approve shell?", "$ rm -rf build"), results.append)
        await pilot.pause()
        # `f` reveals the feedback input; typed text (including y/a/p/n
        # letters) must land in the field and come back as feedback.
        await pilot.press("f")
        await pilot.pause()
        for ch in "stay safe":
            await pilot.press(ch if ch != " " else "space")
        await pilot.press("enter")
        await pilot.pause()
        assert len(results) == 2
        assert results[1].approved is False
        assert results[1].feedback == "stay safe"
    await app.session.close()
