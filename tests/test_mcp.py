"""MCP client e2e: the agent calls a tool served by a real stdio MCP server."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from agents_codex.config import McpServerConfig
from agents_codex.core import CodexSession

from .mock_responses import MockResponsesServer, Stage
from .test_e2e_mock import CaptureRenderer, make_config

SERVER_SCRIPT = str(Path(__file__).parent / "tiny_mcp_server.py")


async def test_mcp_stdio_tool_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTS_CODEX_HOME", str(tmp_path / "home"))
    stages = [
        Stage(
            reasoning="Consulting the docs MCP server.",
            kind="function",
            payload=("lookup_docs", json.dumps({"topic": "frobnication"})),
            done_marker="function_call_output",
        ),
        Stage(
            reasoning="Relaying what the docs said.",
            kind="message",
            payload="The docs say: use the frobnicator.",
        ),
    ]
    server = MockResponsesServer(stages=stages)
    base_url = await server.start()
    try:
        cfg = make_config(tmp_path, base_url)
        cfg = type(cfg)(
            **{
                **{f: getattr(cfg, f) for f in cfg.__dataclass_fields__},
                "mcp_servers": (
                    McpServerConfig(
                        name="tiny",
                        command=sys.executable,
                        args=[SERVER_SCRIPT],
                    ),
                ),
            }
        )
        session = CodexSession(cfg=cfg)
        renderer = CaptureRenderer()
        final = await session.run_turn("How do I frobnicate?", renderer)
        await session.close()

        assert final == "The docs say: use the frobnicator."
        # The MCP tool's real output went back to the model.
        followup_input = json.dumps(server.requests[-1]["input"])
        assert "use the frobnicator" in followup_input
        # And the SDK advertised the MCP tool to the model.
        tools = json.dumps(server.requests[0].get("tools", []))
        assert "lookup_docs" in tools
    finally:
        await server.stop()
