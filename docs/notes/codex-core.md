# Codex CLI Core Engine — Engineering Reference

Source: `/workspace/openai/codex` @ `4a942885c` (2026-08-20). Focus: `codex-rs/` workspace, crate `core`
(`codex-core`) plus the satellite crates it drives. All paths below are relative to `codex-rs/` unless
absolute. Line numbers are from this commit.

---

## 1. The agent loop

**Crate layering.** `core` is the engine; `tui`/`exec`/`app-server`/`mcp-server` are frontends over its
`Session`/`ThreadManager` API. Protocol types (events, items, policies) live in `protocol`
(`codex-protocol`); HTTP/WS request plumbing in `codex-api`; provider registry in `model-provider-info`.

**Task → turn → sampling request.** A user submission spawns a `RegularTask`
(`core/src/tasks/regular.rs:30`), which loops calling `run_turn` while queued user input is pending
(`regular.rs:76-91`). `run_turn` (`core/src/session/turn.rs:153`) is the real agent loop. Its doc comment:

> Takes initial turn input and runs a loop where, at each sampling request, the model replies with either
> requested function calls [or] an assistant message. … If the model requests a function call, we execute
> it and send the output back to the model in the next sampling request. If the model sends only an
> assistant message, we record it … and consider the turn complete.

Loop structure (`turn.rs:301-588`):
1. Drain pending user input ("steering" typed mid-turn) into history.
2. Capture a `StepContext` — one immutable view of config/model/tools/environments per sampling request.
3. Build the model input as the **entire recorded history** (`sess.clone_history().await.for_prompt(...)`,
   `turn.rs:370-376`) — no `previous_response_id`; the request is stateless (`store: false`).
4. `run_sampling_request` (`turn.rs:1342`) → `try_run_sampling_request` (`turn.rs:2181`) opens the stream
   and consumes `ResponseEvent`s.
5. On `Completed`, compute `needs_follow_up = model made tool calls || pending user input`; if follow-up and
   token limit reached → inline auto-compact then `continue`; if no follow-up → run stop hooks and `break`.

**There is no hard iteration cap** — the loop runs until the model stops calling tools, the user aborts
(`CodexErr::TurnAborted` via `CancellationToken`), or a fatal error occurs. Stream retries per sampling
request are bounded: `stream_max_retries()` default **5**, user-capped at 100
(`model-provider-info/src/lib.rs:27,32`); retry loop in `run_sampling_request` (`turn.rs:1370-1441`) with
`ResponsesStreamRetryState` (`core/src/responses_retry.rs`). Special-cased errors: `ContextWindowExceeded`
(marks tokens full, aborts), `UsageLimitReached` (updates rate-limit snapshot), `TurnAborted` (propagates);
anything else becomes an `ErrorEvent` and the turn ends gracefully (`turn.rs:555-586`).

**Tool dispatch.** Stream events are handled in `try_run_sampling_request` (`turn.rs:2252-2740`):
- `ResponseEvent::OutputItemDone(item)` → `handle_output_item_done` builds a `ToolCall` via
  `ToolRouter::build_tool_call` (`core/src/tools/router.rs:154`) from `FunctionCall`, `CustomToolCall`
  (freeform, e.g. apply_patch), `LocalShellCall`, `ToolSearchCall` items, and returns a future that is
  pushed into a `FuturesOrdered` (`turn.rs:2393-2395`) — tools execute **while the stream continues**.
- `ToolCallRuntime` (`core/src/tools/parallel.rs:41`) gates concurrency with an `Arc<RwLock<()>>`:
  parallel-safe tools acquire a read lock, serial tools a write lock (`parallel.rs:113-158`). Whether the
  API even requests parallel calls is `parallel_tool_calls: prompt.parallel_tool_calls && !responses_lite`
  (`core/src/client.rs:930`).
- Tool outputs (`FunctionCallOutput`/`CustomToolCallOutput`) are appended to history and set
  `needs_follow_up = true`, so the loop samples again with the outputs included.
- Delta events (`OutputTextDelta`, `ReasoningSummaryDelta`, `ToolCallInputDelta`, …) fan out as protocol
  events (`AgentMessageContentDelta`, `AgentReasoning*`) to the UI.

**Context / token management.** After every sampling request the loop computes
`context_window_token_status` (`core/src/session/context_window.rs`) from the token usage returned in
`response.completed`. Auto-compaction threshold comes from `model_info.auto_compact_token_limit` (or config
override `model_auto_compact_token_limit`), with the usable window scaled by
`effective_context_window_percent: 95` (`models-manager/src/model_info.rs`, fallback metadata). When
`token_limit_reached && needs_follow_up`, the loop calls `run_auto_compact` **mid-turn** (`turn.rs:472-500`)
and continues; there is also `run_pre_sampling_compact` before the first request (`turn.rs:169`).

**Compaction** (`core/src/compact.rs`, plus `compact_remote*.rs` for server-side `/responses/compact`):
a summarization request is sent using the prompt template `prompts/templates/compact/prompt.md`:

> You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that will
> resume the task. Include: current progress and key decisions … what remains to be done …

`build_compacted_history` (`compact.rs:639`) rebuilds history as: initial context + retained user messages
(capped at `COMPACT_USER_MESSAGE_MAX_TOKENS = 20_000`, `compact.rs:57`) + a bridge message from
`templates/compact/summary_prefix.md` ("Another language model started to solve this problem and produced a
summary…"). Remote compaction (OpenAI providers, `RemoteCompactionSupport`) posts the whole thread to
`/responses/compact` and receives a `Compaction` item back. There is also a token-budget feature
(`Feature::TokenBudget`) that exposes `get_context_remaining` / `new_context_window` tools and can roll the
thread into a fresh context window (`should_roll_over`, `turn.rs:460-500`).

History itself is a `ContextManager` (`core/src/context_manager/`) of `ResponseItem`s persisted to rollout
files (`rollout/`), reconstructed on resume (`core/src/session/rollout_reconstruction.rs`).

---

## 2. System prompt / base instructions

**Current base prompt:** `models-manager/prompt.md` (275 lines), compiled in as
`BASE_INSTRUCTIONS` (`models-manager/src/model_info.rs:17`) and used as the Responses API `instructions`
field. Server-fetched model catalogs can replace it (`instructions_template` per model in `ModelMessages`);
`gpt-5.2-codex` gets a personality header spliced in (`model_info.rs:190`). Config `base_instructions`
overrides everything.

**Legacy per-model prompts** still ship in `core/`: `gpt_5_codex_prompt.md` (68 lines, terse),
`gpt-5.1-codex-max_prompt.md`, `gpt-5.2-codex_prompt.md`, `gpt_5_1_prompt.md`, `gpt_5_2_prompt.md`, and
`prompt_with_apply_patch_instructions.md` (adds the full patch-format tutorial for models without the
freeform tool).

Structure of `prompt.md` (headers): Personality → **AGENTS.md spec** → Responsiveness (preamble messages)
→ Planning → Task execution → Validating your work → Ambition vs. precision → Sharing progress updates →
Presenting your work / final answer formatting → Tool Guidelines (`shell`, `update_plan`).

Load-bearing passages (verbatim):

- Identity: "You are a coding agent running in the Codex CLI, a terminal-based coding assistant. … You are
  expected to be precise, safe, and helpful."
- Persistence: "Please keep going until the query is completely resolved, before ending your turn and
  yielding back to the user. … Do NOT guess or make up an answer."
- apply_patch: "Use the `apply_patch` tool to edit files (NEVER try `applypatch` or `apply-patch`, only
  `apply_patch`): {\"command\":[\"apply_patch\",\"*** Begin Patch\\n*** Update File: path/to/file.py\\n@@ def
  example():\\n- pass\\n+ return 123\\n*** End Patch\"]}"
- Preambles: "Before making tool calls, send a brief preamble to the user explaining what you're about to
  do" — 8–12 words, grouped, light/friendly, skipped for trivial reads.
- Planning: `update_plan` "not for padding out simple work"; no single-step plans; exactly one
  `in_progress` step.
- AGENTS.md: "For every file you touch in the final patch, you must obey instructions in any AGENTS.md file
  whose scope includes that file. … More-deeply-nested AGENTS.md files take precedence. … Direct
  system/developer/user instructions take precedence over AGENTS.md instructions."
- Approval-mode-aware validation: "When running in the non-interactive approval mode **never**, proactively
  run tests… When working in interactive approval modes like **untrusted**, or **on-request**, hold off on
  running tests or lint commands until the user is ready."
- Final answers: plain text styled by the CLI; `**Title Case**` headers optional; `-` bullets, 4–6 per
  list; backticks for paths/commands; clickable file references `src/app.ts:42` (no URIs, no line ranges);
  "Brevity is very important as a default … no more than 10 lines" unless detail is needed.
- Hygiene rules: no `git commit` unless asked, no inline comments unless asked, no copyright headers, don't
  re-read files after `apply_patch`, never output `【F:...†L...】` citations.

**Sandbox/approvals awareness is injected separately**, not in the base prompt: templated developer
messages from `prompts/templates/permissions/` rendered by `codex-prompts`
(`prompts/src/permissions_instructions.rs`), e.g. `sandbox_mode/workspace_write.md`:

> Filesystem sandboxing defines which files can be read or written. `sandbox_mode` is `workspace-write`:
> The sandbox permits reading files, and editing files in `cwd` and `writable_roots`. Editing files in
> other directories requires approval. Network access is {{ network_access }}.

and `approval_policy/on_request.md`, which teaches escalation mechanics: commands are split "into
independent command segments at shell control operators" (pipes, `&&`, `;`, subshells), each segment
evaluated independently; advanced shell features (redirection, substitution, env prefixes, wildcards)
disqualify rule matching. `approval_policy/never.md`: "Do not provide the `sandbox_permissions` for any
reason, commands will be rejected."

Other prompt inputs assembled per turn (`core/src/context/`): `<environment_context>` (cwd, sandbox/
approval summary, shell), `<user_instructions>` (AGENTS.md content), permissions instructions, skills/
plugins/connectors instructions, current-time reminder, token-budget context, world-state diffs.

---

## 3. Tools exposed to the model

Registered per step in `core/src/tools/spec_plan.rs` (`build_tool_router`); specs in
`core/src/tools/handlers/*_spec.rs`; handlers in `handlers/`, executed through
`registry.rs`/`router.rs`/`orchestrator.rs`.

**Shell family** (`handlers/shell_spec.rs`) — which variant depends on
`shell_type_for_model_and_features` (`spec_plan.rs:980`):
- `shell_command` (classic, one-shot): params `command` (string, required — a script for the user's
  default shell), `workdir`, `timeout_ms` (default 10000), optional `login`; plus approval params below.
  Description: "Runs a shell command and returns its output. Always set the `workdir` param … Do not use
  `cd` unless absolutely necessary."
- Unified exec (PTY-session model, used by codex models): `exec_command` — params `cmd` (required),
  `workdir`, `tty` (bool), `yield_time_ms` (default 10000, range 250–30000), `max_output_tokens`
  ("Output token budget. Defaults to 10000 tokens"), `shell`, `login`, optional `environment_id` — and
  `write_stdin` (`session_id`, `chars`, `yield_time_ms`, `max_output_tokens`) for interacting with
  still-running sessions. Output schema includes `session_id`, `exit_code`, `wall_time_seconds`,
  `original_token_count`, truncated `output` (`shell_spec.rs:264-296`). When unified exec is active the
  legacy `shell_command` stays registered but `ToolExposure::Hidden` (`spec_plan.rs:993-1000`).
- Approval params on both (`shell_spec.rs:298-344`): `sandbox_permissions` ∈ `use_default` |
  `with_additional_permissions` | `require_escalated`; `justification` ("User-facing approval question for
  `require_escalated`"); `prefix_rule` (e.g. `["git","pull"]` — reusable approval prefix);
  `additional_permissions` (a permission-profile object: `file_system.read/write` path arrays,
  `network.enabled`).

**apply_patch** (`handlers/apply_patch_spec.rs`) — a **freeform/custom tool** with a Lark grammar
(`type: "grammar", syntax: "lark"`), not JSON: "The `apply_patch` tool can be used to edit files. This is
a FREEFORM tool, so do not wrap the patch in JSON." Grammar (`handlers/apply_patch.lark`, verbatim):

```lark
start: begin_patch hunk+ end_patch
begin_patch: "*** Begin Patch" LF
end_patch: "*** End Patch" LF?
hunk: add_hunk | delete_hunk | update_hunk
add_hunk: "*** Add File: " filename LF add_line+
delete_hunk: "*** Delete File: " filename LF
update_hunk: "*** Update File: " filename LF change_move? change?
filename: /(.+)/
add_line: "+" /(.*)/ LF -> line
change_move: "*** Move to: " filename LF
change: (change_context | change_line)+ eof_line?
change_context: ("@@" | "@@ " /(.+)/) LF
change_line: ("+" | "-" | " ") /(.*)/ LF
eof_line: "*** End of File" LF
```

The parser/applier is the standalone `apply-patch` crate (`apply-patch/src/parser.rs:37-45` defines the
markers; `seek_sequence.rs` does fuzzy context matching). Update hunks are context diffs: `@@` optionally
followed by a locator line (e.g. a function signature), then ` `/`+`/`-` lines. `apply_patch` also works
as a CLI/`arg0` alias so the model can call it via shell in envs without the tool. Whether it is
registered depends on `model_info.apply_patch_tool_type` (`spec_plan.rs:1112-1116`); models without it get
`prompt_with_apply_patch_instructions.md` in the prompt instead.

**update_plan** (`handlers/plan_spec.rs:43`): function tool, args `{ explanation?, plan: [{step, status ∈
pending|in_progress|completed}] }`.

**view_image** (`handlers/view_image_spec.rs`): attach a local image path to the conversation
(feature-gated `Feature::ViewImage`).

**web_search**: hosted tool spec via `tools/hosted_spec.rs::create_web_search_tool`, sent only when
`provider.capabilities().web_search` (i.e. `web_search_request` in the Responses payload; type depends on
`model_info.web_search_tool_type`). Newer builds also support a standalone `web.run` extension tool
(`supports_standalone_web_search` on the provider).

**Others** (registered conditionally, `spec_plan.rs:1034-1140`): `request_permissions`, `request_user_input`,
`wait_for_environment`, `get_context_remaining` + `new_context_window` (token budget), `current_time` +
`sleep`, `tool_search` (deferred-tool discovery), `list_mcp_resources` / `list_mcp_resource_templates` /
`read_mcp_resource`, plugin-install tools, and multi-agent v2 collaboration tools (`spawn_agent`,
`send_message`, `wait_agent`, `interrupt_agent`, `list_agents`, …). **MCP server tools** are registered
through `codex-mcp` bindings with qualified names and per-tool approval policies (`core/src/mcp.rs`,
`tools/handlers/mcp.rs`).

**Output truncation.** Exec output is formatted by `format_exec_output_for_model` / `format_exec_output_str`
(`core/src/tools/mod.rs:99-134`): prefixes `Exit code` / `Wall time` / `Total output lines`, then truncates
via `TruncationPolicy` (`utils/output-truncation/src/lib.rs`) — **middle truncation** with a token or byte
budget (`truncate_middle_with_token_budget`). The policy comes from `model_info.truncation_policy`; the
compiled fallback is `bytes(10_000)` (`protocol/src/openai_models.rs:913`). Unified exec instead enforces
the model-supplied `max_output_tokens` (default 10k tokens) and reports `original_token_count`.

---

## 4. Wire protocol

**Responses API only.** `WireApi` has a single variant `Responses` (`model-provider-info/src/lib.rs:63`);
`wire_api = "chat"` is a **hard deserialization error**: "`wire_api = \"chat\"` is no longer supported. …
set `wire_api = \"responses\"`" (`lib.rs:56`). Endpoints (`core/src/client.rs:161-167`): `/responses`,
`/responses/compact`, `/realtime/calls`, `/memories/trace_summarize`.

**Request shape** — `ResponsesApiRequest` (`codex-api/src/common.rs:252`): `model`, `instructions`,
`input: Vec<ResponseItem>`, `tools`, `tool_choice: "auto"`, `parallel_tool_calls`, `reasoning`,
`store: false`, `stream: true`, `stream_options`, `include`, `service_tier`, `prompt_cache_key`, `text`
(verbosity + output schema), `client_metadata`. Built in `ModelClient::build_responses_request`
(`core/src/client.rs:845-942`).

**Reasoning:** `build_reasoning` (`client.rs:825-843`) sets `reasoning.effort` (config
`model_reasoning_effort` or model default) and `reasoning.summary` (`auto` by default) when
`supports_reasoning_summary_parameter`. Crucially `include = ["reasoning.encrypted_content"]`
(`client.rs:905`): since `store:false`, reasoning items come back with encrypted content and are stored in
history as `ResponseItem::Reasoning { encrypted_content, … }` (`protocol/src/models.rs:984,1152`), then
**replayed verbatim in the next request's input** so the model keeps its chain-of-thought across tool
calls. For non-OpenAI providers the encrypted args/metadata are stripped before sending
(`client.rs:856-867`). A "Responses Lite" mode (`model_info.use_responses_lite`) moves instructions+tools
into developer-role input items and sets `reasoning.context = all_turns` (`client.rs:837-891`).

**Streaming:** SSE parsing in `codex-api/src/sse/responses.rs` handles `response.created`,
`response.output_item.added/done`, `response.output_text.delta`, `response.reasoning_summary_text.delta/
done`, `response.reasoning_summary_part.added`, `response.reasoning_text.delta`, `response.completed`
(token usage), rate-limit events, etc., mapped into `ResponseEvent` consumed in `turn.rs`. A stream that
ends without `response.completed` is an error and triggers reconnect/retry. OpenAI provider also supports a
**WebSocket transport** for Responses (`supports_websockets`, beta header
`responses_websockets=2026-02-06`, `client.rs:158`) with sticky routing (`x-codex-routing-hint`,
`x-codex-turn-state` headers) and HTTP fallback (`force_http_fallback`, `client.rs:524`).

**Auth modes** (`protocol/src/auth.rs:9`): `ApiKey`, `Chatgpt` (OAuth managed by Codex),
`ChatgptAuthTokens`, `Headers`, `AgentIdentity`, `PersonalAccessToken`. Base-URL selection
(`model-provider-info/src/lib.rs:289-303`): ChatGPT-family auth → `https://chatgpt.com/backend-api/codex`
(`CHATGPT_CODEX_BASE_URL`, so requests go to `…/backend-api/codex/responses`); API key →
`https://api.openai.com/v1`. ChatGPT requests carry `Authorization: Bearer <oauth access token>` plus
`chatgpt-account-id` (from the token's `chatgpt_account_id` claim, `login/src/token_data.rs:38`); the
`login` crate runs the local OAuth flow (PKCE server, `login/src/server.rs`) and persists/refreshes
`auth.json`. Extra Codex headers: `x-codex-installation-id`, `x-codex-turn-metadata`, `x-codex-window-id`,
`OpenAI-Organization`/`OpenAI-Project` from env (`client.rs:143-159`, `lib.rs:393-403`).

**Model defaults:** current default slug is **`gpt-5.5`** (`core/src/config/mod.rs:278`); the models
catalog (served by the backend, cached via `models-manager`) defines presets incl. `gpt-5.2-codex`,
`gpt-5.1-codex-max`, Bedrock IDs up to `openai.gpt-5.6-*` (`model-provider-info/src/lib.rs:44-51`).
Fallback model metadata assumes a 272k context window. (Historically: `gpt-5-codex` / `gpt-5`; the old
prompts remain in `core/`.)

---

## 5. Approval + sandbox policy engine

**AskForApproval** (`protocol/src/protocol.rs:916`): `UnlessTrusted` (serialized `untrusted` — "Commands
require approval unless an explicit exec policy rule allows them"), `OnRequest` (default; alias
`on-failure`), `Granular(GranularApprovalConfig)` (per-category booleans: `sandbox_approval`, `rules`,
`skill_approval`, `request_permissions`, `mcp_elicitations` — `false` means auto-reject instead of
prompt), `Never`.

**SandboxPolicy** (`protocol.rs:1002`): `DangerFullAccess`, `ReadOnly { network_access }`,
`ExternalSandbox { network_access }` (process already sandboxed externally), `WorkspaceWrite
{ writable_roots, network_access, exclude_tmpdir_env_var, exclude_slash_tmp }`. `WritableRoot`
(`protocol.rs:1058`) carries `read_only_subpaths` and `protected_metadata_names` so `.git`, `.codex`,
`.agents` (notably `.git/hooks`) stay read-only even under a writable root. Modern code lowers this into a
`PermissionProfile` / `FileSystemSandboxPolicy` + `NetworkSandboxPolicy` pair (`protocol/src/permissions.rs`).

**Command classification** (`core/src/exec_policy.rs`, `execpolicy` crate): commands are canonicalized and
split into segments (`commands_for_exec_policy`, `core/src/command_canonicalization.rs`) then evaluated
against **execpolicy rules** (starlark-ish rule files; `docs/execpolicy.md`) yielding `Decision::Allow |
Prompt | Forbidden`. Mapping to `ExecApprovalRequirement` (`exec_policy.rs:376-440`): Forbidden → reject
with reason; Prompt → `NeedsApproval` (or Forbidden under `Never`/Granular-off) with a
`proposed_execpolicy_amendment` (a prefix rule like `["git","push"]` the UI can persist as "don't ask
again"); Allow → `Skip { bypass_sandbox: true }` only if **every** segment matched an explicit allow rule.
Unmatched commands fall back to `default_exec_approval_requirement` (`tools/sandboxing.rs:189-230`):

> Never: do not ask; OnRequest: ask unless filesystem access is unrestricted; Granular: ask unless
> unrestricted, auto-reject when sandbox approval disabled; UnlessTrusted: always ask.

plus **dangerous-command heuristics** (`shell-command/src/command_safety/is_dangerous_command.rs`): forced
`rm` (`-f`/`-rf`, incl. through `sudo`/`env`/nesting), dangerous `git` ops, PowerShell equivalents — these
force a prompt/warning even when a sandbox exists. Model-requested escalation: `sandbox_permissions:
"require_escalated"` + `justification` triggers `Session::request_command_approval`
(`core/src/session/mod.rs:2371`) which emits `EventMsg::ExecApprovalRequest` to the frontend and awaits
`notify_approval` (`mod.rs:2926`) with a `ReviewDecision` (approve once / approve for session / approve
with prefix-rule / deny / abort). Sandboxed commands that fail are retried unsandboxed **only after** user
approval (the classic "escalate on sandbox denial" path; denial detection in `sandboxing/src/denial.rs`).

**Patch safety** is separate: `assess_patch_safety` (`core/src/safety.rs:26`) auto-approves patches whose
writes all fall inside writable roots *when a platform sandbox exists to enforce it*, else asks/rejects;
`UnlessTrusted` always asks.

---

## 6. Linux sandboxing implementation

`get_platform_sandbox` (`sandboxing/src/manager.rs:62`): macOS → Seatbelt (`sandbox-exec` SBPL profiles in
`sandboxing/src/*.sbpl`), Linux → `SandboxType::LinuxSeccomp`, Windows → restricted-token sandbox if
enabled, else `None` (⇒ every command needs approval).

On Linux the command is wrapped as `codex-linux-sandbox --sandbox-policy-cwd … --command-cwd …
--permission-profile <json> [--use-legacy-landlock] [--allow-network-for-proxy] -- <cmd…>`
(`sandboxing/src/landlock.rs:23-60`). **arg0 trick** (`arg0/src/lib.rs:195-289`): the single `codex`
binary is re-exec'd through a temp-dir alias/hard-link named `codex-linux-sandbox`
(`CODEX_LINUX_SANDBOX_ARG0`); `arg0_dispatch` sees the basename and jumps straight into
`codex_linux_sandbox::run_main()` — same trick provides `apply_patch` and `codex-execve-wrapper` aliases
on `PATH`.

Inside the helper (`linux-sandbox/src/`):
- **Filesystem**: **bubblewrap** is now the primary mechanism (`bwrap.rs`) — read-only root with writable
  roots bind-mounted on top, mirroring Seatbelt semantics; protected metadata (`.git`, `.codex`,
  `.agents`) stays read-only; a **bundled bwrap** binary with digest verification is used when the system
  one is unsuitable (`bundled_bwrap.rs`, exit code 8 on digest mismatch), with `/proc`-related fallbacks
  (`run_bwrap_with_proc_fallback`, `linux_run_main.rs:397`).
- **Network**: seccomp BPF filter (crate `seccompiler`) blocking socket syscalls, installed on the current
  thread with `PR_SET_NO_NEW_PRIVS` (`landlock.rs::apply_permission_profile_to_current_thread`); with
  managed network requirements, proxy-only networking uses bwrap's isolated netns + an in-sandbox proxy
  (`proxy_lifecycle.rs`, `proxy_routing.rs`).
- **Landlock** (crate `landlock`) survives only as the opt-in legacy fallback (`--use-legacy-landlock`);
  the file header says: "Filesystem restrictions are enforced by bubblewrap in `linux_run_main`. Landlock
  helpers remain available here as legacy/backup utilities" (`linux-sandbox/src/landlock.rs:1-4`). If
  neither sandbox can be applied the exec fails as a sandbox error rather than running unprotected, and at
  the policy layer a missing platform sandbox downgrades auto-approval to AskUser (`safety.rs:72-88`).

---

## 7. AGENTS.md and config.toml

**AGENTS.md discovery** (`core/src/agents_md.rs`): filenames tried per directory are
`AGENTS.override.md` first, then `AGENTS.md` (`agents_md.rs:41-44`, plus config-provided
`project_doc_fallback_filenames`). Discovery walks **from the project root (nearest `.git` ancestor) down
to the cwd**, collecting every file (`agents_md_paths`, `agents_md.rs:199`), subject to a byte budget
(`project_doc_max_bytes`). A user-level `~/.codex/AGENTS.md` is prepended; multiple docs are joined with
`"\n\n--- project-doc ---\n\n"` (`agents_md.rs:48`). The result is injected as a `<user_instructions>`
user/developer message in the initial turn context (`context/user_instructions.rs`), and the base prompt's
"AGENTS.md spec" section tells the model these are already loaded ("The contents of the AGENTS.md file at
the root of the repo and any directories from the CWD up to the root are included with the developer
message and don't need to be re-read"). Nested/subdirectory AGENTS.md files must be read by the model
itself.

**config.toml** (`~/.codex/config.toml`; struct `Config` in `core/src/config/mod.rs`, TOML layer in
`config/src/config_toml.rs`; docs moved to developers.openai.com/codex/config-reference). Key options:
- `model` (default `gpt-5.5`), `model_reasoning_effort`, `model_reasoning_summary`, `model_verbosity`,
  `model_context_window`, `model_auto_compact_token_limit`.
- `model_provider` (id into `model_providers`), `model_providers.<id>` table: `name`, `base_url`,
  `env_key`, `wire_api = "responses"`, `query_params`, `http_headers`, `env_http_headers`,
  `request_max_retries`, `stream_max_retries`, `stream_idle_timeout_ms`, `requires_openai_auth`,
  `supports_websockets`, `auth` (command-backed bearer token), `aws` (SigV4)
  (`model-provider-info/src/lib.rs:95-150`).
- `approval_policy` (`untrusted` | `on-request` | `never` | granular table), `sandbox_mode`
  (`read-only` | `workspace-write` | `danger-full-access`) with `[sandbox_workspace_write]`
  `writable_roots` / `network_access` / tmpdir exclusions.
- `[mcp_servers.<name>]` — stdio (`command`/`args`/`env`) or HTTP MCP servers, tool
  allow/deny + approval policies; `[profiles.<name>]` — named bundles of
  `model`/`approval_policy`/`sandbox_mode`/`model_provider`/`base_instructions` selected via `--profile`
  (`ConfigProfile`, `config/mod.rs:2510`).
- Also: `base_instructions`, `[features]` flags (unified exec, view_image, request_permissions tool, token
  budget, …), `[hooks]` lifecycle hooks, execpolicy `rules` files, `notify`, `history`, OTEL settings,
  managed/enterprise layering via `requirements.toml` (`ConfigLayerStack`).

---

## 8. Provider-agnosticism (non-OpenAI backends)

Built-in providers (`model-provider-info/src/lib.rs:494-526`): `openai`, `amazon-bedrock`,
`amazon-bedrock-runtime` (SigV4-signed, Mantle OpenAI-compatible endpoint
`https://bedrock-mantle.us-east-1.api.aws/openai/v1`, GPT-5.x model IDs), `ollama`
(`http://localhost:11434/v1`), `lmstudio` (`http://localhost:1234/v1`) — the latter two created by
`create_oss_provider` (env overrides `CODEX_OSS_PORT` / `CODEX_OSS_BASE_URL`) with
`requires_openai_auth: false`. Comment at `lib.rs:503`: "We do not want to be in the business of
adjudicating which third-party providers are bundled … users are encouraged to add to `model_providers` in
config.toml." Any OpenAI-Responses-compatible endpoint therefore works via `base_url` + `env_key`;
`codex-api`'s `Provider` carries only base_url/headers/retry (`to_api_provider`, `lib.rs:289-326`).
Provider capability gates in core: hosted `web_search`, websockets, remote compaction, `service_tier`,
encrypted reasoning passthrough (stripped for non-OpenAI, `client.rs:856-867`) — everything else
(sandboxing, tools, approvals, compaction) is provider-independent. Note the chat-completions wire API
**was removed entirely** (2026), as was the `ollama-chat` provider id; local providers must speak the
Responses API (Ollama ≥ the version that added `/v1/responses`; helper crates `ollama/` and `lmstudio/`
manage local model pulls).

---

## Quick cross-reference

| Concern | Entry point |
|---|---|
| Turn loop | `core/src/session/turn.rs:153` (`run_turn`), `tasks/regular.rs:30` |
| Stream consumption | `core/src/session/turn.rs:2181` (`try_run_sampling_request`) |
| Request build | `core/src/client.rs:845` (`build_responses_request`) |
| SSE decode | `codex-api/src/sse/responses.rs` |
| Tool registry | `core/src/tools/spec_plan.rs` (`build_tool_router`) |
| Shell/unified-exec specs | `core/src/tools/handlers/shell_spec.rs` |
| apply_patch grammar | `core/src/tools/handlers/apply_patch.lark`; crate `apply-patch/` |
| Exec output truncation | `core/src/tools/mod.rs:99`; `utils/output-truncation/` |
| Approval engine | `core/src/exec_policy.rs`, `core/src/tools/sandboxing.rs`, `execpolicy/` |
| Patch safety | `core/src/safety.rs:26` |
| Policies (types) | `protocol/src/protocol.rs:916,1002` |
| Linux sandbox | `linux-sandbox/src/` (bwrap+seccomp), `sandboxing/src/landlock.rs`, `arg0/src/lib.rs` |
| Compaction | `core/src/compact.rs`, `prompts/templates/compact/` |
| Base prompt | `models-manager/prompt.md`; legacy: `core/gpt_5_codex_prompt.md` et al. |
| AGENTS.md | `core/src/agents_md.rs` |
| Providers | `model-provider-info/src/lib.rs` |
| Auth | `login/`, `protocol/src/auth.rs`, `core/src/client.rs` |
