"""Session engine: builds the SDK agent and drives turns for every frontend
(TUI, exec mode, tests). Mirrors codex-core's role in the Rust workspace.
"""

from __future__ import annotations

import importlib.resources
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol

from agents import (
    Agent,
    ApplyPatchTool,
    ModelSettings,
    Runner,
    RunResultStreaming,
    SQLiteSession,
    ShellTool,
    set_tracing_disabled,
)
from agents.mcp import MCPServerStdio, MCPServerStreamableHttp
from openai import AsyncOpenAI
from openai.types.shared import Reasoning

from .agents_md import load_agents_md
from .approval import (
    ApprovalAnswer,
    ApprovalPolicy,
    make_patch_needs_approval,
    make_shell_needs_approval,
)
from .config import Config
from .environment_context import build_environment_context
from .sessions import default_session_db, new_thread_id
from .tools.patch import WorkspaceEditor
from .tools.plan import PlanState, update_plan
from .tools.shell import ShellExecutor


class Renderer(Protocol):
    """Frontend interface; every hook is optional-effect and non-blocking
    except ask_approval, which resolves a pending approval interruption."""

    def on_event(self, event: Any) -> None: ...
    async def ask_approval(self, kind: str, title: str, detail: str) -> ApprovalAnswer: ...


@dataclass
class TurnContext:
    """Mutable per-session context passed as Runner context (ctx.context)."""

    plan: PlanState = field(default_factory=PlanState)


@dataclass
class CodexSession:
    cfg: Config
    thread_id: str = ""
    policy: ApprovalPolicy = None  # type: ignore[assignment]
    editor: WorkspaceEditor = None  # type: ignore[assignment]
    context: TurnContext = field(default_factory=TurnContext)
    _agent: Agent | None = None
    _memory: SQLiteSession | None = None
    _mcp_servers: list = field(default_factory=list)
    _sent_initial_context: bool = False

    def __post_init__(self) -> None:
        set_tracing_disabled(True)  # local CLI: never upload traces
        if not self.thread_id:
            self.thread_id = new_thread_id()
        if self.policy is None:
            self.policy = ApprovalPolicy(cfg=self.cfg)
        if self.editor is None:
            self.editor = WorkspaceEditor(cfg=self.cfg)

    # -- assembly ----------------------------------------------------------

    def _instructions(self) -> str:
        base = (
            importlib.resources.files("agents_codex.prompts")
            .joinpath("system.md")
            .read_text(encoding="utf-8")
        )
        return base

    def _initial_context_message(self) -> str:
        parts = [build_environment_context(self.cfg)]
        agents_md = load_agents_md(self.cfg.cwd)
        if agents_md:
            parts.append(f"<user_instructions>\n{agents_md}\n</user_instructions>")
        return "\n\n".join(parts)

    def _model(self):
        if self.cfg.base_url:
            client = AsyncOpenAI(
                base_url=self.cfg.base_url, api_key=self.cfg.api_key or "unused"
            )
            if self.cfg.wire_api == "chat":
                from agents import OpenAIChatCompletionsModel

                return OpenAIChatCompletionsModel(
                    model=self.cfg.model, openai_client=client
                )
            from agents import OpenAIResponsesModel

            return OpenAIResponsesModel(model=self.cfg.model, openai_client=client)
        return self.cfg.model

    async def _build_mcp_servers(self) -> list:
        servers = []
        for spec in self.cfg.mcp_servers:
            if spec.url:
                server = MCPServerStreamableHttp(
                    params={"url": spec.url},
                    cache_tools_list=True,
                    require_approval=spec.require_approval,
                )
            else:
                server = MCPServerStdio(
                    params={
                        "command": spec.command,
                        "args": spec.args,
                        "env": spec.env or None,
                    },
                    cache_tools_list=True,
                    require_approval=spec.require_approval,
                )
            await server.connect()
            servers.append(server)
        return servers

    async def agent(self) -> Agent:
        if self._agent is None:
            self._mcp_servers = await self._build_mcp_servers()
            reasoning = None
            if self.cfg.reasoning_effort != "none":
                reasoning = Reasoning(
                    effort=self.cfg.reasoning_effort,
                    summary=self.cfg.reasoning_summary,
                )
            self._agent = Agent(
                name="agents-codex",
                instructions=self._instructions(),
                model=self._model(),
                model_settings=ModelSettings(
                    reasoning=reasoning,
                    parallel_tool_calls=True,
                    truncation="auto",
                ),
                tools=[
                    ShellTool(
                        executor=ShellExecutor(self.cfg),
                        needs_approval=make_shell_needs_approval(self.policy),
                    ),
                    ApplyPatchTool(
                        editor=self.editor,
                        needs_approval=make_patch_needs_approval(self.policy),
                    ),
                    update_plan,
                ],
                mcp_servers=self._mcp_servers,
            )
        return self._agent

    @property
    def memory(self) -> SQLiteSession:
        if self._memory is None:
            self._memory = SQLiteSession(self.thread_id, str(default_session_db()))
        return self._memory

    async def close(self) -> None:
        for server in self._mcp_servers:
            try:
                await server.cleanup()
            except Exception:
                pass

    # -- turn driving ------------------------------------------------------

    async def run_turn(self, user_message: str, renderer: Renderer) -> str | None:
        """Run one user turn to completion, resolving approval interruptions
        via the renderer. Returns the final output text (None if aborted)."""
        agent = await self.agent()
        if self._sent_initial_context:
            payload: Any = user_message
        else:
            payload = f"{self._initial_context_message()}\n\n{user_message}"
            self._sent_initial_context = True

        result = Runner.run_streamed(
            agent,
            payload,
            session=self.memory,
            context=self.context,
            max_turns=self.cfg.max_turns,
        )
        final = await self._consume(result, renderer)
        while result.interruptions:
            state = result.to_state()
            for item in result.interruptions:
                answer = await self._resolve_interruption(item, renderer)
                if answer.approved:
                    state.approve(item, always_approve=answer.always_for_session)
                else:
                    message = answer.feedback or "The user declined this action."
                    state.reject(item, rejection_message=message)
            result = Runner.run_streamed(
                agent,
                state,
                session=self.memory,
                context=self.context,
                max_turns=self.cfg.max_turns,
            )
            final = await self._consume(result, renderer)
        return final

    async def _consume(self, result: RunResultStreaming, renderer: Renderer) -> str | None:
        async for event in result.stream_events():
            renderer.on_event(event)
        return result.final_output if result.is_complete else None

    async def _resolve_interruption(self, item, renderer: Renderer) -> ApprovalAnswer:
        kind = item.name or "tool"
        detail = _describe_tool_call(item)
        if self.cfg.approval_policy == "never":
            return ApprovalAnswer(
                approved=False,
                feedback="Approval required but approval policy is 'never'; "
                "choose an approach that stays within the sandbox.",
            )
        answer = await renderer.ask_approval(kind, f"Approve {kind}?", detail)
        if answer.approved and answer.add_prefix_rule and kind == "shell":
            for command in _shell_commands_of(item):
                self.policy.add_prefix_rule(command)
        if answer.approved and answer.always_for_session:
            if kind == "shell":
                self.policy.approve_all_shell = True
            elif kind == "apply_patch":
                self.policy.approve_all_patches = True
        return answer


def _shell_commands_of(item) -> list[str]:
    # Shell interruptions carry the wire item in raw_item; function-tool
    # interruptions carry a JSON arguments dict.
    raw = getattr(item, "raw_item", None)
    action = getattr(raw, "action", None) or (
        raw.get("action") if isinstance(raw, dict) else None
    )
    if action is not None:
        commands = getattr(action, "commands", None) or (
            action.get("commands") if isinstance(action, dict) else None
        )
        if commands:
            return list(commands)
    args = item.arguments if isinstance(item.arguments, dict) else {}
    return list((args.get("action") or {}).get("commands") or [])


def _describe_tool_call(item) -> str:
    commands = _shell_commands_of(item)
    if commands:
        return "\n".join(f"$ {c}" for c in commands)
    if isinstance(item.arguments, dict):
        return "\n".join(f"{k}: {str(v)[:200]}" for k, v in item.arguments.items())
    return str(item.arguments)[:400]


async def stream_turn_events(
    session: CodexSession, user_message: str, renderer: Renderer
) -> AsyncIterator[Any]:
    """Convenience wrapper for tests: yields nothing, runs the turn."""
    await session.run_turn(user_message, renderer)
    return
    yield  # pragma: no cover
