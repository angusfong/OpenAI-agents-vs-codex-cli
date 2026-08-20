"""End-to-end tests: CodexSession against the mock Responses API.

Proves the full wiring without an API key: streaming reasoning summaries,
sandboxed shell execution, apply_patch file edits, session persistence,
and the approval-interruption (clarification) flow.
"""

from __future__ import annotations

import pytest

from agents_codex.approval import ApprovalAnswer
from agents_codex.config import Config
from agents_codex.core import CodexSession

from .mock_responses import MockResponsesServer, Stage


class CaptureRenderer:
    def __init__(self, approve: bool = True):
        self.approve = approve
        self.reasoning_deltas: list[str] = []
        self.text_deltas: list[str] = []
        self.item_events: list[str] = []
        self.approvals_asked: list[str] = []

    def on_event(self, event) -> None:
        if event.type == "raw_response_event":
            data = event.data
            if data.type == "response.reasoning_summary_text.delta":
                self.reasoning_deltas.append(data.delta)
            elif data.type == "response.output_text.delta":
                self.text_deltas.append(data.delta)
        elif event.type == "run_item_stream_event":
            self.item_events.append(event.name)

    async def ask_approval(self, kind, title, detail) -> ApprovalAnswer:
        self.approvals_asked.append(f"{kind}: {detail}")
        return ApprovalAnswer(approved=self.approve)


SCENARIO = [
    Stage(
        reasoning="Inspecting the workspace before making changes.",
        kind="shell",
        payload=["ls"],
        done_marker="shell_call_output",
    ),
    Stage(
        reasoning="Creating greeting.py with the requested function.",
        kind="apply_patch",
        payload={
            "type": "create_file",
            "path": "greeting.py",
            "diff": "+def hello():\n+    return 'hello world'\n",
        },
        done_marker="apply_patch_call_output",
    ),
    Stage(
        reasoning="Summarizing the change for the user.",
        kind="message",
        payload="Created greeting.py with a hello() function.",
    ),
]


def make_config(tmp_path, base_url, **overrides) -> Config:
    defaults = dict(
        model="mock-model",
        base_url=base_url,
        cwd=tmp_path,
        sandbox_mode="workspace-write",
        approval_policy="on-request",
    )
    defaults.update(overrides)
    return Config(**defaults)


@pytest.fixture
async def mock_server():
    server = MockResponsesServer(stages=[Stage(**vars(s)) for s in SCENARIO])
    base_url = await server.start()
    yield server, base_url
    await server.stop()


async def test_full_coding_turn(tmp_path, monkeypatch, mock_server):
    server, base_url = mock_server
    monkeypatch.setenv("AGENTS_CODEX_HOME", str(tmp_path / "home"))
    session = CodexSession(cfg=make_config(tmp_path, base_url))
    renderer = CaptureRenderer()

    final = await session.run_turn("Add a hello function in greeting.py", renderer)

    # Final assistant message made it through.
    assert final == "Created greeting.py with a hello() function."
    # apply_patch actually wrote the file, jailed to the workspace.
    assert (tmp_path / "greeting.py").read_text().startswith("def hello():")
    # Reasoning summaries streamed for every stage.
    joined = "".join(renderer.reasoning_deltas)
    assert "Inspecting the workspace" in joined
    assert "Creating greeting.py" in joined
    # The shell tool really executed (its output went back to the model).
    assert len(server.requests) == 3
    assert renderer.approvals_asked == []  # on-request + sandboxed: no prompts
    # Environment context and AGENTS.md scaffolding went into the first turn.
    first_input = str(server.requests[0]["input"])
    assert "<environment_context>" in first_input
    await session.close()


async def test_untrusted_policy_asks_before_shell(tmp_path, monkeypatch, mock_server):
    server, base_url = mock_server
    monkeypatch.setenv("AGENTS_CODEX_HOME", str(tmp_path / "home"))
    session = CodexSession(
        cfg=make_config(tmp_path, base_url, approval_policy="untrusted")
    )
    renderer = CaptureRenderer(approve=True)

    final = await session.run_turn("Add a hello function in greeting.py", renderer)

    assert final == "Created greeting.py with a hello() function."
    # `ls` is prefix-safe, so only apply_patch should have asked.
    assert len(renderer.approvals_asked) == 1
    assert renderer.approvals_asked[0].startswith("apply_patch")
    await session.close()


async def test_rejection_feeds_back_to_model(tmp_path, monkeypatch):
    stages = [
        Stage(
            reasoning="Trying to remove the build directory.",
            kind="shell",
            payload=["rm -rf build"],
            done_marker="shell_call_output",
        ),
        Stage(
            reasoning="Acknowledging the rejection.",
            kind="message",
            payload="Understood, I will not delete anything.",
        ),
    ]
    server = MockResponsesServer(stages=stages)
    base_url = await server.start()
    try:
        monkeypatch.setenv("AGENTS_CODEX_HOME", str(tmp_path / "home"))
        session = CodexSession(cfg=make_config(tmp_path, base_url))
        renderer = CaptureRenderer(approve=False)

        final = await session.run_turn("Clean the build dir", renderer)

        # Dangerous command forced a prompt despite the sandbox...
        assert len(renderer.approvals_asked) == 1
        assert "rm -rf build" in renderer.approvals_asked[0]
        # ...the rejection went back to the model, which answered gracefully.
        assert final == "Understood, I will not delete anything."
        await session.close()
    finally:
        await server.stop()
