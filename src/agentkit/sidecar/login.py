"""The CLIs' sign-in: the pty flow behind /login/start and /login/code, the status and logout
calls. Claude Code prints a claude.ai link and waits for a code; Codex prints a device-flow link
with a one-time code and ends when the user has entered it."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import select
import shutil
import struct
import subprocess
import termios
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentkit.sidecar.runner import (
    BACKENDS,
    USER_RE,
    BadRequest,
    Busy,
    Config,
    backend_dir,
    check_backend,
    check_user,
    child_env,
    prepare,
    release,
    run_tracked,
    sandboxed,
    spawn,
)

CODE_RE = re.compile(r"[\x21-\x7e]{1,1024}")
ANSI_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-_]")
LINK_RE = re.compile(r"https://[^\s\x07\x1b\"'<>]+")
DEVICE_CODE_RE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,5}\b")
START_LIMIT, START_WINDOW = 5, 60.0

Key = tuple[str, str]


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\r", "")


def login_args(cfg: Config, backend: str) -> list[str]:
    """The CLI's own login: claude.ai sign-in with a code to paste back, or the ChatGPT device
    code flow, which needs no callback into the container."""
    if backend == "claude":
        return [cfg.bins["claude"], "auth", "login", "--claudeai"]
    return [cfg.bins["codex"], "login", "--device-auth"]


@dataclass
class Login:
    proc: subprocess.Popen[bytes]
    fd: int
    started: float = field(default_factory=time.monotonic)
    output: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    closed: bool = False
    drained: threading.Event = field(default_factory=threading.Event)

    def text(self) -> str:
        with self.lock:
            return strip_ansi("".join(self.output))

    def close(self) -> None:
        """Stop the CLI. The pty master stays open until the pump has seen the end of the output
        and calls `free_fd`, so a reused descriptor number is never read or written by a stale
        owner."""
        release(self.proc)

    def write(self, data: bytes) -> bool:
        """Send `data` to the CLI's terminal; False when the sign-in is over."""
        with self.lock:
            if self.closed:
                return False
            try:
                os.write(self.fd, data)
            except OSError:
                return False
            return True

    def free_fd(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            with contextlib.suppress(OSError):
                os.close(self.fd)


class Aux:
    """Bounds the status and logout subprocesses and caches status answers, so a flood of
    requests cannot start a flood of CLIs."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.slots = threading.BoundedSemaphore(cfg.max_aux)
        self.lock = threading.Lock()
        self.cache: dict[Key, tuple[float, dict[str, Any]]] = {}

    def run(
        self, args: list[str], directory: Path, backend: str
    ) -> subprocess.CompletedProcess[bytes] | None:
        if not self.slots.acquire(timeout=30):
            raise Busy()
        try:
            env = child_env(self.cfg, directory, backend)
            full, env = sandboxed(self.cfg, args, env, directory)
            return run_tracked(full, timeout=30, cwd=directory / "home", env=env)
        except (OSError, subprocess.TimeoutExpired):
            return None
        finally:
            self.slots.release()

    def forget(self, user: str, backend: str) -> None:
        with self.lock:
            self.cache.pop((user, backend), None)

    def status(self, user: str, backend: str) -> dict[str, Any]:
        key = (user, backend)
        with self.lock:
            hit = self.cache.get(key)
        if hit and time.monotonic() - hit[0] < self.cfg.status_ttl:
            return dict(hit[1])
        found = status(self.cfg, user, backend, self)
        if "error" not in found:
            with self.lock:
                self.cache[key] = (time.monotonic(), found)
        return found


class Logins:
    """At most one running sign-in per user and backend, `max_logins` overall; each ends after
    `login_ttl`."""

    def __init__(self, cfg: Config, aux: Aux | None = None) -> None:
        self.cfg = cfg
        self.aux = aux or Aux(cfg)
        self.lock = threading.Lock()
        self.running: dict[Key, Login] = {}
        self.key_locks: dict[Key, threading.Lock] = {}
        self.starts: dict[str, list[float]] = {}

    def _rate_limit(self, user: str) -> None:
        """At most START_LIMIT sign-in starts per user and START_WINDOW seconds."""
        now = time.monotonic()
        with self.lock:
            recent = [t for t in self.starts.get(user, []) if now - t < START_WINDOW]
            if len(recent) >= START_LIMIT:
                self.starts[user] = recent
                raise BadRequest("too many sign-in attempts; wait a minute")
            self.starts[user] = [*recent, now]
            for other in [u for u, ts in self.starts.items() if now - ts[-1] > START_WINDOW]:
                del self.starts[other]

    def _key_lock(self, key: Key) -> threading.Lock:
        with self.lock:
            return self.key_locks.setdefault(key, threading.Lock())

    def _drop(self, key: Key, expected: Login | None = None) -> None:
        """End the sign-in under `key`; with `expected`, only if it is still that one."""
        with self.lock:
            login = self.running.get(key)
            if login is None or (expected is not None and login is not expected):
                login = None
            else:
                del self.running[key]
        if login is not None:
            login.close()

    def start(self, user: object, backend: object, wait: float = 20.0) -> dict[str, str]:
        key: Key = (check_user(user), check_backend(backend))
        self._rate_limit(key[0])
        with self._key_lock(key):
            directory = prepare(self.cfg, *key)
            self._drop(key)
            with self.lock:
                if len(self.running) >= self.cfg.max_logins:
                    raise BadRequest("too many sign-ins at once; try again shortly")
            if shutil.which(self.cfg.bins[key[1]], path=self.cfg.path) is None:
                raise BadRequest("the CLI is not installed")
            login = self._spawn(key, directory)
            return self._await_link(key, login, wait)

    def _spawn(self, key: Key, directory: Path) -> Login:
        master, slave = os.openpty()
        # Wide enough that the login URL is never wrapped.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 4000, 0, 0))
        env = child_env(self.cfg, directory, key[1])
        args, env = sandboxed(self.cfg, login_args(self.cfg, key[1]), env, directory)
        try:
            proc = spawn(
                args, cwd=directory / "home", env=env, stdin=slave, stdout=slave, stderr=slave
            )
        except OSError as exc:
            os.close(master)
            raise BadRequest("the CLI could not start") from exc
        finally:
            os.close(slave)
        login = Login(proc, master)
        with self.lock:
            self.running[key] = login
        self.aux.forget(*key)
        threading.Thread(target=self._pump, args=(key, login), daemon=True).start()
        return login

    def _await_link(self, key: Key, login: Login, wait: float) -> dict[str, str]:
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            found = self._link(key[1], login.text())
            if found:
                return found
            if login.proc.poll() is not None:
                # It may have printed the link and exited: read what is left.
                login.drained.wait(2)
                found = self._link(key[1], login.text())
                if found:
                    return found
                break
            time.sleep(0.1)
        self._drop(key, login)
        raise BadRequest("the CLI did not offer a sign-in link")

    @staticmethod
    def _link(backend: str, text: str) -> dict[str, str] | None:
        links = LINK_RE.findall(text)
        if backend == "claude":
            pick = next((u for u in links if "oauth" in u or "authorize" in u), None)
            return {"url": pick} if pick else None
        url = next((u for u in links if "device" in u), links[0] if links else None)
        code = DEVICE_CODE_RE.search(text)
        return {"url": url, "code": code.group(0)} if url and code else None

    def _pump(self, key: Key, login: Login) -> None:
        try:
            while time.monotonic() - login.started <= self.cfg.login_ttl:
                try:
                    ready, _, _ = select.select([login.fd], [], [], 0.5)
                    if not ready:
                        if login.proc.poll() is not None:
                            break
                        continue
                    chunk = os.read(login.fd, 4096)
                except (OSError, ValueError):
                    break
                if not chunk:
                    break
                with login.lock:
                    login.output.append(chunk.decode(errors="replace"))
                    del login.output[:-64]
        finally:
            login.drained.set()
            self._drop(key, login)
            login.close()
            login.free_fd()
            self.aux.forget(*key)

    def code(self, user: object, backend: object, code: str, wait: float = 20.0) -> int | None:
        """Hand the code to the waiting CLI; its exit code (0: signed in) or None if still open."""
        key: Key = (check_user(user), check_backend(backend))
        if key[1] != "claude":
            raise BadRequest("this backend takes no code")
        if not CODE_RE.fullmatch(code.strip()):
            raise BadRequest("invalid code")
        with self.lock:
            login = self.running.get(key)
        if login is None:
            raise BadRequest("no sign-in is running; start again")
        if not login.write(code.strip().encode() + b"\r"):
            raise BadRequest("no sign-in is running; start again")
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and login.proc.poll() is None:
            time.sleep(0.1)
        self.aux.forget(*key)
        return login.proc.returncode

    def cancel(self, user: str, backend: str) -> None:
        self._drop((user, backend))
        self.aux.forget(user, backend)


def status(cfg: Config, user: object, backend: object, aux: Aux | None = None) -> dict[str, Any]:
    """{connected, account_hint?} from the CLI's own status command; never a token."""
    name = check_backend(backend)
    directory = backend_dir(cfg, user, name)
    if not (directory / "home").is_dir():
        return {"connected": False}
    args = (
        [cfg.bins["claude"], "auth", "status"]
        if name == "claude"
        else [cfg.bins["codex"], "login", "status"]
    )
    try:
        done = (aux or Aux(cfg)).run(args, directory, name)
    except Busy:
        return {"connected": False, "error": "busy"}
    if done is None:
        return {"connected": False, "error": "unavailable"}
    if done.returncode != 0:
        return {"connected": False}
    if name == "claude":
        return _claude_status(done.stdout)
    text = strip_ansi((done.stdout + done.stderr).decode(errors="replace"))
    line = next(
        (ln.strip() for ln in text.splitlines() if ln.strip().lower().startswith("logged in")), ""
    )
    return {"connected": True, "account_hint": line[:80]} if line else {"connected": True}


def _claude_status(stdout: bytes) -> dict[str, Any]:
    try:
        info = json.loads(stdout)
    except ValueError:
        info = {}
    if not isinstance(info, dict):
        return {"connected": True}
    if info.get("loggedIn") is False:
        return {"connected": False}
    hint = info.get("email") or info.get("authMethod")
    if isinstance(hint, str) and hint:
        return {"connected": True, "account_hint": hint[:80]}
    return {"connected": True}


def logout(cfg: Config, logins: Logins, user: object, backend: object) -> None:
    """The CLI's logout, then the user's directory for that backend is wiped."""
    key, name = check_user(user), check_backend(backend)
    logins.cancel(key, name)
    directory = backend_dir(cfg, key, name)
    if (directory / "home").is_dir():
        args = (
            [cfg.bins["claude"], "auth", "logout"]
            if name == "claude"
            else [cfg.bins["codex"], "logout"]
        )
        with contextlib.suppress(Busy):  # the directory goes anyway, and with it the login
            logins.aux.run(args, directory, name)
    shutil.rmtree(directory, ignore_errors=True)
    logins.aux.forget(key, name)


def prune(cfg: Config, logins: Logins, keep: object, min_age_s: float = 600) -> int:
    """Remove the directories (and logins) of every user not in `keep`: users deleted from the
    app, or left over from a database that was reset or restored. A directory younger than
    `min_age_s` stays, as its key may be newer than the app's list. Returns how many went."""
    if not isinstance(keep, list) or not all(
        isinstance(k, str) and USER_RE.fullmatch(k) for k in keep
    ):
        raise BadRequest("invalid keep list")
    base = cfg.root / "users"
    if not base.is_dir():
        return 0
    entries = [e for e in base.iterdir() if e.is_dir() and not e.is_symlink()]
    if not keep and entries:
        # An empty keep list would wipe every login: most likely a wrong database, not a wish.
        raise BadRequest("refusing to prune every user")
    removed, now = 0, time.time()
    for entry in entries:
        if entry.name in keep or now - entry.stat().st_mtime < min_age_s:
            continue
        for backend in BACKENDS:
            logins.cancel(entry.name, backend)
        shutil.rmtree(entry, ignore_errors=True)
        removed += 1
    return removed


def terminal_login(cfg: Config, user: str, backend: str) -> int:
    """The interactive fallback: the CLI's own login in this terminal, as that app user."""
    name = check_backend(backend)
    directory = prepare(cfg, check_user(user), name)
    os.chdir(directory / "home")
    env = child_env(cfg, directory, name)
    args, env = sandboxed(cfg, login_args(cfg, name), env, directory)
    os.execvpe(args[0], args, env)
    return 1
