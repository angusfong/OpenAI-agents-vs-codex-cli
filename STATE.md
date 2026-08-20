# Overnight Run State

Project: Rebuild the OpenAI Codex CLI experience — Phase 1 validate open-source rebuildability, Phase 2 rebuild on the Python Agents SDK.
Branch: `claude/openai-codex-cli-rebuild-8answc`. This file is the resume point: on any wake-up, read it, check `git log`, and continue the first unchecked item.

## Scope decisions (user-confirmed 2026-08-20)
- Phase 2 in **Python** (openai-agents SDK), full parity bar: core loop + streaming reasoning traces, shell + apply_patch + plan tools, approval modes with clarifications, AGENTS.md, session resume, slash commands, exec mode, **Textual full-screen TUI**, **MCP client**, **Linux Landlock/seccomp sandboxing** (graceful fallback + documentation if kernel blocks it).
- User will provide `OPENAI_API_KEY` (not yet present). Build everything against a mock Responses API harness; run live e2e as soon as the key appears (check env + ask nothing — just probe `printenv OPENAI_API_KEY`).
- Phase 1 = build + dissect: compile codex-rs from source, evidence-based feasibility report.
- Deliverables: code in this repo, `docs/phase1-feasibility.md`, `docs/comparison.md`, artifacts published at the end.

## Progress
- [x] Clone openai/codex and openai/openai-agents-python to /workspace/openai/ (re-clone if container was recycled; anonymous git reads work through the proxy)
- [ ] Phase 1: compile codex-cli (`cargo build -p codex-cli --profile dev-small` in /workspace/openai/codex/codex-rs; log in scratchpad codex-build.log)
- [ ] Phase 1: dissection notes in docs/notes/{codex-core,codex-ux,agents-sdk}.md (3 background agents writing these)
- [ ] Phase 1: docs/phase1-feasibility.md
- [ ] Phase 2: scaffold `agents_codex` Python package
- [ ] Phase 2: tools (shell, apply_patch, update_plan) + tests
- [ ] Phase 2: approval modes + sandboxing (landlock/seccomp attempt, fallback documented)
- [ ] Phase 2: reasoning-trace streaming, sessions/resume, AGENTS.md, slash commands, exec mode
- [ ] Phase 2: Textual TUI + MCP client
- [ ] Phase 2: mock Responses API harness + e2e tests (live tests pending key)
- [ ] docs/comparison.md + final artifacts + final push

## Rules for resumed sessions
- Commit + push after each completed checklist item (small, descriptive commits).
- If usage credits ran out: the send_later check-in that woke you was queued during the pause; just resume from the first unchecked item.
- Keep a running log of surprises/blockers in docs/notes/journal.md.
