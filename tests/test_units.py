"""Unit tests: approval classifier, patch jailing, AGENTS.md discovery,
config layering, session resume."""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.editor import ApplyPatchOperation

from agents_codex.agents_md import load_agents_md
from agents_codex.approval import ApprovalPolicy, Decision, split_segments
from agents_codex.config import Config, load_config
from agents_codex.core import CodexSession
from agents_codex.tools.patch import WorkspaceEditor

from .mock_responses import MockResponsesServer, Stage
from .test_e2e_mock import CaptureRenderer, SCENARIO, make_config


def policy(**cfg_kwargs) -> ApprovalPolicy:
    return ApprovalPolicy(cfg=Config(**cfg_kwargs))


class TestApprovalClassifier:
    def test_safe_reads_allowed(self):
        p = policy(approval_policy="untrusted")
        assert p.classify_shell("ls -la && git status") is Decision.ALLOW
        assert p.classify_shell("grep -r foo | head -5") is Decision.ALLOW

    def test_untrusted_prompts_for_writes(self):
        p = policy(approval_policy="untrusted")
        assert p.classify_shell("pip install requests") is Decision.PROMPT

    def test_dangerous_always_prompts_even_sandboxed(self):
        p = policy(approval_policy="on-request")
        assert p.classify_shell("rm -rf build") is Decision.PROMPT
        assert p.classify_shell("git push --force origin main") is Decision.PROMPT

    def test_never_mode_forbids_dangerous(self):
        p = policy(approval_policy="never")
        assert p.classify_shell("rm -rf /") is Decision.FORBIDDEN
        assert p.classify_shell("make build") is Decision.ALLOW

    def test_redirection_disqualifies_prefix_match(self):
        p = policy(approval_policy="untrusted")
        assert p.classify_shell("echo hi > /etc/passwd") is Decision.PROMPT

    def test_session_prefix_rule(self):
        p = policy(approval_policy="untrusted")
        assert p.classify_shell("git push origin main") is Decision.PROMPT
        p.add_prefix_rule("git push origin main")
        assert p.classify_shell("git push origin feature") is Decision.ALLOW

    def test_segment_splitting(self):
        assert split_segments("ls && cat foo | wc -l") == [["ls"], ["cat", "foo"], ["wc", "-l"]]
        assert split_segments("echo $(whoami)") is None


class TestPatchJail:
    def test_escape_is_blocked(self, tmp_path):
        editor = WorkspaceEditor(cfg=Config(cwd=tmp_path))
        op = ApplyPatchOperation(type="create_file", path="../evil.txt", diff="+x\n")
        with pytest.raises(Exception):
            editor.create_file(op)
        assert not (tmp_path.parent / "evil.txt").exists()

    def test_read_only_blocks_writes(self, tmp_path):
        editor = WorkspaceEditor(cfg=Config(cwd=tmp_path, sandbox_mode="read-only"))
        op = ApplyPatchOperation(type="create_file", path="a.txt", diff="+x\n")
        with pytest.raises(Exception):
            editor.create_file(op)

    def test_update_and_move(self, tmp_path):
        (tmp_path / "a.txt").write_text("one\ntwo\n")
        editor = WorkspaceEditor(cfg=Config(cwd=tmp_path))
        op = ApplyPatchOperation(
            type="update_file", path="a.txt", diff=" one\n-two\n+2\n", move_to="b.txt"
        )
        result = editor.update_file(op)
        assert result.status == "completed"
        assert not (tmp_path / "a.txt").exists()
        assert (tmp_path / "b.txt").read_text() == "one\n2\n"


class TestAgentsMd:
    def test_discovery_order(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENTS_CODEX_HOME", str(tmp_path / "home"))
        (tmp_path / ".git").mkdir()
        (tmp_path / "AGENTS.md").write_text("root rules")
        sub = tmp_path / "pkg"
        sub.mkdir()
        (sub / "AGENTS.md").write_text("pkg rules")
        merged = load_agents_md(sub)
        assert merged.index("root rules") < merged.index("pkg rules")


class TestConfig:
    def test_layering_and_profiles(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("AGENTS_CODEX_HOME", str(home))
        (home / "config.toml").write_text(
            'model = "m-user"\napproval_policy = "untrusted"\n'
            '[profiles.fast]\nmodel = "m-fast"\n'
            '[mcp_servers.docs]\ncommand = "docs-mcp"\n'
        )
        project = tmp_path / "proj"
        project.mkdir()
        (project / ".agents-codex.toml").write_text('sandbox_mode = "read-only"\n')
        cfg = load_config(cwd=project, profile="fast", overrides={"reasoning_effort": "high"})
        assert cfg.model == "m-fast"
        assert cfg.approval_policy == "untrusted"
        assert cfg.sandbox_mode == "read-only"
        assert cfg.reasoning_effort == "high"
        assert cfg.mcp_servers[0].name == "docs"


async def test_session_resume_replays_history(tmp_path, monkeypatch):
    """A resumed thread sends prior history, so the mock jumps straight to
    the final stage — proving persistence + resume works end-to-end."""
    monkeypatch.setenv("AGENTS_CODEX_HOME", str(tmp_path / "home"))
    server = MockResponsesServer(stages=[Stage(**vars(s)) for s in SCENARIO])
    base_url = await server.start()
    try:
        cfg = make_config(tmp_path, base_url)
        first = CodexSession(cfg=cfg)
        await first.run_turn("Add a hello function in greeting.py", CaptureRenderer())
        thread_id = first.thread_id
        await first.close()
        requests_before = len(server.requests)

        resumed = CodexSession(cfg=cfg, thread_id=thread_id)
        renderer = CaptureRenderer()
        final = await resumed.run_turn("thanks!", renderer)
        await resumed.close()

        # One sampling request sufficed: history satisfied every done_marker.
        assert len(server.requests) == requests_before + 1
        assert final == "Created greeting.py with a hello() function."
        last_input = str(server.requests[-1])
        assert "shell_call" in last_input  # replayed history items
    finally:
        await server.stop()
