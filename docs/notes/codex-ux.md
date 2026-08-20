# Codex CLI — User-Facing Layer (codex-rs)

Engineering reference for the UX surface of OpenAI Codex CLI, from source at
`/workspace/openai/codex` (commit `4a942885c8b4` , 2026-08-20). Paths below are relative to
`codex-rs/` unless noted.

---

## 1. TUI (`tui/`)

### Framework & architecture
- **ratatui 0.30** + **crossterm** (bracketed-paste, event-stream) — `tui/Cargo.toml`,
  workspace `Cargo.toml:384`.
- Two render surfaces:
  - **Finalized history is written into the terminal's own scrollback** via raw escape
    sequences, not a ratatui viewport — `tui/src/insert_history.rs` ("Codex uses the terminal
    scrollback itself for finalized chat history"). This gives native scroll/copy.
  - The **live region** (active streaming cell + status row + bottom pane) is a ratatui area at
    the bottom, redrawn each frame — `tui/src/chatwidget.rs`, `tui/src/tui.rs`.
- Alt-screen optional: `--no-alt-screen` runs inline preserving scrollback (`tui/src/cli.rs:75`).
- A full-history **transcript overlay** opens with `Ctrl+T` (pager-style, with live tail) —
  `tui/src/pager_overlay.rs`.
- `ChatWidget` is split into ~70 submodules under `tui/src/chatwidget/` (streaming, protocol,
  slash_dispatch, tokens, approvals via `permission_popups`, etc.). Bottom pane widgets live in
  `tui/src/bottom_pane/` (composer, popups, approval overlay, footer).

### Layout
- Transcript = terminal scrollback (committed `HistoryCell`s from `tui/src/history_cell/*`:
  messages, exec, patches, mcp, plans, notices, session header...).
- **Status indicator row**: animated "Working" header with shimmer, elapsed time, `Esc to
  interrupt` hint — `tui/src/status_indicator_widget.rs`, shimmer band in `tui/src/shimmer.rs`
  (time-based sweep, truecolor gradient w/ 256/16-color fallbacks; disabled under reduced
  motion, `tui/src/motion.rs`).
- **Composer** (`bottom_pane/chat_composer.rs`, `textarea.rs`): multiline editor, optional Vim
  mode (`/vim`), paste-burst handling (`paste_burst.rs`), image paste, `@` file-search popup
  (`file_search_popup.rs`), `/` command popup (`command_popup.rs`), custom prompts, history
  recall (`chat_composer_history.rs`).
- **Footer** (`bottom_pane/footer.rs`): key hints, queued messages, and context gauge rendered
  as `NN% context left` (`context_window_line`, footer.rs:999).
- Status line and terminal title are user-configurable via `/statusline` and `/title`
  (defaults `["model-with-reasoning", "current-dir"]`, chatwidget.rs:498).

### Streaming output & reasoning
- Assistant answer deltas stream through `tui/src/streaming/` (controller.rs, chunking.rs) —
  markdown is committed to history in whole lines ("commit-tick" cadence, code-fence and
  table holdback logic) and rendered via `tui/src/markdown_render.rs` with syntax highlighting.
- **Reasoning is NOT streamed into the transcript.** `on_agent_reasoning_delta`
  (`chatwidget/streaming.rs:229`) accumulates deltas in `reasoning_buffer` and extracts the
  **first bold span** of the summary as a header; that header replaces the "Working" shimmer
  text in the status row. So thinking appears as an animated one-line status ("collapsed"),
  not full text.
- On block end (`on_agent_reasoning_final`, streaming.rs:273) the full summary parts become a
  `new_reasoning_summary_block` history cell (`history_cell/messages.rs:629`) that is
  **transcript-only** (shown in the Ctrl+T overlay, hidden from main scrollback) unless a
  title can be shown. `ReasoningSummaryPartAdded` events insert section breaks
  (`chatwidget/protocol.rs:90`).
- Raw chain-of-thought deltas render only when `show_raw_agent_reasoning` config is set
  (`chatwidget/protocol.rs:86`).

### Approval dialog (`bottom_pane/approval_overlay.rs`)
Request kinds: `Exec`, `Permissions`, `ApplyPatch`, `McpElicitation` (enum `ApprovalRequest`).
Header shows optional Thread/Environment labels, model-supplied **Reason**, the requested
permission rule, and the command (`$ cmd`, bash-highlighted). Options are a selection list
(number keys + shortcut bindings), e.g. for exec:
1. `Yes, proceed` (or `Yes, just this once` for network approvals)
2. `Yes, and don't ask again for commands that start with `<prefix>`` (prefix-rule amendment)
3. `Yes, and don't ask again for this command in this session` / "allow these permissions for
   this session" / "allow this host for this conversation" (approve-for-session)
4. Optional persistent network-policy option: `Yes, and allow this host in the future` /
   `No, and block this host in the future`
5. `No, continue without running it`
6. `No, and tell Codex what to do differently` (Esc) — feedback goes back to the model.
File-change requests: `Yes, proceed` / `Yes, and don't ask again for these files` / `No, and
tell Codex...` (approval_overlay.rs:996). Permission grants: for-turn, for-turn-with-strict-
auto-review, for-session, deny (`PermissionsDecision`, :774). Patch header w/ diff summary in
`bottom_pane/apply_patch_header.rs`.

### Clarification flow
- Model-initiated questions surface as a `request_user_input` widget
  (`bottom_pane/request_user_input/`): question text + selectable options + optional free-text
  notes area; also a history cell (`history_cell/request_user_input.rs`). MCP servers can
  elicit input via `bottom_pane/mcp_server_elicitation.rs`.

### Diff rendering (`tui/src/diff_render.rs`)
- Unified diffs with right-aligned line numbers, `+`/`-`/` ` gutter, syntect syntax
  highlighting per hunk (parser state preserved within a hunk). Theme-aware backgrounds:
  dark terminals `#212922`/`#3C170F` tints, light terminals GitHub-style pastels; fixed
  palettes for truecolor/256/16-color. Per-file summary rows with `+added -removed` counts
  (`create_diff_summary`). Used for apply_patch cells (`history_cell/patches.rs`) and `/diff`.

### Token usage display
- `tui/src/token_usage.rs`: input/cached_input/output/reasoning_output/total; context-left %
  computed against `context_window` minus a 12k `BASELINE_TOKENS`.
- Footer shows `NN% context left`; `/status` card (`tui/src/status/card.rs`) shows model +
  reasoning effort, directory, permissions summary (approval policy × sandbox), account/plan,
  session id, token usage spans and **rate-limit rows** (5h/weekly usage bars,
  `status/rate_limits.rs`); `/usage` opens account usage view.

## 2. Slash commands (`tui/src/slash_command.rs`)
Enum order = popup order. Descriptions from `description()`:
`/model` (model+reasoning effort picker), `/ide`, `/permissions` (what Codex may do), `/keymap`,
`/vim`, `/setup-default-sandbox`, `/sandbox-add-read-dir <path>` (Windows), `/experimental`,
`/approve` (retry an auto-review denial), `/memories`, `/skills`, `/import` (migrate setup from
Claude Code), `/hooks`, `/review` (code review), `/rename`, `/new`, `/archive`, `/delete`,
`/resume`, `/fork`, `/app` (hand off to Desktop app), `/init` (create AGENTS.md), `/compact`
(summarize to save context), `/plan` (Plan mode), `/goal`, `/agents`, `/subagents`, `/side` &
`/btw` (ephemeral side-conversation fork), `/copy`, `/export` (markdown export), `/raw`
(copy-friendly scrollback), `/diff` (git diff incl. untracked), `/mention` (mention a file),
`/status`, `/cd`, `/pwd`|`/cwd`, `/usage`, `/debug-config`, `/title`, `/statusline`, `/theme`
(syntax theme), `/pets`, `/mcp [verbose]`, `/apps`, `/plugins`, `/logout`, `/quit`|`/exit`,
`/feedback`, `/ps` (background terminals), `/stop`|`/clean`, `/clear`, `/personality`,
plus debug-only `/rollout` (print rollout path) and `/test-approval`. Availability is gated by
`available_during_task()` / `available_in_side_conversation()`.

## 3. Session persistence (`rollout/`, `history/`)
- **Rollout files**: `~/.codex/sessions/YYYY/MM/DD/rollout-YYYY-MM-DDThh-mm-ss-<thread_id>.jsonl`
  (`rollout/src/list.rs:426`, `rollout_file_name.rs`; reverted threads append `_<rollout_id>`).
  Archived sessions move under `~/.codex/archived_sessions` (`rollout/src/lib.rs:67-68`).
- **JSONL format**: each line is `RolloutLine { timestamp, ordinal?, ...RolloutItem }`
  (`history/src/lib.rs:201`); `RolloutItem` = `SessionMeta | ResponseItem | Compacted |
  TurnContext | WorldState | SecurityRiskScore | EventMsg | InterAgentCommunication(…)`
  (`history/src/lib.rs:95`). First line is session metadata (id, cwd, instructions, git info).
- Writing via async `RolloutRecorder` (`rollout/src/recorder.rs`); a SQLite **state DB /
  session index** accelerates listing/search (`rollout/src/state_db.rs`, `session_index.rs`);
  `codex migrate-rollouts` converts legacy files to paginated thread history
  (`cli/src/migrate_rollouts.rs`). Compressed rollouts supported (`compression.rs`).
- **Resume**: `codex resume [SESSION_ID|name] [--last] [--all] [--include-non-interactive]` —
  picker by default, filtered to cwd (`cli/src/main.rs:344`, picker in
  `tui/src/resume_picker.rs` / `session_resume.rs`). **Fork**: `codex fork` clones a session
  into a new thread id. In-TUI `/resume`, `/fork`, `/archive`, `/delete`, `/rename` mirror
  these; `codex archive|unarchive|delete <session>` from the CLI.
- Cross-session **message history** (composer up-arrow) in `~/.codex/history.jsonl`
  (`message-history/` crate).

## 4. Non-interactive: `codex exec` (`exec/`)
- Usage: `codex exec [OPTIONS] [PROMPT]` (prompt from arg or stdin, `-` = stdin;
  piped stdin is appended as a `<stdin>` block) — `exec/src/cli.rs`.
- Flags: shared set (`-m/--model`, `--oss`, `--local-provider lmstudio|ollama`, `-p/--profile`,
  `-s/--sandbox read-only|workspace-write|danger-full-access`, `--auto-review`,
  `--dangerously-bypass-approvals-and-sandbox`, `-C/--cd`, `--add-dir`, `-i/--image`; from
  `utils/cli/src/shared_options.rs`) plus `--skip-git-repo-check`, `--ephemeral` (no rollout
  persisted), `--ignore-user-config`, `--ignore-rules`, `--output-schema FILE` (JSON Schema for
  final answer), `--color`, `--json`, `-o/--output-last-message FILE`.
- **Approvals headlessly**: approval policy defaults to `AskForApproval::Never`
  (`exec/src/lib.rs:411`) — failures return to the model; safety comes from the sandbox mode.
- **Output modes**: human-readable (`event_processor_with_human_output.rs`, prints
  `OpenAI Codex v… --------` header, colored events) or `--json` JSONL stream
  (`event_processor_with_jsonl_output.rs`) emitting `ThreadEvent`s: `thread.started`,
  `turn.started/completed/failed`, `item.started/updated/completed`, `error`; items are typed
  `ThreadItemDetails` (agent_message, reasoning, command_execution, file_change, mcp_tool_call,
  web_search, todo_list, error, collab…) — `exec/src/exec_events.rs`.
- Subcommands: `codex exec resume <id>|--last [--all] [prompt]`, `codex exec fork <id>`,
  `codex exec review` (non-interactive code review) — `exec/src/cli.rs` `enum Command`.

## 5. CLI surface (`cli/src/main.rs`, clap `Subcommand` at :133)
No subcommand → interactive TUI (`codex [OPTIONS] [PROMPT]`). Subcommands:
- `exec` (alias `e`) — headless run; `review` — non-interactive code review.
- `login [status] [--with-api-key] [--with-access-token] [--device-auth]`, `logout`.
- `mcp list|get|add|remove|login|logout` (manage external MCP servers, `cli/src/mcp_cmd.rs`);
  `mcp-server` — run Codex **as** an MCP server on stdio.
- `app-server` [experimental] — JSON-RPC app server (`--listen stdio://|unix://|ws://IP:PORT`);
  backs IDE/desktop integrations; `remote-control`, `app-server-daemon` variants.
- `agents` — browse sessions on the shared app-server daemon.
- `resume`, `fork`, `queue` (enqueue message to a session), `archive`, `unarchive`, `delete`,
  `migrate-rollouts`.
- `apply` (alias `a`) `<task_id>` — `git apply` the diff from a Codex Cloud task
  (`chatgpt/src/apply_command.rs`); `cloud` [experimental] — browse Cloud tasks TUI.
- `sandbox <cmd>` — run a command under the platform sandbox (Seatbelt on macOS, Landlock on
  Linux, Windows sandbox; `debug_sandbox.rs`), plus hidden `debug` tools (models catalog,
  app-server send, prompt-input dump, trace-reduce), hidden `execpolicy check`.
- `completion [shell]`, `update` (self-update), `doctor` (install/config/auth health),
  `features` (feature flags), `plugin`, hidden `responses-api-proxy`, `stdio-to-uds`,
  `exec-server` [experimental].
- Global: `-c key=value` config overrides (`CliConfigOverrides`), feature toggles, and the full
  TUI flag set (`-a/--ask-for-approval on-request|never`, `--search`, `--no-alt-screen`, …).

## 6. MCP
- **Client**: config `[mcp_servers.<name>]` in `~/.codex/config.toml`
  (`config/src/mcp_types.rs:514`): `stdio` (`command`, `args`, `env`, `env_vars`, `cwd`) or
  `streamable_http` (`url`, `bearer_token_env_var`, `http_headers`, `env_http_headers`,
  `http_headers_helper`). Runtime uses the official `rmcp` SDK via `rmcp-client/`
  (stdio launcher, streamable-http with retry, elicitation passthrough, OAuth). MCP OAuth
  creds stored in OS keyring, falling back to `$CODEX_HOME/.credentials.json`
  (`rmcp-client/src/oauth.rs`). `codex mcp add|login…` edits config / runs OAuth
  (`cli/src/mcp_cmd.rs`); `/mcp` lists tools in the TUI.
- **Server**: `codex mcp-server` (`mcp-server/`) exposes tools `codex` (start a session with
  `CodexToolCallParam`) and `codex-reply` (continue by session id)
  (`message_processor.rs:357`). Approvals are forwarded to the MCP host as
  `elicitation/create` requests tagged `codex_elicitation: "exec-approval"` /
  patch-approval (`mcp-server/src/exec_approval.rs`, `patch_approval.rs`).

## 7. Auth (`login/`)
- **ChatGPT OAuth (default)**: browser flow against issuer `https://auth.openai.com`
  (`login/src/server.rs:59`), PKCE (`pkce.rs`), local callback server on **port 1455**
  (`server.rs:60`, redirect `http://localhost:1455/auth/callback`), client id
  `app_EMoamEEZ73f0CkXaXp7hrann` (`login/src/auth/manager.rs:1678`). Headless alternative:
  `codex login --device-auth` (`device_code_auth.rs`).
- **API key**: `codex login --with-api-key` (reads stdin), or env. `--api-key` flag is
  deprecated/hidden.
- **`$CODEX_HOME/auth.json`** (`login/src/auth/storage.rs:38`): `{ OPENAI_API_KEY?,
  tokens?: { id_token (JWT→email/plan claims), access_token (JWT), refresh_token,
  account_id? }, last_refresh }`. Optional OS-keyring storage backend.
- **Endpoints**: ChatGPT-auth modes hit `https://chatgpt.com/backend-api/codex` (Responses
  wire API); API-key auth hits `https://api.openai.com/v1`
  (`model-provider-info/src/lib.rs:39,289-303`). `--oss` targets local
  Ollama (11434) / LM Studio via a Responses-compatible shim.

## 8. Update & packaging
- **npm**: `@openai/codex` (`codex-cli/package.json`) is a thin Node launcher
  (`codex-cli/bin/codex.js`) that spawns a platform binary from optional deps
  `@openai/codex-{linux-x64,linux-arm64,darwin-x64,darwin-arm64,win32-x64,win32-arm64}`
  (musl static builds on Linux). Built by `codex-cli/scripts/build_npm_package.py`.
- **brew**: cask `codex` (update check hits `https://formulae.brew.sh/api/cask/codex.json`);
  GitHub releases at `openai/codex` (`tui/src/updates.rs:61-62`). `codex update` /
  update prompt picks npm/pnpm/brew/direct action per detected install method
  (`tui/src/update_action.rs`).
- Repo is self-contained for building the CLI (`cargo build -p codex-cli`; Bazel files also
  present). Not in-repo: the OpenAI backend services (chatgpt.com/backend-api/codex, Cloud
  tasks API, auth.openai.com OAuth app) and the published platform npm packages — you need an
  OpenAI API key or ChatGPT account for a working client; only `--oss` against a local
  Ollama/LM Studio works fully offline.
