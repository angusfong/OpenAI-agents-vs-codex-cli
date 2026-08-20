"""Non-interactive `agents-codex exec` mode, mirroring `codex exec`:
approval policy defaults to `never` (the sandbox is the safety layer),
output is either human-readable or a --json JSONL event stream.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, replace

from rich.console import Console

from .approval import ApprovalAnswer
from .config import Config
from .core import CodexSession
from .render import ConsoleRenderer, event_to_json, jsonl


@dataclass
class JsonRenderer:
    def on_event(self, event) -> None:
        record = event_to_json(event)
        if record:
            print(jsonl(record), flush=True)

    async def ask_approval(self, kind, title, detail) -> ApprovalAnswer:
        print(jsonl({"type": "approval.auto_rejected", "tool": kind, "detail": detail}), flush=True)
        return ApprovalAnswer(
            approved=False,
            feedback="Non-interactive mode: approval unavailable; stay within the sandbox.",
        )


async def run_exec(
    cfg: Config,
    prompt: str,
    *,
    as_json: bool = False,
    thread_id: str = "",
) -> int:
    if cfg.approval_policy != "never":
        cfg = replace(cfg, approval_policy="never")
    session = CodexSession(cfg=cfg, thread_id=thread_id)
    if as_json:
        renderer: object = JsonRenderer()
    else:
        renderer = ConsoleRenderer(console=Console(stderr=False), auto_approve=False)
    try:
        final = await session.run_turn(prompt, renderer)
    finally:
        await session.close()
    if as_json:
        print(jsonl({"type": "turn.completed", "final_output": final, "thread_id": session.thread_id}))
    else:
        if final is not None:
            print(f"\n{final}")
        print(f"\n[thread: {session.thread_id}]", file=sys.stderr)
    return 0 if final is not None else 1
