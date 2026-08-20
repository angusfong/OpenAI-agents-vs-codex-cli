# OpenAI Agents SDK vs. Codex CLI

An experiment in two phases:

1. **Validate** that the OpenAI Codex CLI is genuinely rebuildable from open source — by compiling it from source and dissecting its architecture. → [`docs/phase1-feasibility.md`](docs/phase1-feasibility.md)
2. **Rebuild** the Codex CLI experience on the OpenAI **Agents SDK** (Python): `agents-codex`, a terminal coding agent with streaming reasoning traces, approval modes with clarifications, a sandboxed shell, Codex-format file patching, AGENTS.md, sessions, slash commands, exec mode, a Textual TUI, and MCP support. → [`docs/comparison.md`](docs/comparison.md)

Dissection notes from reading the `openai/codex` and `openai/openai-agents-python` sources live in [`docs/notes/`](docs/notes/).

## agents-codex

```bash
uv venv .venv && uv pip install -p .venv -e '.[dev]'

# interactive TUI (needs OPENAI_API_KEY, or --oss / --base-url for local models)
.venv/bin/agents-codex

# non-interactive, approvals off, sandbox on (like `codex exec`)
.venv/bin/agents-codex exec "fix the failing test" --json

# resume the last session
.venv/bin/agents-codex resume --last

# knobs (mirror codex): -m MODEL, -s SANDBOX, -a APPROVAL, -C DIR, -p PROFILE
.venv/bin/agents-codex -s workspace-write -a untrusted
```

Configuration: `~/.agents-codex/config.toml` and per-repo `.agents-codex.toml` (model, sandbox_mode, approval_policy, mcp_servers, profiles) — see `src/agents_codex/config.py`.

Sandboxing on Linux uses bubblewrap (`apt install bubblewrap`) with a pure-ctypes Landlock fallback; without either, commands run unsandboxed and approval prompts tighten accordingly, matching Codex's degradation.

## Tests

19 tests run **without any API key** against a schema-validated mock of the OpenAI Responses API (`tests/mock_responses.py`), covering full coding turns (reasoning stream → sandboxed shell → apply_patch → final answer), approval prompts and rejection feedback, session resume, MCP stdio roundtrip, the subprocess `exec --json` interface, and a headless TUI session:

```bash
OPENAI_API_KEY=mock .venv/bin/python -m pytest tests/
```
