"""Local shell executor for the SDK's ShellTool.

Mirrors Codex CLI behavior: commands run relative to the session cwd with a
timeout and per-stream output truncation; under read-only / workspace-write
sandbox modes the process is wrapped by the sandbox layer when available.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

from agents import ShellCommandOutput, ShellCommandRequest, ShellResult
from agents.tool import ShellCallOutcome

from ..config import Config
from ..sandbox import wrap_command


@dataclass
class ShellExecutor:
    cfg: Config

    async def __call__(self, request: ShellCommandRequest) -> ShellResult:
        action = request.data.action
        timeout_ms = action.timeout_ms or self.cfg.shell_timeout_ms
        max_bytes = action.max_output_length or self.cfg.shell_max_output_bytes
        outputs: list[ShellCommandOutput] = []
        for command in action.commands:
            outputs.append(
                await self._run_one(command, timeout_ms=timeout_ms, max_bytes=max_bytes)
            )
        return ShellResult(output=outputs, provider_data={"cwd": str(self.cfg.cwd)})

    async def _run_one(
        self, command: str, *, timeout_ms: int, max_bytes: int
    ) -> ShellCommandOutput:
        argv, sandbox_note = wrap_command(command, self.cfg)
        env = dict(os.environ)
        if self.cfg.sandbox_mode != "danger-full-access":
            # Codex marks sandboxed environments so tooling can adapt.
            env["CODEX_SANDBOX"] = env["AGENTS_CODEX_SANDBOX"] = self.cfg.sandbox_mode
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self.cfg.cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            return ShellCommandOutput(
                command=command,
                stdout="",
                stderr=f"failed to spawn: {exc}",
                outcome=ShellCallOutcome(type="exit", exit_code=127),
            )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_ms / 1000
            )
            outcome = ShellCallOutcome(type="exit", exit_code=proc.returncode)
        except asyncio.TimeoutError:
            proc.kill()
            stdout_b, stderr_b = await proc.communicate()
            outcome = ShellCallOutcome(type="timeout")
        stdout = _truncate(stdout_b, max_bytes)
        stderr = _truncate(stderr_b, max_bytes)
        if sandbox_note and outcome.type == "exit" and outcome.exit_code not in (0, None):
            stderr += f"\n[note: command ran under {sandbox_note}]"
        return ShellCommandOutput(
            command=command, stdout=stdout, stderr=stderr, outcome=outcome
        )


def _truncate(data: bytes, max_bytes: int) -> str:
    text = data.decode("utf-8", errors="replace")
    if len(data) <= max_bytes:
        return text
    head = text[: max_bytes // 2]
    tail = text[-(max_bytes // 2) :]
    omitted = len(data) - max_bytes
    return f"{head}\n[... {omitted} bytes truncated ...]\n{tail}"
