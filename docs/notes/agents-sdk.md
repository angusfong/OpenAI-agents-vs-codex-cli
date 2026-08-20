# OpenAI Agents SDK (Python) — Engineering Reference for a Codex-CLI-like Agent

Assessed source: `/workspace/openai/openai-agents-python` @ **v0.22.0** (`pyproject.toml`, `src/agents/version.py`).
This version is far ahead of most public docs: it has **tool approvals / interruptions (HITL), ShellTool, ApplyPatchTool, a V4A diff engine, RunState serialization, and a sandbox subsystem** natively in Python.

---

## 1. Version + packaging

`pyproject.toml`:
- `name = "openai-agents"`, `version = "0.22.0"`, `requires-python >= 3.10`, MIT.
- Core deps: `openai>=3.0.0,<4`, `pydantic>=2.12.2`, `griffelib`, `typing-extensions`, `requests`, `websockets>=15`, `mcp>=1.19.0,<3`.
- Extras: `litellm`, `any-llm`, `voice` (numpy), `viz` (graphviz), `realtime`, `sqlalchemy`, `redis`, `mongodb`, `dapr`, `encrypt` (cryptography), plus **sandbox backends**: `docker`, `e2b`, `modal`, `daytona`, `runloop`, `blaxel`, `cloudflare`, `vercel`, `s3`, `temporal`.
- Package root: `src/agents/` (import name `agents`). Public API re-exported from `src/agents/__init__.py`.

## 2. Runner API + agent loop

`src/agents/run.py` — `class Runner` (classmethod facade over `AgentRunner`, line 537):

```python
result = await Runner.run(agent, "prompt")                      # -> RunResult
result = Runner.run_sync(agent, "prompt")                       # blocking wrapper
result = Runner.run_streamed(agent, "prompt")                   # -> RunResultStreaming (not awaited)
```

Full signature (all three share it):

```python
Runner.run(starting_agent, input,            # input: str | list[TResponseInputItem] | RunState
    *, context=None, max_turns=DEFAULT_MAX_TURNS,  # DEFAULT_MAX_TURNS = 10 (run_config.py:44); None = unlimited
    hooks=None, run_config=None, error_handlers=None,
    previous_response_id=None, auto_previous_response_id=False,
    conversation_id=None, session=None)
```

- **Loop** (docstring + `run_internal/run_loop.py`, `run_steps.py`): invoke model → if output matches `agent.output_type` → done; if handoff → swap agent, loop; else execute tool calls (in parallel, via `run_internal/tool_execution.py`), append results, loop. `MaxTurnsExceeded` raised past `max_turns` (interceptable via `error_handlers` keyed by error kind, e.g. `"max_turns"`).
- **`input` accepts a `RunState`** — this is how interrupted (approval-pending) runs resume (see §4).
- `Agent` (`agent.py`): `name`, `instructions` (str or `(ctx, agent) -> str` callable, sync/async), `prompt`, `tools`, `mcp_servers`, `mcp_config`, `handoffs`, `model` (str or `Model` instance), `model_settings`, `input_guardrails`/`output_guardrails`, `output_type`, `hooks`, `tool_use_behavior` (`"run_llm_again"` default | `"stop_on_first_tool"` | `StopAtTools` | custom function), `reset_tool_choice=True`.
- `RunConfig` (`run_config.py`): `model` (global override), `model_provider` (default `MultiProvider()`), `model_settings` (global overlay), `handoff_input_filter`, `nest_handoff_history`, guardrail lists, `tracing_disabled`, `trace_include_sensitive_data`, `workflow_name`, `trace_id`, `group_id`, `trace_metadata`, `session_settings`, `max_function_tool_concurrency`, `pre_approval_tool_input_guardrails`, `sandbox: SandboxRunConfig`.
- **`RunResult`** (`result.py:482`): `final_output`, `new_items: list[RunItem]`, `raw_responses`, `input`, `last_agent`, `context_wrapper.usage`, `interruptions: list[ToolApprovalItem]`, `.to_input_list()`, `.to_state()`, `.final_output_as(cls)`, `.last_response_id`.
- **`RunResultStreaming`** (`result.py:595`): same base plus `current_turn`, `is_complete`, `.stream_events()` (async iterator), `.cancel(mode="immediate"|"after_turn")`, and `interruptions` (populated once the stream finishes a turn with pending approvals).

## 3. Streaming

`src/agents/stream_events.py` — `StreamEvent = RawResponsesStreamEvent | RunItemStreamEvent | AgentUpdatedStreamEvent`.

```python
result = Runner.run_streamed(agent, prompt)
async for event in result.stream_events():
    if event.type == "raw_response_event":         # event.data: TResponseStreamEvent (openai Responses stream event)
        ...
    elif event.type == "run_item_stream_event":    # event.name + event.item (RunItem)
        ...
    elif event.type == "agent_updated_stream_event": ...
```

### Raw events — reasoning summaries: VERIFIED, they flow through

- `OpenAIResponsesModel.stream_response` (`models/openai_responses.py:639`) does `async for chunk in stream: ... yield chunk` — **every Responses API SSE event is passed through verbatim**, including `response.reasoning_summary_part.added/.done`, `response.reasoning_summary_text.delta/.done`, `response.output_text.delta`, `response.output_item.added/done`, `response.function_call_arguments.delta`, etc.
- Even the **Chat Completions path synthesizes them**: `models/chatcmpl_stream_handler.py` emits `ResponseReasoningSummaryPartAddedEvent` / `ResponseReasoningSummaryTextDeltaEvent` (`type="response.reasoning_summary_text.delta"`, lines 765–781) from providers that stream reasoning content — so a CLI's "thinking" pane works on both model classes.
- **Enable** via `ModelSettings.reasoning` (an `openai.types.shared.Reasoning`):

```python
from openai.types.shared import Reasoning
agent = Agent(name="coder", model="gpt-5.1",
    model_settings=ModelSettings(reasoning=Reasoning(effort="medium", summary="detailed")))
# effort: "none"|"low"|"medium"|"high"|"xhigh" (examples/basic/hello_world_gpt_5.py)
```

First-party example: `examples/basic/stream_ws.py` (line 112 handles `response.reasoning_summary_text.delta`; line 195 sets `Reasoning(effort="medium", summary="detailed")`). Also `examples/reasoning_content/` for gpt-oss/chatcmpl reasoning.

### Run-item events

`RunItemStreamEvent.name` values: `message_output_created`, `handoff_requested`, `handoff_occured` (sic), `tool_called`, `tool_output`, `tool_search_called`, `tool_search_output_created`, `reasoning_item_created`, `mcp_approval_requested`, `mcp_approval_response`, `mcp_list_tools`. `event.item` is a `RunItem` from `items.py` (`MessageOutputItem`, `ToolCallItem`, `ToolCallOutputItem`, `ReasoningItem`, `HandoffCallItem`, `ToolApprovalItem`, ...). Use `ItemHelpers.text_message_output(item)` for text extraction.

## 4. Human-in-the-loop / approvals — FULLY PRESENT in the Python SDK

The old "JS-only" gap is closed. Machinery: `tool.py` (`needs_approval` on tools), `items.py:563` (`ToolApprovalItem`), `run_state.py` (`RunState`, 5.5k lines), `run_internal/approvals.py`, `util/_approvals.py`.

- **Declare**: `@function_tool(needs_approval=True)` or `needs_approval=async (run_context, tool_params: dict, call_id) -> bool` (per-call policy hook — this is where a CLI's approval-policy engine plugs in). Same field on `ShellTool` (signature `(ctx, action, call_id)`), `ApplyPatchTool` (`(ctx, operation, call_id)`), `CustomTool`; `on_approval` optional immediate auto-decide callback (`ShellOnApprovalFunctionResult = {"approve": bool, "reject_message"?}`). Hosted-environment ShellTool forbids approvals (runs server-side).
- **Interrupt**: when approval is needed the run *returns* (does not raise): `result.interruptions: list[ToolApprovalItem]` non-empty; each item has `.agent`, `.name`, `.arguments`, `.raw_item`, `tool_origin`.
- **Decide + resume** (`run_state.py:1254/1269`):

```python
result = await Runner.run(agent, "do the thing")
while result.interruptions:
    state = result.to_state()                       # RunState
    for item in result.interruptions:
        if ok: state.approve(item, always_approve=False)
        else:  state.reject(item, always_reject=False, rejection_message="user said no")
    result = await Runner.run(agent, state)          # resume with state as input
```

- `always_approve`/`always_reject` persist a sticky per-tool decision in the run context ("don't ask again"). `state.get_interruptions()` returns detached copies. Nested agent-as-tool interruptions are routed automatically.
- **Persistence**: `state.to_json()` → dict; `await RunState.from_json(agent, data)` / `RunState.from_string(...)` restore across processes (used by `examples/agent_patterns/human_in_the_loop.py`, which writes state to a JSON file and reloads it). Custom (non-JSON) contexts need `context_serializer`.
- **Streaming variant**: `examples/agent_patterns/human_in_the_loop_stream.py` — after `stream_events()` completes, check `result.interruptions`, `result.to_state()`, approve, then `Runner.run_streamed(agent, state)` again.
- Related: `RunConfig.pre_approval_tool_input_guardrails`; hosted MCP approvals are separate (§7); `ComputerTool` has `on_safety_check` (`ComputerToolSafetyCheckData`).

A CLI therefore only implements the *policy* (auto-approve reads, prompt for writes/shell, session-scoped "always allow") and the prompt UI, not the mechanism.

## 5. Sessions (history persistence, resume/fork)

`src/agents/memory/`:
- `Session` protocol (`session.py`): `get_items(limit)`, `add_items(items)`, `pop_item()`, `clear_session()` over `TResponseInputItem` dicts.
- **`SQLiteSession(session_id, db_path=":memory:")`** (`sqlite_session.py`) — file-locked, table-per-session, ideal for CLI rollout storage: `SQLiteSession("chat-42", "~/.mycli/history.db")`.
- **`OpenAIConversationsSession`** (`openai_conversations_session.py`) — server-side Conversations API; also `start_openai_conversations_session()`. Alternatively skip sessions and pass `conversation_id=` or `previous_response_id=` (+`auto_previous_response_id=True`) to `Runner.run` for server-side threading.
- Extensions (`extensions/memory/`): `AdvancedSQLiteSession` (usage stats, branching), `AsyncSQLiteSession`, `SQLAlchemySession`, `EncryptSession`, Redis/Mongo/Dapr variants. `openai_responses_compaction_session.py` = automatic context compaction; also server-side via `ModelSettings.context_management=[{"type":"compaction","compact_threshold":...}]`.
- Usage: `await Runner.run(agent, "next msg", session=session)` — history auto-prepended and results auto-appended.
- **Resume** = reuse same `session_id`/db file. **Fork** = manual: `items = await old.get_items()`; `await new.add_items(items)` (or copy rows / use `result.to_input_list()` as the next run's input). No first-party fork API.

## 6. Tools

`src/agents/tool.py` (2832 lines). Types union `Tool = FunctionTool | FileSearchTool | WebSearchTool | ComputerTool | HostedMCPTool | LocalShellTool | ShellTool | ApplyPatchTool | CustomTool | CodeInterpreterTool | ImageGenerationTool ...`

### @function_tool (line 2455; alias `@tool` in `decorators.py`)

```python
@function_tool  # or function_tool(name_override=..., needs_approval=..., timeout=30, ...)
async def read_file(path: str) -> str:
    """Read a file.  Args: path: absolute path."""   # docstring -> schema descriptions
    ...
```

Keyword options: `name_override`, `description_override`, `docstring_style`, `use_docstring_info`, `failure_error_function` (exception → model-visible string; pass `None` to raise instead; default `default_tool_error_function`), `strict_mode`, `is_enabled` (bool or callable — dynamic tool hiding), `needs_approval`, `tool_input_guardrails`/`tool_output_guardrails`, `timeout` + `timeout_behavior` (`"error_as_result"`|`"raise_exception"`), `defer_loading` (Responses tool-search), `output_type`/`output_json_schema`, `allowed_callers`. First param may be `RunContextWrapper[T]`/`ToolContext` for context access. Rich outputs: `ToolOutputText`/`ToolOutputImage`/`ToolOutputFileContent`.

### Built-in local shell — YES (two generations)

- **`ShellTool`** (line 1362, "next-generation"; `LocalShellTool` at 1167 is legacy for `local_shell`-capable models): `ShellTool(executor=..., needs_approval=..., on_approval=..., environment=...)`. Executor: `(ShellCommandRequest) -> str | ShellResult`; `request.data.action` = `ShellActionRequest(commands: list[str], timeout_ms, max_output_length)`; return `ShellResult(output=[ShellCommandOutput(stdout, stderr, outcome=ShellCallOutcome(type="exit"|"timeout", exit_code), command)])`. **You supply the subprocess execution** (see `examples/tools/shell.py` for a complete asyncio-subprocess executor + approval callback, and `shell_human_in_the_loop.py`). `environment` may be `{"type":"local"}` (default, requires executor) or hosted container envs (`container.auto` / reference, with network policy allowlists and skills — server-side execution, no approvals).
- **`ApplyPatchTool`** (line 1418): `ApplyPatchTool(editor=..., needs_approval=...)`. `editor` implements `ApplyPatchEditor` protocol (`editor.py`): `create_file/update_file/delete_file(operation: ApplyPatchOperation(type, path, diff, move_to)) -> ApplyPatchResult|str|None`. The SDK ships the **V4A patch parser**: `agents.apply_diff.apply_diff(input_text, diff, mode="default"|"create")` (`apply_diff.py`, handles `*** Update File:`/`*** Add File:`/`*** Delete File:`/`*** End Patch` markers with context-hunk fuzz) — the same patch format Codex CLI uses. Full workspace-editor with approvals + path jailing: `examples/tools/apply_patch.py`.
- Hosted tools: `WebSearchTool(user_location, search_context_size, filters)`, `FileSearchTool(vector_store_ids, max_num_results, include_search_results)`, `CodeInterpreterTool`, `ImageGenerationTool`, `HostedMCPTool`, `ComputerTool(computer=...)` (+`Computer`/`AsyncComputer` interfaces in `computer.py`). `CustomTool` = Responses raw-string-input tool (custom grammar/format).
- Bonus: `agents.extensions.experimental.codex.codex_tool` (`examples/tools/codex.py`) wraps the **Codex CLI itself** as a tool with thread/turn events — experimental.
- Sandbox subsystem (`src/agents/sandbox/`, `SandboxAgent`, `RunConfig(sandbox=SandboxRunConfig(...))`): manifest-declared workspaces, Docker/unix-local runtimes (`sandbox/sandboxes/{docker,unix_local}.py`) plus e2b/modal/daytona/runloop/cloudflare backends in extensions, snapshots, mount-security, its own shell/apply_patch/view_image capability tools (`sandbox/capabilities/tools/`). Heavyweight but relevant if you want sandboxed execution without writing it.

## 7. MCP

`src/agents/mcp/server.py` + `util.py`:
- `MCPServerStdio(params={"command": "npx", "args": [...], "env": ...}, cache_tools_list=True, client_session_timeout_seconds=...)`, `MCPServerSse(params={"url", "headers", ...})`, `MCPServerStreamableHttp(params={"url", ...})`. All are async context managers (`await server.connect()` / use `async with`); attach via `Agent(mcp_servers=[...])`.
- **Tool filtering**: `tool_filter=create_static_tool_filter(allowed_tool_names=[...], blocked_tool_names=[...])` or a dynamic callable receiving `ToolFilterContext(run_context, agent, server_name)`.
- **Approvals**: every server constructor takes `require_approval` — `"always"`/`"never"`, bool, `{"always": {"tool_names": [...]}, "never": {...}}`, per-tool dict, or a callable → produces the same `ToolApprovalItem` interruption flow as §4. `HostedMCPTool(tool_config={"require_approval": ...}, on_approval_request=...)` for server-hosted MCP (`MCPToolApprovalRequest` → `{"approve": bool, "reason"?}`), surfaced in streams as `mcp_approval_requested`.
- MCP tools are converted to `FunctionTool`s (`MCPUtil.get_all_function_tools`), with optional name-prefixing (`include_server_in_tool_names`) and `mcp_config` on the agent (`convert_schemas_to_strict`, `failure_error_function`).

## 8. Model layer

`src/agents/models/`:
- `Model`/`ModelProvider` interfaces (`interface.py`): `get_response(...)` and `stream_response(...)`.
- **`OpenAIResponsesModel`** (`openai_responses.py:482`) — default; native hosted tools, reasoning, `previous_response_id`, `include` computation from tools + `response_include`. There is also a websocket transport (`responses_websocket_session.py`, `examples/basic/stream_ws.py`) for lower-latency chained turns.
- **`OpenAIChatCompletionsModel`** (`openai_chatcompletions.py`) — converts items ↔ chat messages (`chatcmpl_converter.py`) and synthesizes Responses-style stream events (incl. reasoning summary deltas) for compatible providers.
- **Custom endpoint**: `set_default_openai_client(AsyncOpenAI(base_url=..., api_key=...), use_for_tracing=False)`, `set_default_openai_api("chat_completions"|"responses")`, `set_default_openai_key(...)` (`_config.py`); or `OpenAIProvider(base_url=..., api_key=..., use_responses=...)`; or per-agent `Agent(model=OpenAIChatCompletionsModel(model="...", openai_client=client))`.
- **`MultiProvider`** (`multi_provider.py`, RunConfig default): prefix routing — `"gpt-5.1"`/`"openai/..."` → OpenAI, `"litellm/anthropic/claude-..."` → `LitellmModel` (`extensions/models/litellm_model.py`, extra `[litellm]`), `"any-llm/..."` → any-llm. Extendable via `MultiProviderMap.add_provider(prefix, provider)`.
- `ModelSettings` (§3 file `model_settings.py`) knobs: `temperature`, `top_p`, penalties, `tool_choice` (incl. `MCPToolChoice(server_label, name)`), `parallel_tool_calls`, `truncation` (`"auto"|"disabled"`), `max_tokens`, **`reasoning`** (`Reasoning(effort=..., summary="auto"|"concise"|"detailed")`), **`verbosity`**, `metadata`, `store`, `prompt_cache_retention`/`prompt_cache_options`, `include_usage`, `response_include`, `top_logprobs`, `extra_query/body/headers/args`, `retry` (runner-managed model retries + backoff), `context_management` (server-side compaction), `timeout`. `resolve()` overlays run-level over agent-level settings.

## 9. Guardrails, hooks, usage, tracing

- **Guardrails** (`guardrail.py`): `@input_guardrail` / `@output_guardrail` → return `GuardrailFunctionOutput(output_info, tripwire_triggered)`; tripwire raises `Input/OutputGuardrailTripwireTriggered`. Input guardrails run only for the first agent; attach on `Agent(...)` or `RunConfig`. **Tool guardrails** (`tool_guardrails.py`): `ToolInputGuardrail`/`ToolOutputGuardrail` with behaviors `allow` / `reject_content` (message to model, keep running) / `raise_exception`.
- **Hooks** (`lifecycle.py`) — a CLI's UI event bus alongside stream events: `RunHooks`: `on_agent_start/end`, `on_llm_start/end`, `on_tool_start/end`, `on_handoff`. `AgentHooks`: same per-agent (`on_start/on_end/...`). Pass `hooks=` to `Runner.run` or `Agent(hooks=...)`. `examples/basic/agent_lifecycle_example.py`.
- **Usage** (`usage.py`): `result.context_wrapper.usage` → `Usage(requests, input_tokens, input_tokens_details.cached_tokens, output_tokens, output_tokens_details.reasoning_tokens, total_tokens, request_usage_entries)` — `request_usage_entries` preserves the per-request breakdown (per-call cost display / context-window meters). Per-response: `raw_responses[i].usage`. `ModelSettings.preserve_raw_usage` keeps the unnormalized provider payload.
- **Tracing** (`tracing/`): default exports to OpenAI's traces dashboard. Disable: `set_tracing_disabled(True)`, `RunConfig(tracing_disabled=True)`, or env `OPENAI_AGENTS_DISABLE_TRACING=1`. Redirect: `add_trace_processor(...)` / `set_trace_processors([...])` (replace exporter entirely); `set_tracing_export_api_key(...)`; `RunConfig.trace_include_sensitive_data=False` scrubs LLM/tool IO; custom spans via `trace()`, `custom_span()`. For a local CLI: disable, or point a processor at local rollout logs.

## 10. GAPS — what a Codex-CLI-parity build must implement itself

The SDK gives you: agent loop, streaming (incl. reasoning summaries), approval interruptions + serializable resume state, shell/apply_patch tool *frames* + V4A parser, MCP, sessions, provider abstraction, usage. You must build:

1. **Terminal UI**: everything — TUI framework (textual/rich/prompt_toolkit), streaming render of reasoning vs answer panes, diff colorization, spinners, transcripts. SDK only emits events.
2. **Approval policy engine**: Codex's `--ask-for-approval` modes (`untrusted`/`on-failure`/`on-request`/`never`), auto-approving known-safe commands, session-scoped "always allow" UX. SDK gives the `needs_approval(ctx, params, call_id)` callback + sticky `always_approve` — the classification logic (command parsing, writable-path analysis) is yours.
3. **Sandboxing of the default local shell**: `ShellTool` executor is a raw subprocess you write; no seatbelt/landlock/seccomp equivalent for plain local exec. `agents.sandbox` (Docker/unix-local/e2b/...) exists but is a different, manifest-based execution model you'd have to adopt wholesale.
4. **AGENTS.md / project-doc discovery**: no support. Implement file discovery (repo root → cwd chain), merge, and inject into `instructions`.
5. **Prompt engineering**: Codex's system prompt, environment context block (cwd, git status, platform), tool usage rules — all yours.
6. **Rollout/session files + resume/fork UX**: `SQLiteSession` + `RunState.to_json()` are storage primitives; `~/.codex/sessions`-style JSONL rollouts, `--resume`/`--continue` pickers, forking are yours.
7. **Slash commands, config**: `/model`, `/approvals`, `/compact`(you can call Responses compaction or `context_management`), `config.toml`, profiles, keybindings.
8. **Git integration**: diff review, "you are on branch X" warnings, commit/PR helpers.
9. **apply_patch editor semantics**: path jailing, atomicity, dry-run preview, per-hunk approval — SDK parses V4A (`apply_diff`) but the editor (`ApplyPatchEditor`) enforcement is yours (crib `examples/tools/apply_patch.py`).
10. **Cross-provider parity for the shell/apply_patch tool types**: `ShellTool`/`ApplyPatchTool`/`CustomTool` are Responses-API tool types; for non-OpenAI models via LiteLLM you may need to re-expose them as plain function tools.
11. **Interrupt handling (Ctrl-C mid-turn)**: `RunResultStreaming.cancel("immediate"|"after_turn")` exists; graceful "stop and keep partial transcript" UX is yours.
12. **Auth**: API-key management/ChatGPT-style OAuth login, keychain storage.
13. **Update/version checks, telemetry opt-out, MCP server config management** (`mcp_servers` in config → constructing `MCPServerStdio` instances at startup).

### Minimal skeleton

```python
from agents import Agent, Runner, ModelSettings, ShellTool, ApplyPatchTool, SQLiteSession
from openai.types.shared import Reasoning

agent = Agent(
    name="codex-py", instructions=SYSTEM_PROMPT + agents_md,
    model="gpt-5.1",
    model_settings=ModelSettings(reasoning=Reasoning(effort="medium", summary="auto"),
                                 parallel_tool_calls=True, truncation="auto"),
    tools=[ShellTool(executor=my_exec, needs_approval=policy.shell),
           ApplyPatchTool(editor=my_editor, needs_approval=policy.patch)])

session = SQLiteSession(chat_id, db_path)
result = Runner.run_streamed(agent, user_msg, session=session, max_turns=None)
async for ev in result.stream_events(): render(ev)
while result.interruptions:
    state = result.to_state()
    for item in result.interruptions: (state.approve if ui_ok(item) else state.reject)(item)
    result = Runner.run_streamed(agent, state, session=session)
    async for ev in result.stream_events(): render(ev)
```

Key files: `src/agents/{run,run_state,result,stream_events,tool,editor,apply_diff,model_settings,items,lifecycle,guardrail,tool_guardrails,usage}.py`, `src/agents/models/{openai_responses,chatcmpl_stream_handler,multi_provider}.py`, `src/agents/memory/`, `src/agents/mcp/`, `src/agents/sandbox/`; examples: `examples/agent_patterns/human_in_the_loop*.py`, `examples/tools/{shell,apply_patch,codex}.py`, `examples/basic/stream_ws.py`.
