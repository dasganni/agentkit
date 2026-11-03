"""Runs a command under a Landlock ruleset: the listed system paths readable (and executable), only
the
listed directories writable, everything else in the file system out of reach.

A run, a status call and a sign-in all use the same uid in the agent sidecar; this keeps a run from
reading the login or the runner's secret even if a tool slipped past the CLI flags. Landlock needs
no
privileges, no user namespaces and works under no-new-privileges, cap_drop ALL and the default
seccomp
profiles of Docker and Podman. The ruleset holds for the command and everything it starts.

    python -I -S sandbox.py --ro /usr --ro /etc --rw /home/agent/claude -- claude -p …
    AGENT_SANDBOX_RULES='{"ro": [...], "rw": [...]}' python -I -S sandbox.py -- claude -p …

The runner passes the paths in AGENT_SANDBOX_RULES (removed before the command starts), so they do
not show
in /proc/<pid>/cmdline, which every process of the uid can read. From ABI 6 on the ruleset also
scopes
signals and abstract unix sockets to the sandbox: a run can signal or connect only to its own
processes.

Exit codes: 126 when the ruleset cannot be applied (the command never starts), 127 when the command
cannot be started. Standard library only.

Where user namespaces work, `bwrap_command` adds a second layer around it: a bubblewrap mount view
with
the root read-only, a private /tmp, /proc and /dev and the given directories hidden.
"""

from __future__ import annotations

import ctypes
import json
import os
import struct
import subprocess
import sys

# syscall numbers of the generic table (x86_64 and aarch64 alike)
NR_CREATE_RULESET = 444
NR_ADD_RULE = 445
NR_RESTRICT_SELF = 446
FLAG_VERSION = 1  # landlock_create_ruleset(NULL, 0, VERSION) returns the ABI
RULE_PATH_BENEATH = 1
PR_SET_NO_NEW_PRIVS = 38
RULES_ENV = "AGENT_SANDBOX_RULES"
SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0  # ABI 6
SCOPE_SIGNAL = 1 << 1  # ABI 6

# file system access rights by bit
EXECUTE = 1 << 0
WRITE_FILE = 1 << 1
READ_FILE = 1 << 2
READ_DIR = 1 << 3
TRUNCATE = 1 << 14  # ABI 3
IOCTL_DEV = 1 << 15  # ABI 5
# every right an ABI knows (ABI 4 added network rights only, ABI 6 and later scopes)
RIGHTS_BY_ABI = {1: (1 << 13) - 1, 2: (1 << 14) - 1, 3: (1 << 15) - 1, 5: (1 << 16) - 1}
READ = EXECUTE | READ_FILE | READ_DIR
# the rights a rule on a single file (not a directory) may carry
ON_FILES = EXECUTE | WRITE_FILE | READ_FILE | TRUNCATE | IOCTL_DEV

_libc = ctypes.CDLL(None, use_errno=True)
_libc.syscall.restype = ctypes.c_long


def abi() -> int:
    """The kernel's Landlock ABI version, 0 without Landlock (or when seccomp refuses it)."""
    return max(int(_libc.syscall(NR_CREATE_RULESET, None, 0, FLAG_VERSION)), 0)


def rights(version: int) -> int:
    return RIGHTS_BY_ABI[max(v for v in RIGHTS_BY_ABI if v <= version)]


def _fail(what: str) -> OSError:
    err = ctypes.get_errno()
    return OSError(err, f"{what}: {os.strerror(err)}")


def apply(read_only: list[str], read_write: list[str]) -> None:
    """Restricts this process (and what it starts) to the paths given; raises OSError when it
    cannot.
    Paths that do not exist are skipped."""
    version = abi()
    if version < 1:
        raise OSError("Landlock is not available")
    handled = rights(version)
    if version >= 6:  # struct landlock_ruleset_attr {handled_access_fs, handled_access_net, scoped}
        attr = struct.pack("=QQQ", handled, 0, SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL)
    else:
        attr = struct.pack("=Q", handled)
    ruleset = _libc.syscall(NR_CREATE_RULESET, ctypes.c_char_p(attr), len(attr), 0)
    if ruleset < 0:
        raise _fail("landlock_create_ruleset")
    try:
        for paths, allowed in ((read_only, READ), (read_write, handled)):
            for path in paths:
                try:
                    fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
                except FileNotFoundError:
                    continue
                try:
                    mask = (allowed if os.path.isdir(path) else allowed & ON_FILES) & handled
                    rule = struct.pack(
                        "=Qi", mask, fd
                    )  # struct landlock_path_beneath_attr (packed)
                    if (
                        _libc.syscall(
                            NR_ADD_RULE, ruleset, RULE_PATH_BENEATH, ctypes.c_char_p(rule), 0
                        )
                        < 0
                    ):
                        raise _fail(f"landlock_add_rule {path}")
                finally:
                    os.close(fd)
        if _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise _fail("prctl")
        if _libc.syscall(NR_RESTRICT_SELF, ruleset, 0) < 0:
            raise _fail("landlock_restrict_self")
    finally:
        os.close(ruleset)


# How /proc appears inside bubblewrap: a fresh one for the new pid namespace or, where a container
# forbids mounting proc, the container's read-only one.
_PROC_MOUNTS = (("--proc", "/proc"), ("--ro-bind", "/proc", "/proc"))
_bwrap_proc: dict[str, tuple[str, ...] | None] = {}


def bwrap_proc(bwrap: str) -> tuple[str, ...] | None:
    """The /proc mount arguments with which `bwrap` can create its namespaces here, or None.
    A definite answer is kept; a timeout or a kill is probed again next time."""
    if bwrap in _bwrap_proc:
        return _bwrap_proc[bwrap]
    for proc in _PROC_MOUNTS:
        probe = [
            bwrap,
            "--unshare-user",
            "--unshare-pid",
            "--die-with-parent",
            "--ro-bind",
            "/",
            "/",
            *proc,
            "true",
        ]
        try:
            done = subprocess.run(probe, capture_output=True, timeout=10, check=False)
        except subprocess.TimeoutExpired:
            return None
        except OSError:
            break
        if done.returncode < 0:
            return None  # killed by a signal (e.g. the orphan reaper): not an answer, probe again
        if done.returncode == 0:
            _bwrap_proc[bwrap] = proc
            return proc
    _bwrap_proc[bwrap] = None
    return None


def bwrap_command(
    bwrap: str,
    proc: tuple[str, ...],
    args: list[str],
    writable: list[str],
    hide: list[str],
    cwd: str,
) -> list[str]:
    """`args` inside bubblewrap: the root read-only, a private /tmp, /proc and /dev, the `hide`
    directories empty, then only the `writable` directories mounted back."""
    view = [
        bwrap, "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
        "--die-with-parent", "--new-session", "--ro-bind", "/", "/", *proc,
        "--dev", "/dev", "--tmpfs", "/tmp",
    ]  # fmt: skip
    for hidden in hide:
        if os.path.isdir(hidden):
            view += ["--tmpfs", hidden]
    for rw in writable:
        view += ["--bind", rw, rw]
    return [*view, "--chdir", cwd, "--", *args]


def main(argv: list[str]) -> int:
    read_only: list[str] = []
    read_write: list[str] = []
    rules = os.environ.pop(RULES_ENV, None)
    if rules is not None:
        try:
            parsed = json.loads(rules)
            read_only += [str(p) for p in parsed["ro"]]
            read_write += [str(p) for p in parsed["rw"]]
        except (ValueError, KeyError, TypeError):
            print(f"sandbox: {RULES_ENV} is not valid", file=sys.stderr)
            return 126
    while len(argv) >= 2 and argv[0] in ("--ro", "--rw"):
        (read_only if argv[0] == "--ro" else read_write).append(argv[1])
        argv = argv[2:]
    if not argv or argv[0] != "--" or len(argv) < 2:
        print("usage: sandbox.py [--ro PATH | --rw PATH]... -- COMMAND [ARG]...", file=sys.stderr)
        return 2
    command = argv[1:]
    try:
        apply(read_only, read_write)
    except OSError as e:
        print(f"sandbox: {e}", file=sys.stderr)
        return 126
    try:
        os.execvp(command[0], command)
    except OSError as e:
        print(f"sandbox: cannot start {command[0]}: {e.strerror}", file=sys.stderr)
        return 127
    return 127


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
