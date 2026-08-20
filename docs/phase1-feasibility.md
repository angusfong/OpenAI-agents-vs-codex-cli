# Phase 1 — Can the OpenAI Codex CLI be rebuilt entirely from open source?

**Verdict: the client — everything you install and run on your machine — is fully open source and fully rebuildable; we proved it by compiling and running it. The service behind it is not: the models, the Responses API backend, the ChatGPT-login auth service, and the hosted model catalog are proprietary. The CLI is provider-agnostic, so the open-source client runs against any Responses-API-compatible endpoint, including local Ollama/LM Studio.**

All findings below are from direct source dissection of `openai/codex` @ `4a942885c` (2026-08-20) and hands-on experiments in a Linux container. Detailed dissection notes: [`docs/notes/codex-core.md`](notes/codex-core.md), [`docs/notes/codex-ux.md`](notes/codex-ux.md).

## Build proof

- Cloned `github.com/openai/codex` (Apache-2.0) and compiled the Rust workspace with stock `cargo`: `cargo build -p codex-cli --profile dev-small` → **1,011 crates, exit 0**, on 4 CPUs / 15 GB RAM.
- The resulting binary runs: `codex --version` → `codex-cli 0.0.0`, full subcommand surface present (tui, exec, login, mcp, app-server, sandbox, resume, apply, …).
- **Its sandbox works from source too.** `codex sandbox -- bash -c …` initially panicked ("bubblewrap is unavailable"); after `apt-get install bubblewrap` it fully enforced policy in our container:
  - `touch /root/x` → `Read-only file system`
  - `curl https://example.com` → network blocked
  - workspace writes allowed.

No proprietary blob, key, or server call was needed for any of the above.

## What is open (the entire client)

| Component | Where | Notes |
|---|---|---|
| Agent loop / engine | `codex-rs/core` | Turn loop, tool dispatch (`FuturesOrdered`, parallel/serial via RwLock), retries, auto-compaction |
| System prompts | `models-manager/prompt.md` + `core/*.md` | The full base instructions ship in the repo |
| TUI | `codex-rs/tui` (ratatui) | Transcript-into-scrollback design, approval overlays, ~60 slash commands |
| apply_patch | `apply-patch` crate + Lark grammar | The complete V4A patch format and fuzzy applier |
| Approval engine | `core/exec_policy.rs`, `execpolicy` | untrusted / on-request / granular / never; prefix rules; dangerous-command heuristics |
| Sandboxing | `linux-sandbox` (bwrap + seccomp; legacy Landlock), macOS Seatbelt profiles | Bundled-bwrap logic included |
| Sessions | `history` crate | JSONL rollouts + SQLite index, resume/fork |
| MCP client & server | `codex-mcp`, `mcp-server` | stdio + streamable HTTP, OAuth creds |
| Wire client | `codex-api` | Responses API SSE/WebSocket client |
| Auth client | `login` crate | PKCE OAuth flow, auth.json handling |

## What is NOT open (the service)

1. **The models** (`gpt-5.5` default, `gpt-5.1-codex-max`, …) — weights are proprietary; the CLI is a shell around a hosted brain.
2. **The Responses API backend** — `/responses`, `/responses/compact` (remote compaction), the WebSocket transport, hosted `web_search`: server-side implementations.
3. **ChatGPT-login backend** — `chatgpt.com/backend-api/codex` and the OAuth app (`auth.openai.com`, client id baked in). Reproducible in form but the service is theirs; API-key auth avoids it.
4. **The server-fetched model catalog** (`models-manager`) — prompt/parameter presets served by OpenAI; the repo ships working fallbacks.
5. **Cloud tasks / Codex cloud** — explicitly out of our scope.

## Provider-agnosticism (measured, not marketed)

The client hardcodes nothing about OpenAI's servers except defaults: `model_providers` config accepts any `base_url` + `env_key`. Built-ins include `ollama` (`localhost:11434/v1`) and `lmstudio`; Bedrock with SigV4 signing is in-tree. Two caveats from source: the chat-completions wire API **was removed in 2026** (providers must speak the Responses protocol), and capability gates (hosted web_search, websockets, remote compaction, encrypted-reasoning passthrough) disable gracefully for third-party providers.

## Answer to the Phase-1 question

"Can we completely rebuild the OpenAI coding agent from what is open source?" — split it in two:

- **The CLI experience** (loop, tools, sandbox, TUI, approvals, sessions): yes, demonstrably — the reference implementation itself compiles and enforces its sandbox from pure open source, and every behavioral detail is inspectable in the repo. A from-scratch rebuild is an engineering exercise, not a reverse-engineering one.
- **The agent as a product**: no — the intelligence is a hosted proprietary model. Any rebuild either calls OpenAI's API (key required) or substitutes an open-weight model via the same open client, with model quality as the only differentiator (explicitly out of scope for this project).

Phase 2 ([`docs/comparison.md`](comparison.md)) tests the corollary: whether the **OpenAI Agents SDK** provides enough of the same machinery to reproduce the CLI's behavior in Python — it does; see the comparison for what came free, what we hand-built, and where the gaps are.
