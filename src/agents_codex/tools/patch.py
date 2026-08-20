"""apply_patch editor: enforces workspace jailing and applies V4A diffs.

The SDK's ApplyPatchTool parses the Codex patch envelope and hands us
create/update/delete operations; agents.apply_diff applies hunks. We add
what Codex's apply-patch crate enforces: path jailing to the workspace,
sandbox-mode write policy, and unified-diff previews for approval UIs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agents import apply_diff
from agents.editor import ApplyPatchOperation, ApplyPatchResult

from ..config import Config


class PatchPolicyError(Exception):
    """Raised when an operation escapes the workspace or violates policy."""


@dataclass
class WorkspaceEditor:
    cfg: Config
    # (op_type, path) log so UIs / tests can show what a turn changed.
    applied: list[tuple[str, str]] = field(default_factory=list)

    def _resolve(self, raw: str) -> Path:
        path = Path(raw)
        if not path.is_absolute():
            path = self.cfg.cwd / path
        resolved = path.resolve()
        workspace = self.cfg.cwd.resolve()
        if not (resolved == workspace or workspace in resolved.parents):
            raise PatchPolicyError(f"path escapes workspace: {raw}")
        return resolved

    def _check_writable(self) -> None:
        if self.cfg.sandbox_mode == "read-only":
            raise PatchPolicyError(
                "sandbox is read-only; file edits are not permitted"
            )

    def create_file(self, operation: ApplyPatchOperation) -> ApplyPatchResult:
        self._check_writable()
        target = self._resolve(operation.path)
        if target.exists():
            return ApplyPatchResult(status="failed", output=f"file exists: {operation.path}")
        content = apply_diff("", operation.diff or "", mode="create")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self.applied.append(("create", operation.path))
        return ApplyPatchResult(status="completed", output=f"created {operation.path}")

    def update_file(self, operation: ApplyPatchOperation) -> ApplyPatchResult:
        self._check_writable()
        target = self._resolve(operation.path)
        if not target.is_file():
            return ApplyPatchResult(status="failed", output=f"no such file: {operation.path}")
        try:
            updated = apply_diff(
                target.read_text(encoding="utf-8"), operation.diff or ""
            )
        except Exception as exc:  # hunk mismatch — surface to the model
            return ApplyPatchResult(status="failed", output=f"patch failed: {exc}")
        if operation.move_to:
            dest = self._resolve(operation.move_to)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(updated, encoding="utf-8")
            target.unlink()
            self.applied.append(("move", f"{operation.path} -> {operation.move_to}"))
        else:
            target.write_text(updated, encoding="utf-8")
            self.applied.append(("update", operation.path))
        return ApplyPatchResult(status="completed", output=f"updated {operation.path}")

    def delete_file(self, operation: ApplyPatchOperation) -> ApplyPatchResult:
        self._check_writable()
        target = self._resolve(operation.path)
        if not target.is_file():
            return ApplyPatchResult(status="failed", output=f"no such file: {operation.path}")
        target.unlink()
        self.applied.append(("delete", operation.path))
        return ApplyPatchResult(status="completed", output=f"deleted {operation.path}")
