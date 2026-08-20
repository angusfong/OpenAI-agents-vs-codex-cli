"""Environment context block injected into the first user turn, like Codex CLI's
<environment_context> element (cwd, platform, sandbox/approval posture, git state).
"""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path

from .config import Config


def _git(cwd: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def build_environment_context(cfg: Config) -> str:
    lines = [
        "<environment_context>",
        f"  <cwd>{cfg.cwd}</cwd>",
        f"  <approval_policy>{cfg.approval_policy}</approval_policy>",
        f"  <sandbox_mode>{cfg.sandbox_mode}</sandbox_mode>",
        f"  <network_access>{'restricted' if cfg.sandbox_mode == 'read-only' else 'enabled'}</network_access>",
        f"  <platform>{platform.system().lower()} ({platform.machine()})</platform>",
    ]
    branch = _git(cfg.cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if branch is not None:
        status = _git(cfg.cwd, "status", "--porcelain") or ""
        dirty = "dirty" if status else "clean"
        lines.append(f"  <git>branch {branch}, worktree {dirty}</git>")
    lines.append("</environment_context>")
    return "\n".join(lines)
