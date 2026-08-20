# Phase 2 — Codex CLI vs. `agents-codex` (a rebuild on the OpenAI Agents SDK)

**Verdict: yes — the Python Agents SDK (v0.22.0) can be wired into a coding agent that reproduces the Codex CLI's observable behavior: streaming reasoning traces, graded approvals and clarifications, sandboxed local shell, Codex-format file patching, AGENTS.md, sessions/resume, slash commands, exec mode, and MCP. The SDK supplies the loop and the tool/approval machinery; roughly everything user-visible had to be hand-built. Two real gaps: sandboxing (SDK offers none for its local ShellTool) and Codex's server-assisted features (remote compaction, hosted web_search under ChatGPT auth).**

The rebuild lives in `src/agents_codex/` (~1,600 lines of Python) with 19 passing tests that run without any API key against a schema-validated mock of the Responses API (`tests/mock_responses.py`). Model quality was explicitly out of scope; this compares *wiring*, not brains.

## Feature-by-feature

| Behavior | Codex CLI (Rust) | agents-codex | SDK contribution |
|---|---|---|---|
| Agent loop (sample → tools → repeat) | `core/session/turn.rs`, no turn cap | `Runner.run_streamed(max_turns=None)` | **Free** |
| Reasoning traces, streamed | Responses `reasoning.summary` deltas → status header + transcript cells | Same events via `raw_response_event` passthrough; rendered dim-italic in TUI/exec | **Free** (rendering ours) |
| Reasoning continuity across tool calls | Replays `reasoning.encrypted_content` (store:false) | SDK sessions replay reasoning items server-normally | **Free** |
| Local shell tool | `shell_command`/unified exec, PTY sessions, middle truncation | `ShellTool` + our executor: timeouts, per-stream truncation, sandbox wrapping | Frame free, **executor ours** |
| File edits | `apply_patch` freeform tool, Lark grammar, fuzzy hunks | `ApplyPatchTool` + SDK's V4A `apply_diff` parser + our jailed `WorkspaceEditor` | Parser **free**, editor ours |
| Plan tool | `update_plan` | `@function_tool update_plan` + `/plan` view | Trivial either way |
| Approval modes | untrusted / on-request / granular / never; prefix rules; dangerous-cmd heuristics | Same modes (minus granular) in `approval.py`; SDK `needs_approval` + interruption/resume state does the plumbing | Mechanism **free**, policy ours |
| Clarifications ("No, and tell the agent…") | Approval overlay option feeds guidance back | `ApprovalAnswer.feedback` → `state.reject(rejection_message=…)`; model course-corrects (tested) | **Free** |
| Sandboxing | bwrap + seccomp (primary), Landlock legacy, Seatbelt on macOS | Our bwrap wrapper (verified: FS + network blocked) + ctypes Landlock fallback | **None from SDK** — biggest gap |
| AGENTS.md | override files, root→cwd chain, `<user_instructions>` | Same discovery/merge in `agents_md.py` | None (ours) |
| Environment context | `<environment_context>` block per session | Same | None (ours) |
| Sessions / resume | JSONL rollouts + SQLite index, resume/fork pickers | `SQLiteSession` + thread registry, `resume --last`, `sessions` | Storage **free**, UX ours |
| TUI | ratatui; scrollback-injection design; ~60 slash commands | Textual full-screen app; transcript, approval modal, 10 slash commands, token meter | None (ours) |
| exec mode | `codex exec`, approvals=never, `--json` event stream | `agents-codex exec [--json]`, same defaults, JSONL events | Runner free, mode ours |
| MCP client | stdio + HTTP, approvals, OAuth keyring | `MCPServerStdio/StreamableHttp` from config (stdio roundtrip tested) | **Free** |
| Custom / local providers | any Responses `base_url`; `--oss` Ollama | Same (`--oss`, `base_url`, `wire_api=chat` extra) | **Free** (`MultiProvider`, chatcmpl adapter) |
| Interrupt mid-turn | Ctrl-C abort, keeps partial transcript | Ctrl-C cancels worker (`RunResultStreaming.cancel`) | Mostly free |
| Auto-compaction | mid-turn local + remote `/responses/compact` | **Not implemented** (SDK has compaction sessions + server `context_management` we didn't wire) | Available, unwired |
| ChatGPT-login auth | OAuth PKCE → chatgpt.com backend | **Not implemented** (API key / base_url only) | None |
| Hosted web_search, cloud tasks | Provider-gated | Out of scope | — |

## What the experiment showed

1. **The SDK's newest primitives map 1:1 onto Codex's tool surface.** `ShellTool`, `ApplyPatchTool`, and the V4A diff parser exist precisely because Codex normalized those tool types into the Responses API; the SDK exposes the same wire types (`shell_call`, `apply_patch_call`). Older SDK writeups (function-tools-only, JS-only approvals) are obsolete.
2. **Human-in-the-loop is the load-bearing feature.** `needs_approval` callbacks + `result.interruptions` + serializable `RunState` gave us Codex's whole approve/reject/steer loop — including "reject with guidance," which our test proves changes the model's next action.
3. **Reasoning traces require zero SDK work.** `ModelSettings(reasoning=Reasoning(effort, summary))` plus the raw-event stream delivers the exact deltas Codex renders; the chat-completions adapter even synthesizes them for non-OpenAI providers.
4. **Everything the user actually sees is yours to build.** TUI, approval UX, policy classification, sandbox, AGENTS.md, sessions UX, slash commands — the SDK is an engine, not a product. Our tally: engine ~free, product ~100% hand-built.
5. **Sandboxing is the one hard gap.** The SDK's local ShellTool executes whatever you give it; its `agents.sandbox` subsystem is a separate manifest/Docker-style model, not an equivalent of Codex's transparent bwrap wrap. We closed the gap with our own bubblewrap wrapper (verified blocking writes + network in-container) and a ctypes Landlock fallback (kernel refused it here — ENOSYS — matching Codex's own degradation path).

## Honest deltas vs. the real thing

- No PTY/interactive command sessions (Codex's unified exec `write_stdin`); one-shot commands only.
- No auto-compaction wired (context will eventually overflow on very long threads).
- Approval "granular" mode, execpolicy rule *files*, `.git`-metadata write protection, bundled-bwrap digest verification: not replicated.
- TUI is functional, not pixel-Codex: no scrollback injection, transcript overlay, or 60-command surface.
- Live-model behavior (actual `gpt-5.x-codex` traces) pending an `OPENAI_API_KEY`; all behavior above is proven against a schema-validated mock, so the wiring risk is retired.

## Bottom line

If you want a Codex-like CLI of your own, the Agents SDK is a legitimate chassis: you inherit the agent loop, streaming reasoning, approvals, patch format, sessions, and MCP, and you spend your effort where Codex spent theirs — policy, sandboxing, and terminal UX. What you cannot rebuild from either codebase is the hosted model and OpenAI's server-side features; those you rent (API key) or replace (open-weight models via the same wiring).
