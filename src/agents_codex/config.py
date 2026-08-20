"""Configuration loading, modeled on Codex CLI's ~/.codex/config.toml.

Precedence (last wins): built-in defaults < ~/.agents-codex/config.toml
< <repo>/.agents-codex.toml < profile overlay < CLI flags.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

APP_DIR_ENV = "AGENTS_CODEX_HOME"


def app_home() -> Path:
    override = os.environ.get(APP_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".agents-codex"


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    command: str | None = None          # stdio transport
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None              # streamable-http transport
    require_approval: str = "never"     # "always" | "never"


@dataclass(frozen=True)
class Config:
    model: str = "gpt-5.1-codex-max"
    # "read-only" | "workspace-write" | "danger-full-access"
    sandbox_mode: str = "workspace-write"
    # "untrusted" | "on-request" | "never"  (Codex also has "granular")
    approval_policy: str = "on-request"
    reasoning_effort: str = "medium"        # none|low|medium|high|xhigh
    reasoning_summary: str = "auto"         # auto|concise|detailed
    base_url: str | None = None             # custom provider endpoint
    wire_api: str = "responses"             # "responses" | "chat"
    api_key_env: str = "OPENAI_API_KEY"
    max_turns: int | None = None            # None = unlimited, like Codex
    shell_timeout_ms: int = 10_000
    shell_max_output_bytes: int = 10_240    # per stream, mirrors Codex truncation
    cwd: Path = field(default_factory=Path.cwd)
    mcp_servers: tuple[McpServerConfig, ...] = ()
    disable_sandbox_hardening: bool = False  # skip Landlock even if available

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env)


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"error: invalid TOML in {path}: {exc}") from exc


def _apply(cfg: Config, data: dict[str, Any]) -> Config:
    simple = {
        k: v
        for k, v in data.items()
        if k in Config.__dataclass_fields__ and k not in ("mcp_servers", "cwd")
    }
    if "cwd" in data:
        simple["cwd"] = Path(data["cwd"]).expanduser()
    servers = list(cfg.mcp_servers)
    for name, spec in data.get("mcp_servers", {}).items():
        servers = [s for s in servers if s.name != name]
        servers.append(
            McpServerConfig(
                name=name,
                command=spec.get("command"),
                args=list(spec.get("args", [])),
                env=dict(spec.get("env", {})),
                url=spec.get("url"),
                require_approval=spec.get("require_approval", "never"),
            )
        )
    return replace(cfg, mcp_servers=tuple(servers), **simple)


def load_config(
    cwd: Path | None = None,
    profile: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> Config:
    cwd = (cwd or Path.cwd()).resolve()
    cfg = Config(cwd=cwd)
    user_data = _load_toml(app_home() / "config.toml")
    cfg = _apply(cfg, user_data)
    cfg = _apply(cfg, _load_toml(cwd / ".agents-codex.toml"))
    if profile:
        profiles = user_data.get("profiles", {})
        if profile not in profiles:
            raise SystemExit(f"error: unknown profile {profile!r}")
        cfg = _apply(cfg, profiles[profile])
    if overrides:
        cfg = _apply(cfg, {k: v for k, v in overrides.items() if v is not None})
    return cfg
