"""Approval policy engine, mirroring Codex CLI's exec-policy flow.

Codex classifies each shell command by splitting it into segments at shell
control operators and matching each segment against allow rules; unmatched
commands fall back to the approval policy default, and dangerous commands
(forced rm, destructive git) force a prompt even when a sandbox exists.
Decisions feed the SDK's needs_approval callbacks; the UI layer supplies
an `ask` coroutine and can persist prefix rules ("don't ask again for
`git push ...`") for the session.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable

from .config import Config
from .sandbox import bwrap_available, landlock_available

# Command prefixes Codex treats as known-safe reads (subset of its execpolicy
# defaults). A segment matches if its argv starts with one of these tuples.
SAFE_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("ls",), ("cat",), ("head",), ("tail",), ("wc",), ("stat",), ("file",),
    ("pwd",), ("echo",), ("true",), ("false",), ("which",), ("env",),
    ("grep",), ("rg",), ("find",), ("sed", "-n"), ("awk",), ("cut",),
    ("sort",), ("uniq",), ("diff",), ("du",), ("df",), ("date",), ("uname",),
    ("git", "status"), ("git", "log"), ("git", "diff"), ("git", "show"),
    ("git", "branch"), ("git", "rev-parse"), ("git", "remote"),
    ("python", "--version"), ("python3", "--version"), ("node", "--version"),
    ("cargo", "check"), ("nl",), ("tr",), ("basename",), ("dirname",),
)

CONTROL_OPERATORS = {"&&", "||", ";", "|", "&"}
# Shell features that disqualify prefix matching (Codex: redirection,
# substitution, env prefixes, wildcards make a segment untrusted).
DISQUALIFIERS = {">", ">>", "<", "$(", "`", "*", "?", "~", "=", "\n"}

DANGEROUS_HINTS = (
    ("rm", "-rf"), ("rm", "-fr"), ("rm", "-f"), ("git", "reset", "--hard"),
    ("git", "clean", "-f"), ("git", "push", "--force"), ("chmod", "-R"),
    ("chown", "-R"), ("mkfs",), ("dd",), ("shutdown",), ("reboot",),
)


class Decision(Enum):
    ALLOW = "allow"           # run without asking
    PROMPT = "prompt"         # ask the user
    FORBIDDEN = "forbidden"   # auto-reject


def split_segments(command: str) -> list[list[str]] | None:
    """Split a shell command into pipeline/list segments; None = unparseable."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    if any(any(d in tok for d in DISQUALIFIERS) for tok in tokens):
        return None
    segments: list[list[str]] = [[]]
    for tok in tokens:
        if tok in CONTROL_OPERATORS:
            if segments[-1]:
                segments.append([])
        else:
            segments[-1].append(tok)
    return [s for s in segments if s]


def _matches_prefix(argv: list[str], prefixes) -> bool:
    return any(tuple(argv[: len(p)]) == tuple(p) for p in prefixes)


def is_dangerous(command: str) -> bool:
    lowered = command.lower()
    for hint in DANGEROUS_HINTS:
        if all(part in lowered for part in hint):
            return True
    return False


@dataclass
class ApprovalPolicy:
    """Session-scoped policy state consulted by tool needs_approval hooks."""

    cfg: Config
    # UI callback: (kind, description, detail) -> ApprovalAnswer
    ask: Callable[[str, str, str], Awaitable["ApprovalAnswer"]] | None = None
    session_prefix_rules: list[tuple[str, ...]] = field(default_factory=list)
    approve_all_shell: bool = False
    approve_all_patches: bool = False

    @property
    def sandboxed(self) -> bool:
        if self.cfg.sandbox_mode == "danger-full-access":
            return False
        return bwrap_available() or landlock_available()

    def classify_shell(self, command: str) -> Decision:
        """Codex's default_exec_approval_requirement, condensed."""
        if is_dangerous(command):
            # Dangerous commands force a prompt even inside a sandbox
            # (auto-rejected under `never`).
            return Decision.FORBIDDEN if self.cfg.approval_policy == "never" else Decision.PROMPT
        segments = split_segments(command)
        if segments and all(
            _matches_prefix(seg, SAFE_PREFIXES)
            or _matches_prefix(seg, self.session_prefix_rules)
            for seg in segments
        ):
            return Decision.ALLOW
        policy = self.cfg.approval_policy
        if policy == "never":
            return Decision.ALLOW      # sandbox is the safety layer (codex exec default)
        if policy == "untrusted":
            return Decision.PROMPT     # always ask for unmatched commands
        # on-request (default): don't ask while a sandbox constrains the
        # command; ask when we'd be running it unprotected.
        if self.approve_all_shell:
            return Decision.ALLOW
        return Decision.ALLOW if self.sandboxed else Decision.PROMPT

    def classify_patch(self) -> Decision:
        """Codex's assess_patch_safety, condensed: the editor already jails
        writes to the workspace, so auto-approve in interactive-sandboxed and
        never modes; ask when unsandboxed under untrusted."""
        if self.cfg.sandbox_mode == "read-only":
            return Decision.FORBIDDEN
        if self.approve_all_patches or self.cfg.approval_policy == "never":
            return Decision.ALLOW
        if self.cfg.approval_policy == "untrusted":
            return Decision.PROMPT
        return Decision.ALLOW

    def add_prefix_rule(self, command: str, length: int = 2) -> None:
        segments = split_segments(command)
        if segments and segments[0]:
            self.session_prefix_rules.append(tuple(segments[0][:length]))


@dataclass
class ApprovalAnswer:
    approved: bool
    # graded options, like Codex's approval overlay
    always_for_session: bool = False
    add_prefix_rule: bool = False
    feedback: str | None = None   # "No, and tell the agent what to do differently"


def make_shell_needs_approval(policy: ApprovalPolicy):
    """needs_approval callback for ShellTool: (ctx, action, call_id) -> bool."""

    async def needs_approval(ctx, action, call_id) -> bool:
        commands = getattr(action, "commands", None) or []
        return any(policy.classify_shell(c) != Decision.ALLOW for c in commands)

    return needs_approval


def make_patch_needs_approval(policy: ApprovalPolicy):
    """needs_approval callback for ApplyPatchTool: (ctx, operation, call_id)."""

    async def needs_approval(ctx, operation, call_id) -> bool:
        return policy.classify_patch() != Decision.ALLOW

    return needs_approval
