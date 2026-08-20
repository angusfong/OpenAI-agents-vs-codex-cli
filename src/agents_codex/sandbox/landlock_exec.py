"""Landlock sandbox helper: apply a ruleset, then exec the target command.

Usage:
    python -m agents_codex.sandbox.landlock_exec --probe
    python -m agents_codex.sandbox.landlock_exec --mode read-only|workspace-write \
        [--writable PATH]... -- CMD [ARG]...

Filesystem policy mirrors Codex CLI's SandboxPolicy:
  read-only        read anywhere, write nowhere
  workspace-write  read anywhere; write to --writable roots, /tmp, /dev/null
Network policy: on Landlock ABI >= 4 TCP bind/connect are denied in both
modes (Codex denies network unless configured otherwise).

This is intentionally dependency-free (raw syscalls via ctypes) so the
sandboxed child needs nothing but the standard library.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys

# --- Landlock uapi constants (linux/landlock.h) ---
SYS_landlock_create_ruleset = 444
SYS_landlock_add_rule = 445
SYS_landlock_restrict_self = 446

LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
LANDLOCK_RULE_PATH_BENEATH = 1

ACCESS_FS_EXECUTE = 1 << 0
ACCESS_FS_WRITE_FILE = 1 << 1
ACCESS_FS_READ_FILE = 1 << 2
ACCESS_FS_READ_DIR = 1 << 3
ACCESS_FS_REMOVE_DIR = 1 << 4
ACCESS_FS_REMOVE_FILE = 1 << 5
ACCESS_FS_MAKE_CHAR = 1 << 6
ACCESS_FS_MAKE_DIR = 1 << 7
ACCESS_FS_MAKE_REG = 1 << 8
ACCESS_FS_MAKE_SOCK = 1 << 9
ACCESS_FS_MAKE_FIFO = 1 << 10
ACCESS_FS_MAKE_BLOCK = 1 << 11
ACCESS_FS_MAKE_SYM = 1 << 12
ACCESS_FS_REFER = 1 << 13        # ABI 2
ACCESS_FS_TRUNCATE = 1 << 14     # ABI 3
ACCESS_FS_IOCTL_DEV = 1 << 15    # ABI 5

ACCESS_NET_BIND_TCP = 1 << 0     # ABI 4
ACCESS_NET_CONNECT_TCP = 1 << 1  # ABI 4

READ_ACCESS = ACCESS_FS_EXECUTE | ACCESS_FS_READ_FILE | ACCESS_FS_READ_DIR
WRITE_ACCESS_ABI = {
    1: (
        ACCESS_FS_WRITE_FILE | ACCESS_FS_REMOVE_DIR | ACCESS_FS_REMOVE_FILE
        | ACCESS_FS_MAKE_CHAR | ACCESS_FS_MAKE_DIR | ACCESS_FS_MAKE_REG
        | ACCESS_FS_MAKE_SOCK | ACCESS_FS_MAKE_FIFO | ACCESS_FS_MAKE_BLOCK
        | ACCESS_FS_MAKE_SYM
    ),
}
WRITE_ACCESS_ABI[2] = WRITE_ACCESS_ABI[1] | ACCESS_FS_REFER
WRITE_ACCESS_ABI[3] = WRITE_ACCESS_ABI[2] | ACCESS_FS_TRUNCATE
WRITE_ACCESS_ABI[4] = WRITE_ACCESS_ABI[3]
WRITE_ACCESS_ABI[5] = WRITE_ACCESS_ABI[4] | ACCESS_FS_IOCTL_DEV

PR_SET_NO_NEW_PRIVS = 38

_libc = ctypes.CDLL(None, use_errno=True)


class RulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    ]


class PathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]
    _pack_ = 1


def _syscall(nr: int, *args) -> int:
    res = _libc.syscall(ctypes.c_long(nr), *args)
    if res < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
    return res


def landlock_abi() -> int:
    """Probe the kernel's Landlock ABI level; 0 = unavailable."""
    try:
        return _syscall(
            SYS_landlock_create_ruleset, None, ctypes.c_size_t(0),
            ctypes.c_uint32(LANDLOCK_CREATE_RULESET_VERSION),
        )
    except OSError:
        return 0


def apply_sandbox(mode: str, writable: list[str]) -> str:
    abi = landlock_abi()
    if abi <= 0:
        raise OSError("landlock unavailable")
    abi = min(abi, 5)
    write_access = WRITE_ACCESS_ABI[min(abi, max(WRITE_ACCESS_ABI))]
    handled_fs = READ_ACCESS | write_access
    handled_net = (ACCESS_NET_BIND_TCP | ACCESS_NET_CONNECT_TCP) if abi >= 4 else 0

    attr = RulesetAttr(handled_fs, handled_net, 0)
    # sizeof includes `scoped` (ABI 6); older kernels want the shorter struct.
    size = 16 if abi < 6 else ctypes.sizeof(attr)
    ruleset_fd = _syscall(
        SYS_landlock_create_ruleset, ctypes.byref(attr), ctypes.c_size_t(size),
        ctypes.c_uint32(0),
    )
    try:
        # Read (+execute) everywhere.
        _add_path_rule(ruleset_fd, "/", READ_ACCESS)
        if mode == "workspace-write":
            for root in [*writable, "/tmp", "/dev/null"]:
                if os.path.exists(root):
                    _add_path_rule(ruleset_fd, root, READ_ACCESS | write_access)
        _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
        _syscall(SYS_landlock_restrict_self, ctypes.c_int(ruleset_fd), ctypes.c_uint32(0))
    finally:
        os.close(ruleset_fd)
    net = "tcp-denied" if handled_net else "net-unrestricted(abi<4)"
    return f"landlock abi={abi} mode={mode} {net}"


def _add_path_rule(ruleset_fd: int, path: str, access: int) -> None:
    fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    try:
        attr = PathBeneathAttr(access, fd)
        _syscall(
            SYS_landlock_add_rule, ctypes.c_int(ruleset_fd),
            ctypes.c_int(LANDLOCK_RULE_PATH_BENEATH), ctypes.byref(attr),
            ctypes.c_uint32(0),
        )
    finally:
        os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--mode", choices=["read-only", "workspace-write"])
    parser.add_argument("--writable", action="append", default=[])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    ns = parser.parse_args()

    if ns.probe:
        abi = landlock_abi()
        if abi <= 0:
            print("landlock: unavailable", file=sys.stderr)
            return 1
        # Prove enforcement actually works here (some container runtimes
        # report the syscall but refuse restrict_self).
        try:
            apply_sandbox("read-only", [])
        except OSError as exc:
            print(f"landlock: probe failed: {exc}", file=sys.stderr)
            return 1
        print(f"landlock: ok abi={abi}")
        return 0

    if not ns.mode or not ns.command:
        parser.error("--mode and a command are required")
    cmd = ns.command[1:] if ns.command and ns.command[0] == "--" else ns.command
    try:
        apply_sandbox(ns.mode, ns.writable)
    except OSError as exc:
        print(f"landlock_exec: cannot sandbox: {exc}", file=sys.stderr)
        return 125
    os.execvp(cmd[0], cmd)
    return 127  # unreachable


if __name__ == "__main__":
    sys.exit(main())
