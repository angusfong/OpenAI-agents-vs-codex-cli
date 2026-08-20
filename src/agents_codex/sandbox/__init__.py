"""Sandbox layer: wraps shell commands with OS-level restrictions.

Mirrors modern Codex CLI on Linux: bubblewrap (bwrap) is the primary
backend — read-only root, writable workspace binds, network namespace
unshared — with Landlock (via our landlock_exec helper) as the legacy
fallback, and graceful unsandboxed degradation when neither is usable
(surfaced in the tool result, like Codex's "sandbox unavailable").
"""

from __future__ import annotations

import functools
import shutil
import subprocess
import sys

from ..config import Config

_PROBE_TIMEOUT = 10


@functools.lru_cache(maxsize=1)
def bwrap_available() -> bool:
    if not shutil.which("bwrap"):
        return False
    try:
        proc = subprocess.run(
            ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--unshare-net",
             "--die-with-parent", "--", "true"],
            capture_output=True,
            timeout=_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


@functools.lru_cache(maxsize=1)
def landlock_available() -> bool:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "agents_codex.sandbox.landlock_exec", "--probe"],
            capture_output=True,
            timeout=_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _bwrap_argv(command: str, cfg: Config) -> list[str]:
    argv = [
        "bwrap",
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--unshare-net",
        "--die-with-parent",
    ]
    if cfg.sandbox_mode == "workspace-write":
        cwd = str(cfg.cwd.resolve())
        argv += ["--bind", cwd, cwd, "--bind", "/tmp", "/tmp"]
    else:  # read-only: ephemeral tmpfs so mktemp-style scripts still run
        argv += ["--tmpfs", "/tmp"]
    argv += ["--", "bash", "-lc", command]
    return argv


def wrap_command(command: str, cfg: Config) -> tuple[list[str], str | None]:
    """Return (argv, sandbox_note). sandbox_note is None when unsandboxed."""
    if cfg.sandbox_mode == "danger-full-access" or cfg.disable_sandbox_hardening:
        return (["bash", "-lc", command], None)
    if bwrap_available():
        return (_bwrap_argv(command, cfg), f"bwrap:{cfg.sandbox_mode}")
    if landlock_available():
        argv = [
            sys.executable, "-m", "agents_codex.sandbox.landlock_exec",
            "--mode", cfg.sandbox_mode, "--writable", str(cfg.cwd),
            "--", "bash", "-lc", command,
        ]
        return (argv, f"landlock:{cfg.sandbox_mode}")
    return (["bash", "-lc", command], None)
