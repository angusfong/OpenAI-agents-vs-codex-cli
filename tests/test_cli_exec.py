"""CLI-level tests: `agents-codex exec` as a real subprocess against the
mock server, and the Textual TUI driven headlessly with Pilot.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

from agents_codex.config import Config
from agents_codex.tui.app import CodexTui

from .mock_responses import MockResponsesServer, Stage
from .test_e2e_mock import SCENARIO, make_config


@pytest.fixture
async def mock_server():
    server = MockResponsesServer(stages=[Stage(**vars(s)) for s in SCENARIO])
    base_url = await server.start()
    yield server, base_url
    await server.stop()


async def test_exec_json_subprocess(tmp_path, mock_server):
    _, base_url = mock_server
    env = dict(
        os.environ,
        OPENAI_API_KEY="mock-key",
        AGENTS_CODEX_HOME=str(tmp_path / "home"),
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "agents_codex.cli",
        "--base-url", base_url, "-C", str(tmp_path), "-m", "mock-model",
        "exec", "--json", "Add a hello function in greeting.py",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=90)
    assert proc.returncode == 0, stderr.decode()[-2000:]
    records = [json.loads(line) for line in stdout.decode().splitlines() if line.strip()]
    kinds = {r["type"] for r in records}
    assert "reasoning_summary.delta" in kinds
    assert "item.tool_call" in kinds
    assert "sampling.completed" in kinds
    final = [r for r in records if r["type"] == "turn.completed"]
    assert final and final[0]["final_output"] == "Created greeting.py with a hello() function."
    # exec mode really edited the file
    assert (tmp_path / "greeting.py").exists()


async def test_tui_turn_with_pilot(tmp_path, monkeypatch, mock_server):
    _, base_url = mock_server
    monkeypatch.setenv("AGENTS_CODEX_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OPENAI_API_KEY", "mock-key")
    cfg: Config = make_config(tmp_path, base_url)
    app = CodexTui(cfg)
    async with app.run_test(size=(100, 40)) as pilot:
        composer = app.query_one("#composer")
        composer.value = "Add a hello function in greeting.py"
        await pilot.press("enter")
        for _ in range(120):  # wait for the worker to finish
            await pilot.pause(0.25)
            if not app._busy and (tmp_path / "greeting.py").exists():
                break
        assert (tmp_path / "greeting.py").exists()
        assert app.tokens_in > 0 and app.tokens_out > 0
        # slash command sanity
        composer.value = "/status"
        await pilot.press("enter")
        await pilot.pause(0.1)
    await app.session.close()
