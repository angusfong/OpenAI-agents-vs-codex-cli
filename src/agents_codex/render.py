"""Rich-based rendering of stream events for the non-TUI frontends
(exec mode human output). Mirrors Codex's transcript styling: dim italic
reasoning summaries, bold-cyan tool banners, plain final answers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from rich.console import Console

from .approval import ApprovalAnswer


@dataclass
class ConsoleRenderer:
    """Streams events to a Rich console; approvals resolved via a callback
    (exec mode auto-decides, interactive callers prompt)."""

    console: Console = field(default_factory=Console)
    show_reasoning: bool = True
    auto_approve: bool | None = None  # None = prompt on stdin
    _in_reasoning: bool = False
    _in_text: bool = False

    def on_event(self, event) -> None:
        if event.type == "raw_response_event":
            data = event.data
            if data.type == "response.reasoning_summary_text.delta":
                if self.show_reasoning:
                    if not self._in_reasoning:
                        self.console.print("\n[dim]thinking[/dim]", highlight=False)
                        self._in_reasoning = True
                    self.console.print(
                        f"[dim italic]{data.delta}[/dim italic]", end="", highlight=False
                    )
            elif data.type == "response.output_text.delta":
                if self._in_reasoning or not self._in_text:
                    self.console.print("\n[bold]codex[/bold]", highlight=False)
                    self._in_reasoning = False
                    self._in_text = True
                self.console.print(data.delta, end="", highlight=False, markup=False)
            elif data.type == "response.completed":
                self._in_text = False
        elif event.type == "run_item_stream_event":
            item = event.item
            if event.name == "tool_called":
                self._in_reasoning = False
                self.console.print(
                    f"\n[bold cyan]exec[/bold cyan] {describe_item(item)}",
                    highlight=False,
                )
            elif event.name == "tool_output":
                output = str(getattr(item, "output", "")) or ""
                preview = output.strip().splitlines()
                shown = "\n".join(preview[:8])
                if len(preview) > 8:
                    shown += f"\n… +{len(preview) - 8} lines"
                if shown:
                    self.console.print(f"[dim]{shown}[/dim]", highlight=False, markup=False)

    async def ask_approval(self, kind: str, title: str, detail: str) -> ApprovalAnswer:
        self.console.print(f"\n[bold yellow]approval[/bold yellow] {title}")
        self.console.print(detail, markup=False)
        if self.auto_approve is not None:
            verdict = "approved" if self.auto_approve else "rejected"
            self.console.print(f"[dim]auto-{verdict} (non-interactive)[/dim]")
            return ApprovalAnswer(approved=self.auto_approve)
        while True:
            reply = input("Approve? [y]es / [a]lways / [n]o / [f]eedback: ").strip().lower()
            if reply in ("y", "yes"):
                return ApprovalAnswer(approved=True)
            if reply in ("a", "always"):
                return ApprovalAnswer(approved=True, always_for_session=True)
            if reply in ("n", "no"):
                return ApprovalAnswer(approved=False)
            if reply in ("f", "feedback"):
                feedback = input("What should the agent do instead? ").strip()
                return ApprovalAnswer(approved=False, feedback=feedback)


def describe_item(item) -> str:
    raw = getattr(item, "raw_item", None)
    action = getattr(raw, "action", None)
    commands = getattr(action, "commands", None) if action is not None else None
    if commands:
        return "; ".join(commands)
    operation = getattr(raw, "operation", None)
    if operation is not None:
        op_type = getattr(operation, "type", "?")
        path = getattr(operation, "path", "?")
        return f"apply_patch {op_type} {path}"
    name = getattr(raw, "name", None) or type(item).__name__
    args = getattr(raw, "arguments", None)
    if isinstance(args, str) and len(args) < 120:
        return f"{name}({args})"
    return str(name)


def event_to_json(event) -> dict | None:
    """Map a stream event to a JSONL record for `exec --json`, in the spirit
    of codex exec's thread/turn/item event stream."""
    if event.type == "raw_response_event":
        data = event.data
        if data.type == "response.reasoning_summary_text.delta":
            return {"type": "reasoning_summary.delta", "delta": data.delta}
        if data.type == "response.output_text.delta":
            return {"type": "agent_message.delta", "delta": data.delta}
        if data.type == "response.completed":
            usage = getattr(data.response, "usage", None)
            if usage:
                return {
                    "type": "sampling.completed",
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                }
        return None
    if event.type == "run_item_stream_event":
        if event.name == "tool_called":
            return {"type": "item.tool_call", "detail": describe_item(event.item)}
        if event.name == "tool_output":
            output = str(getattr(event.item, "output", ""))
            return {"type": "item.tool_output", "output": output[:2000]}
        if event.name == "message_output_created":
            return None  # deltas already streamed
    return None


def jsonl(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False)
