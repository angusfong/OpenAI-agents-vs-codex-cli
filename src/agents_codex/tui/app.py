"""Full-screen Textual TUI, mirroring the Codex CLI experience: streaming
transcript with dim reasoning summaries, tool banners, an approval modal
with graded options, slash commands, and a token-usage status bar.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, RichLog, Static

from ..approval import ApprovalAnswer
from ..config import Config
from ..core import CodexSession
from ..render import describe_item
from ..sessions import list_threads

SLASH_HELP = """\
/help            show this help
/new             start a new thread
/model <slug>    switch model (new thread)
/approvals <m>   untrusted | on-request | never
/sandbox <m>     read-only | workspace-write | danger-full-access
/plan            show the current plan
/status          show config, thread, token usage
/sessions        list stored threads
/quit            exit"""


class ApprovalModal(ModalScreen[ApprovalAnswer]):
    """Codex-style approval overlay: yes / always / prefix rule / no / feedback."""

    BINDINGS = [
        Binding("y", "answer('yes')", "yes"),
        Binding("a", "answer('always')", "always for session"),
        Binding("p", "answer('prefix')", "yes + don't ask for this prefix"),
        Binding("n", "answer('no')", "no"),
        Binding("f", "answer('feedback')", "no, with guidance"),
        Binding("escape", "answer('no')", "reject"),
    ]

    def __init__(self, title: str, detail: str):
        super().__init__()
        self._title = title
        self._detail = detail

    def compose(self) -> ComposeResult:
        box = Vertical(id="approval-box")
        with box:
            yield Static(Text(self._title, style="bold yellow"))
            yield Static(Text(self._detail))
            yield Static(
                Text(
                    "[y] yes   [a] yes, for this session   [p] yes + prefix rule\n"
                    "[n] no    [f] no, and tell the agent what to do differently",
                    style="dim",
                )
            )
            yield Input(placeholder="feedback (press f first)…", id="feedback-input")

    def action_answer(self, choice: str) -> None:
        if choice == "feedback":
            self.query_one("#feedback-input", Input).focus()
            return
        self.dismiss(
            {
                "yes": ApprovalAnswer(approved=True),
                "always": ApprovalAnswer(approved=True, always_for_session=True),
                "prefix": ApprovalAnswer(approved=True, add_prefix_rule=True),
                "no": ApprovalAnswer(approved=False),
            }[choice]
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(ApprovalAnswer(approved=False, feedback=event.value or None))


class TuiRenderer:
    """Bridges CodexSession events into the running app."""

    def __init__(self, app: "CodexTui"):
        self.app = app

    def on_event(self, event) -> None:
        self.app.handle_stream_event(event)

    async def ask_approval(self, kind, title, detail) -> ApprovalAnswer:
        return await self.app.push_screen_wait(ApprovalModal(title, detail))


class CodexTui(App):
    CSS = """
    #transcript { height: 1fr; padding: 0 1; }
    #status { height: 1; background: $surface; color: $text-muted; padding: 0 1; }
    #composer { dock: bottom; }
    #approval-box { width: 80%; max-height: 80%; border: round yellow;
                    background: $surface; padding: 1 2; }
    ApprovalModal { align: center middle; }
    """
    BINDINGS = [Binding("ctrl+c", "interrupt_or_quit", "interrupt/quit", priority=True)]

    def __init__(self, cfg: Config, thread_id: str = "", initial_prompt: str | None = None):
        super().__init__()
        self.cfg = cfg
        self.session = CodexSession(cfg=cfg, thread_id=thread_id)
        self.initial_prompt = initial_prompt
        self._busy = False
        self._reasoning_open = False
        self._answer_open = False
        self.tokens_in = 0
        self.tokens_out = 0

    # -- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield RichLog(id="transcript", wrap=True, markup=False, highlight=False)
        yield Static(id="status")
        yield Input(placeholder="Ask agents-codex… (/help for commands)", id="composer")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#composer", Input).focus()
        self._refresh_status()
        log = self.query_one("#transcript", RichLog)
        log.write(Text(f"agents-codex — {self.cfg.model} — thread {self.session.thread_id}", style="bold"))
        log.write(Text(f"sandbox: {self.cfg.sandbox_mode}   approvals: {self.cfg.approval_policy}", style="dim"))
        if self.initial_prompt:
            self.run_worker(self._run_turn(self.initial_prompt))

    def _refresh_status(self, note: str = "") -> None:
        status = self.query_one("#status", Static)
        text = (
            f"{self.cfg.model} | {self.cfg.sandbox_mode} | {self.cfg.approval_policy} "
            f"| tokens {self.tokens_in}↑ {self.tokens_out}↓"
        )
        if note:
            text += f" | {note}"
        status.update(Text(text))

    # -- events from the engine -------------------------------------------

    def handle_stream_event(self, event) -> None:
        log = self.query_one("#transcript", RichLog)
        if event.type == "raw_response_event":
            data = event.data
            if data.type == "response.reasoning_summary_text.delta":
                if not self._reasoning_open:
                    log.write(Text("thinking", style="bold dim"))
                    self._reasoning_open = True
                log.write(Text(data.delta, style="dim italic"), scroll_end=True)
                self._refresh_status("thinking…")
            elif data.type == "response.output_text.delta":
                if not self._answer_open:
                    log.write(Text("\ncodex", style="bold magenta"))
                    self._answer_open = True
                    self._reasoning_open = False
                log.write(Text(data.delta), scroll_end=True)
            elif data.type == "response.completed":
                usage = getattr(data.response, "usage", None)
                if usage:
                    self.tokens_in += usage.input_tokens
                    self.tokens_out += usage.output_tokens
                self._answer_open = False
                self._reasoning_open = False
                self._refresh_status()
        elif event.type == "run_item_stream_event":
            if event.name == "tool_called":
                self._reasoning_open = False
                log.write(Text(f"\nexec {describe_item(event.item)}", style="bold cyan"))
            elif event.name == "tool_output":
                output = str(getattr(event.item, "output", "")).strip()
                lines = output.splitlines()
                shown = "\n".join(lines[:6]) + (f"\n… +{len(lines) - 6} lines" if len(lines) > 6 else "")
                if shown:
                    log.write(Text(shown, style="dim"))

    # -- input -------------------------------------------------------------

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "composer":
            return
        message = event.value.strip()
        event.input.value = ""
        if not message:
            return
        if message.startswith("/"):
            await self._slash(message)
            return
        if self._busy:
            self.notify("A turn is already running", severity="warning")
            return
        self.run_worker(self._run_turn(message))

    async def _run_turn(self, message: str) -> None:
        log = self.query_one("#transcript", RichLog)
        log.write(Text(f"\nyou: {message}", style="bold green"))
        self._busy = True
        self._refresh_status("working…")
        try:
            await self.session.run_turn(message, TuiRenderer(self))
        except Exception as exc:
            log.write(Text(f"error: {exc}", style="bold red"))
        finally:
            self._busy = False
            self._reasoning_open = False
            self._answer_open = False
            self._refresh_status()

    # -- slash commands ----------------------------------------------------

    async def _slash(self, message: str) -> None:
        log = self.query_one("#transcript", RichLog)
        parts = message.split(maxsplit=1)
        cmd, arg = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")
        if cmd == "/help":
            log.write(Text(SLASH_HELP, style="dim"))
        elif cmd == "/quit":
            self.exit()
        elif cmd == "/new":
            await self.session.close()
            self.session = CodexSession(cfg=self.cfg)
            log.write(Text(f"new thread {self.session.thread_id}", style="bold"))
        elif cmd == "/model" and arg:
            self.cfg = replace(self.cfg, model=arg)
            await self.session.close()
            self.session = CodexSession(cfg=self.cfg)
            self._refresh_status()
            log.write(Text(f"model set to {arg} (new thread)", style="bold"))
        elif cmd == "/approvals" and arg in ("untrusted", "on-request", "never"):
            self.cfg = replace(self.cfg, approval_policy=arg)
            self.session.cfg = self.cfg
            self.session.policy.cfg = self.cfg
            self._refresh_status()
            log.write(Text(f"approval policy: {arg}", style="bold"))
        elif cmd == "/sandbox" and arg in ("read-only", "workspace-write", "danger-full-access"):
            self.cfg = replace(self.cfg, sandbox_mode=arg)
            self.session.cfg = self.cfg
            self.session.policy.cfg = self.cfg
            self.session.editor.cfg = self.cfg
            self._refresh_status()
            log.write(Text(f"sandbox mode: {arg}", style="bold"))
        elif cmd == "/plan":
            plan = self.session.context.plan
            if not plan.steps:
                log.write(Text("no plan yet", style="dim"))
            for step in plan.steps:
                mark = {"pending": "☐", "in_progress": "▶", "completed": "☑"}[step.status]
                log.write(Text(f" {mark} {step.step}"))
        elif cmd == "/status":
            log.write(Text(
                f"model={self.cfg.model} sandbox={self.cfg.sandbox_mode} "
                f"approvals={self.cfg.approval_policy} thread={self.session.thread_id} "
                f"tokens={self.tokens_in}↑/{self.tokens_out}↓ cwd={self.cfg.cwd}",
                style="dim",
            ))
        elif cmd == "/sessions":
            for info in list_threads(limit=10):
                log.write(Text(f"{info.thread_id}  items={info.items}  {info.preview}", style="dim"))
        else:
            log.write(Text(f"unknown command: {message}", style="red"))

    def action_interrupt_or_quit(self) -> None:
        if self._busy:
            self.workers.cancel_all()
            self._busy = False
            self.query_one("#transcript", RichLog).write(Text("turn interrupted", style="bold red"))
            self._refresh_status()
        else:
            self.exit()


def run_tui(cfg: Config, thread_id: str = "", initial_prompt: str | None = None) -> int:
    app = CodexTui(cfg, thread_id=thread_id, initial_prompt=initial_prompt)
    app.run()
    try:
        asyncio.run(app.session.close())
    except RuntimeError:
        pass
    return 0
