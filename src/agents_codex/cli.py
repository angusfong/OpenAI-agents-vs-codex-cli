"""agents-codex CLI multitool, mirroring the codex binary's surface:
default = interactive TUI; `exec` = non-interactive; `resume`; `sessions`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from . import __version__
from .config import load_config
from .sessions import latest_thread_id, list_threads


def _add_common(parser: argparse.ArgumentParser) -> None:
    # default=SUPPRESS so subcommand parsers don't clobber values already
    # parsed by the main parser into the shared namespace.
    S = argparse.SUPPRESS
    parser.add_argument("-m", "--model", default=S, help="model slug (default from config)")
    parser.add_argument(
        "-s", "--sandbox", default=S,
        choices=["read-only", "workspace-write", "danger-full-access"],
        dest="sandbox_mode",
    )
    parser.add_argument(
        "-a", "--ask-for-approval", default=S,
        choices=["untrusted", "on-request", "never"],
        dest="approval_policy",
    )
    parser.add_argument("-C", "--cd", dest="cwd", default=S,
                        help="run with this working directory")
    parser.add_argument("-p", "--profile", default=S, help="config profile to apply")
    parser.add_argument("--base-url", default=S,
                        help="custom Responses-API-compatible endpoint")
    parser.add_argument(
        "--oss", action="store_true", default=S,
        help="shortcut for --base-url http://localhost:11434/v1 (Ollama)",
    )
    parser.add_argument("--reasoning-effort", dest="reasoning_effort", default=S,
                        choices=["none", "low", "medium", "high", "xhigh"])


def _config_from(ns: argparse.Namespace):
    get = lambda name: getattr(ns, name, None)
    oss = get("oss") is True
    overrides = {
        "model": get("model"),
        "sandbox_mode": get("sandbox_mode"),
        "approval_policy": get("approval_policy"),
        "base_url": get("base_url") or ("http://localhost:11434/v1" if oss else None),
        "reasoning_effort": get("reasoning_effort"),
    }
    cwd = Path(get("cwd")).expanduser().resolve() if get("cwd") else None
    return load_config(cwd=cwd, profile=get("profile"), overrides=overrides)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agents-codex",
        description="A Codex-CLI-like coding agent built on the OpenAI Agents SDK.",
    )
    parser.add_argument("--version", action="version", version=f"agents-codex {__version__}")
    _add_common(parser)
    parser.add_argument("prompt", nargs="?", help="initial prompt for the interactive TUI")
    sub = parser.add_subparsers(dest="command")

    p_exec = sub.add_parser("exec", help="run non-interactively (approvals default to never)")
    _add_common(p_exec)
    p_exec.add_argument("prompt", help="the task to run")
    p_exec.add_argument("--json", action="store_true", help="emit a JSONL event stream")
    p_exec.add_argument("--thread", default="", help="continue an existing thread id")

    p_resume = sub.add_parser("resume", help="resume a previous interactive session")
    _add_common(p_resume)
    p_resume.add_argument("thread_id", nargs="?", help="thread to resume")
    p_resume.add_argument("--last", action="store_true", help="resume the most recent thread")

    sub.add_parser("sessions", help="list stored sessions")
    return parser


def main(argv: list[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)

    if ns.command == "sessions":
        for info in list_threads():
            print(f"{info.thread_id}  items={info.items}  {info.preview}")
        return 0

    cfg = _config_from(ns)
    if cfg.api_key is None and not cfg.base_url:
        print(
            f"error: no API key found (set {cfg.api_key_env} or configure base_url)",
            file=sys.stderr,
        )
        return 2

    if ns.command == "exec":
        from .exec_mode import run_exec

        return asyncio.run(
            run_exec(cfg, ns.prompt, as_json=ns.json, thread_id=ns.thread)
        )

    thread_id = ""
    if ns.command == "resume":
        thread_id = ns.thread_id or (latest_thread_id() if ns.last else "")
        if not thread_id:
            print("error: no thread to resume (try --last)", file=sys.stderr)
            return 2

    from .tui.app import run_tui

    return run_tui(cfg, thread_id=thread_id, initial_prompt=getattr(ns, "prompt", None))


if __name__ == "__main__":
    sys.exit(main())
