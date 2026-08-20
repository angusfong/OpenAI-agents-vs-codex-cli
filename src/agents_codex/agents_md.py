"""AGENTS.md discovery and merging, mirroring Codex CLI semantics:
global ~/.agents-codex/AGENTS.md first, then repo root down to cwd
(more specific files come later so they take precedence for the model).
"""

from __future__ import annotations

from pathlib import Path

from .config import app_home

FILENAMES = ("AGENTS.md", "AGENTS.override.md")
MAX_BYTES_PER_FILE = 32 * 1024


def _repo_root(cwd: Path) -> Path | None:
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def discover_agents_md(cwd: Path) -> list[Path]:
    found: list[Path] = []
    global_md = app_home() / "AGENTS.md"
    if global_md.is_file():
        found.append(global_md)
    root = _repo_root(cwd) or cwd
    chain: list[Path] = []
    node = cwd.resolve()
    while True:
        chain.append(node)
        if node == root or node.parent == node:
            break
        node = node.parent
    for directory in reversed(chain):
        for name in FILENAMES:
            path = directory / name
            if path.is_file() and path not in found:
                found.append(path)
    return found


def load_agents_md(cwd: Path) -> str:
    sections: list[str] = []
    for path in discover_agents_md(cwd):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:MAX_BYTES_PER_FILE]
        except OSError:
            continue
        sections.append(f"<!-- from {path} -->\n{text.strip()}")
    if not sections:
        return ""
    return "\n\n".join(sections)
