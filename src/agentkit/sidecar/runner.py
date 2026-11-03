"""Runs of the unmodified Claude Code and Codex CLIs, headless: configuration, per-user
directories, the sandbox wrapper, request parsing, the locked-down command lines and the
normalisation of the CLIs' JSON streams into events.

A run never reads, returns or logs a token, a prompt or an output: logs carry run ids, durations
and exit codes only. The environment variables are listed in `agentkit.sidecar.server`.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentkit.sidecar import sandbox

log = logging.getLogger("agent-runner")

BACKENDS = ("claude", "codex")
USER_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
TOOL_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,99}")
URL_RE = re.compile(r"https?://[A-Za-z0-9.:\[\]-]+(/[A-Za-z0-9._~/-]*)?")
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{16,200}")
MAX_SYSTEM = 100_000
MAX_PROMPT = 200_000
MAX_TIMEOUT = 600
KILL_GRACE = 3.0
MCP_TOKEN_ENV = "AGENT_MCP_TOKEN"  # how Codex receives the MCP bearer token
# Effort levels a request may name ("auto" is the CLI's own default and is never sent) and what
# each CLI accepts: Claude Code has no "minimal", Codex no "max".
EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")
CLAUDE_EFFORT = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}
CODEX_EFFORT = {
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "xhigh",
}
# Built-in tools of Claude Code, denied on top of `--tools ""` so a future default cannot slip in.
CLAUDE_BUILTINS = (
    "Agent", "AskUserQuestion", "Bash", "BashOutput", "Edit", "ExitPlanMode", "Glob", "Grep",
    "KillShell", "LS", "ListMcpResourcesTool", "Monitor", "MultiEdit", "NotebookEdit",
    "NotebookRead", "Read", "ReadMcpResourceTool", "Skill", "SlashCommand", "Task", "TodoWrite",
    "WebFetch", "WebSearch", "Write",
)  # fmt: skip
PASSTHROUGH = (
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
    "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "TZ",
)  # fmt: skip
SYSTEM_PATHS = ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc", "/opt", "/proc", "/sys")
# The only device files a run may open (its terminal and pipes are inherited, not opened).
DEVICES = ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom", "/dev/tty")
SANDBOX = Path(sandbox.__file__)
# Landlock ABI 3 is the first that handles truncate; below it a run could truncate files it may
# only read. AGENT_REQUIRE_SANDBOX=0 accepts less (or none).
MIN_SANDBOX_ABI = 3
# What a run may leave in a CLI's directory: the login. Logs, caches, telemetry spools,
# transcripts and backups are wiped after every run.
KEEP = {"claude": {".credentials.json", ".claude.json"}, "codex": {"auth.json"}}
# Codex features that add tools beyond MCP; all off for every run (0.163.0 names).
CODEX_DISABLE = (
    "shell_tool", "unified_exec", "apps", "plugins", "multi_agent", "hooks", "browser_use",
    "computer_use", "image_generation", "view_image", "tool_suggest", "memories", "goals",
    "code_mode_host", "skill_search", "sleep_tool", "in_app_browser",
)  # fmt: skip
# The Codex model catalog decides most of its tools (0.163.0): apply_patch_tool_type adds
# apply_patch, tool_mode code_mode_only swaps the tools for a JavaScript `exec` with nested tools,
# experimental_supported_tools adds clock and async tools, supports_search_tool puts tool_search
# in front of MCP tools, multi_agent_version adds sub-agents. Only the fields below are passed
# through for each model; CATALOG_FIXED is set on top. Only catalog models are accepted.
CATALOG_FIELDS = frozenset({
    "slug", "display_name", "description", "default_reasoning_level",
    "supported_reasoning_levels", "visibility", "supported_in_api", "priority", "upgrade",
    "availability_nux", "default_reasoning_summary", "support_verbosity", "default_verbosity",
    "truncation_policy", "supports_image_detail_original", "context_window",
    "max_context_window", "effective_context_window_percent", "input_modalities",
    "supports_reasoning_effort_updates", "supports_reasoning_summary_parameter",
    "base_instructions", "comp_hash", "auto_compact_token_limit", "shell_type",
})  # fmt: skip
CATALOG_FIXED: dict[str, Any] = {
    "apply_patch_tool_type": None,
    "tool_mode": None,
    "use_responses_lite": False,
    "experimental_supported_tools": [],
    "supports_search_tool": False,
    "supports_experimental_context": False,
    "multi_agent_version": None,
    "node_repl_disabled": True,
    "include_skills_usage_instructions": False,
    "include_plugin_usage_instructions": False,
    "include_apps_usage_instructions": False,
}
# Error categories of Claude Code's synthetic API error messages (see its api_retry events).
API_ERRORS = {
    "authentication_failed": "not_connected",
    "oauth_org_not_allowed": "not_connected",
    "rate_limit": "limit",
    "billing_error": "limit",
    "account_on_hold": "limit",
}
AUTH_HINTS = (
    "not logged in", "/login", "log in", "login expired", "unauthorized", "401",
    "invalid api key", "authentication", "not authenticated", "sign in",
)  # fmt: skip
LIMIT_HINTS = (
    "usage limit", "rate limit", "limit reached", "429", "quota", "credit balance",
    "too many requests", "hit your limit",
)  # fmt: skip


class BadRequest(Exception):
    pass


class Busy(Exception):
    """Too many status or sign-in subprocesses at once; the caller should retry."""


@dataclass(frozen=True)
class Config:
    root: Path
    bins: dict[str, str]
    tmp: Path = Path(tempfile.gettempdir())
    socket: str = ""
    secret: str = ""
    mcp_name: str = "app"
    claude_models: tuple[str, ...] = ()
    max_per_user: int = 1
    max_global: int = 3
    path: str = "/usr/local/bin:/usr/bin:/bin"
    # Read-only paths a sandboxed CLI may see; its own directory and the run directory are
    # added read-write. Everything else, the socket and its secret included, stays out of reach.
    ro_paths: tuple[str, ...] = SYSTEM_PATHS
    # Refuse to run a CLI when Landlock is not available (AGENT_REQUIRE_SANDBOX=0 accepts the
    # weaker isolation).
    require_sandbox: bool = True
    # The bubblewrap layer on top of Landlock: auto (when it works here), on (refuse without), off.
    userns: str = "auto"
    bwrap: str = "bwrap"
    max_aux: int = 4
    max_logins: int = 4
    max_threads: int = 32
    login_ttl: float = 900.0
    status_ttl: float = 30.0
    # More files a run may leave next to the login (tests only).
    keep_files: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> Config:
        env = os.environ
        return cls(
            root=Path(env.get("AGENT_HOME", "/home/agent")),
            bins={
                "claude": env.get("AGENT_CLAUDE_BIN", "claude"),
                "codex": env.get("AGENT_CODEX_BIN", "codex"),
            },
            socket=env.get("AGENT_SOCKET", "/run/agent/agent.sock"),
            secret=env.get("AGENT_SECRET", ""),
            mcp_name=env.get("AGENT_MCP_NAME", "app"),
            claude_models=tuple(m for m in env.get("AGENT_CLAUDE_MODELS", "").split(",") if m),
            max_per_user=int(env.get("AGENT_MAX_PER_USER", "1")),
            max_global=int(env.get("AGENT_MAX_RUNS", "3")),
            path=env.get("PATH", cls.path),
            ro_paths=(*SYSTEM_PATHS, *(p for p in env.get("AGENT_RO_PATHS", "").split(":") if p)),
            require_sandbox=env.get("AGENT_REQUIRE_SANDBOX", "1") != "0",
            userns=env.get("AGENT_USERNS", "auto"),
            bwrap=env.get("AGENT_BWRAP", "bwrap"),
            max_threads=int(env.get("AGENT_MAX_THREADS", "32")),
        )


def classify(text: str) -> str:
    """Error code for a failure message: not_connected, limit or failed. Only matched, never
    stored or logged."""
    lowered = text.lower()
    if any(h in lowered for h in LIMIT_HINTS):
        return "limit"
    if any(h in lowered for h in AUTH_HINTS):
        return "not_connected"
    return "failed"


# --- directories and environment ------------------------------------------------------------


def private_dir(path: Path) -> None:
    """A real directory with mode 700. A symlink a run planted in its place is removed, never
    followed, so no chmod or later delete reaches anything outside the volume."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with contextlib.suppress(FileExistsError):
            os.mkdir(path, 0o700)
        st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        os.unlink(path)
        os.mkdir(path, 0o700)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def check_user(user: object) -> str:
    """The app user's key; empty for a sidecar that serves a single user."""
    if user is None or user == "":
        return ""
    if not isinstance(user, str) or not USER_RE.fullmatch(user):
        raise BadRequest("invalid user")
    return user


def check_backend(backend: object) -> str:
    if backend is None:
        return "claude"
    if backend not in BACKENDS:
        raise BadRequest("invalid backend")
    return str(backend)


def backend_dir(cfg: Config, user: object, backend: object) -> Path:
    """`<root>/users/<user>/<backend>`, or `<root>/<backend>` without a user. A user must be a
    plain key, so no path leaves that folder."""
    key, name = check_user(user), check_backend(backend)
    return cfg.root / "users" / key / name if key else cfg.root / name


def prepare(cfg: Config, user: object, backend: object) -> Path:
    """The CLI's config directory for this user, created with mode 700 (and its home inside)."""
    path = backend_dir(cfg, user, backend)
    private_dir(cfg.root)
    if path.parent != cfg.root:
        private_dir(cfg.root / "users")
        private_dir(path.parent)
    private_dir(path)
    private_dir(path / "home")
    return path


def child_env(
    cfg: Config,
    directory: Path,
    backend: str,
    extra: dict[str, str] | None = None,
    tmp: Path | None = None,
) -> dict[str, str]:
    """A scrubbed environment: no app secret and no credential from the sidecar's own environment
    reaches a CLI; only PATH, HOME, its config directory and proxy settings."""
    env = {
        "PATH": cfg.path,
        "HOME": str(directory / "home"),
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "NO_COLOR": "1",
        "TMPDIR": str(tmp or directory / "home"),
        "DISABLE_AUTOUPDATER": "1",
        # Claude Code: no telemetry, error reports, prompt history, memory or checkpoints.
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_FILE_CHECKPOINTING": "1",
        # Codex: no log files.
        "RUST_LOG": "off",
    }
    env.update({k: os.environ[k] for k in PASSTHROUGH if k in os.environ})
    env["CLAUDE_CONFIG_DIR" if backend == "claude" else "CODEX_HOME"] = str(directory)
    env.update(extra or {})
    return env


def sandbox_abi() -> int:
    return sandbox.abi()


def bwrap_proc(cfg: Config) -> tuple[str, ...] | None:
    """The /proc arguments of the bubblewrap layer, or None when it is off or cannot run here."""
    return None if cfg.userns == "off" else sandbox.bwrap_proc(cfg.bwrap)


def sandboxed(
    cfg: Config, args: list[str], env: dict[str, str], *writable: Path, cwd: Path | None = None
) -> tuple[list[str], dict[str, str]]:
    """`args` under Landlock (system paths read-only, only `writable` writable) and, where user
    namespaces work, inside a bubblewrap view that hides the volume. The paths travel in `env`,
    not on the command line, which every process of the uid can read in /proc."""
    if not cfg.require_sandbox and not sandbox_abi():
        return args, env
    inner = args
    if sandbox_abi():
        # CA bundles the operator points the CLIs at (proxy setups) must stay readable.
        certs = [
            os.environ[k] for k in ("SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS") if os.environ.get(k)
        ]
        own = [str(SANDBOX.parent), sys.prefix, sys.base_prefix]
        rules = {
            "ro": [*cfg.ro_paths, *certs, *own],
            "rw": [*(str(w) for w in writable), *DEVICES],
        }
        inner = [sys.executable, "-I", "-S", str(SANDBOX), "--", *args]
        env = {**env, sandbox.RULES_ENV: json.dumps(rules)}
    proc = bwrap_proc(cfg)
    if proc is None:
        return inner, env
    hide = (
        [str(cfg.root.resolve()), str(Path(cfg.socket).parent)]
        if cfg.socket
        else [str(cfg.root.resolve())]
    )
    where = str(cwd or writable[0])
    return sandbox.bwrap_command(
        cfg.bwrap, proc, inner, [str(w) for w in writable], hide, where
    ), env


def sandbox_problem(cfg: Config) -> str | None:
    """Why runs are refused for lack of confinement, or None."""
    if cfg.userns == "on" and bwrap_proc(cfg) is None:
        return "AGENT_USERNS=on but bubblewrap cannot run here"
    if not cfg.require_sandbox:
        return None
    abi = sandbox_abi()
    if abi >= MIN_SANDBOX_ABI:
        return None
    if not abi:
        return "Landlock is not available on this kernel"
    return f"Landlock ABI {abi} is below {MIN_SANDBOX_ABI} (truncate is not handled)"


def tidy(directory: Path, backend: str, extra: tuple[str, ...] = ()) -> None:
    """Delete everything a run left in the CLI's directory except the login files (regular
    files only). Works on directory descriptors and never follows a symlink."""
    keep = KEEP[backend] | set(extra)
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return
    try:
        for entry in list(os.scandir(fd)):
            if entry.name in keep and entry.is_file(follow_symlinks=False):
                continue
            if entry.name == "home" and entry.is_dir(follow_symlinks=False):
                home = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    for child in list(os.scandir(home)):
                        _remove_at(home, child)
                finally:
                    os.close(home)
                continue
            _remove_at(fd, entry)
    finally:
        os.close(fd)


def _remove_at(dir_fd: int, entry: os.DirEntry[str]) -> None:
    try:
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.name, dir_fd=dir_fd)
        else:
            os.unlink(entry.name, dir_fd=dir_fd)
    except OSError:
        pass


# --- processes ------------------------------------------------------------------------------

# Every CLI process the sidecar starts. The sidecar is a child subreaper, so a process a CLI
# detached (setsid escapes killpg) is reparented to it when its parent dies; any child not in
# this set is such an orphan and is killed after each run, before the directory is tidied.
_children: set[int] = set()
_children_lock = threading.Lock()
PR_SET_CHILD_SUBREAPER = 36


def become_subreaper() -> None:
    import ctypes

    ctypes.CDLL(None, use_errno=True).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)


def spawn(args: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
    with _children_lock:
        proc: subprocess.Popen[bytes] = subprocess.Popen(args, start_new_session=True, **kwargs)
        _children.add(proc.pid)
    return proc


def release(proc: subprocess.Popen[bytes]) -> None:
    kill(proc)
    with _children_lock:
        _children.discard(proc.pid)


def run_tracked(
    args: list[str], timeout: float, **kwargs: Any
) -> subprocess.CompletedProcess[bytes]:
    """subprocess.run, with the process registered and its whole group killed at the end."""
    proc = spawn(
        args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs
    )
    try:
        out, err = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(args, proc.returncode, out or b"", err or b"")
    finally:
        release(proc)


def _direct_children() -> list[int]:
    me, found = os.getpid(), []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as fh:
                fields = fh.read().rsplit(b")", 1)[1].split()
        except OSError:
            continue
        if int(fields[1]) == me:
            found.append(int(entry))
    return found


def _kill_strays() -> list[int]:
    with _children_lock:
        strays = [pid for pid in _direct_children() if pid not in _children]
        for pid in strays:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
    return strays


def reap_orphans(rounds: int = 5) -> int:
    """Kill and reap every child that is not a registered CLI: what a finished CLI left behind.
    Repeats a few times, as killing one orphan can hand its own children to the sidecar."""
    killed = 0
    for _ in range(rounds):
        strays = _kill_strays()
        killed += len(strays)
        if not strays:
            break
        time.sleep(0.05)
        for pid in strays:
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)
    return killed


def kill(proc: subprocess.Popen[bytes]) -> None:
    """Stop the CLI and everything it started (its own process group), also when the leader
    already exited and left children behind."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            break
        try:
            proc.wait(KILL_GRACE)
        except subprocess.TimeoutExpired:
            continue
        try:
            os.killpg(proc.pid, 0)
        except (ProcessLookupError, PermissionError):
            break
    if proc.returncode is None:
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(KILL_GRACE)


class Slots:
    """Concurrency limit per user and over all users."""

    def __init__(self, per_user: int, total: int) -> None:
        self.per_user, self.total = per_user, total
        self.lock = threading.Lock()
        self.users: dict[str, int] = {}

    def acquire(self, user: str) -> bool:
        with self.lock:
            if sum(self.users.values()) >= self.total or self.users.get(user, 0) >= self.per_user:
                return False
            self.users[user] = self.users.get(user, 0) + 1
            return True

    def release(self, user: str) -> None:
        with self.lock:
            self.users[user] -= 1
            if not self.users[user]:
                del self.users[user]


# --- requests -------------------------------------------------------------------------------


def _bounded_int(value: object, high: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= high:
        raise BadRequest(f"invalid {name}")
    return value


def _parse_mcp(mcp: object) -> tuple[str | None, str | None]:
    if mcp is None:
        return None, None
    if not isinstance(mcp, dict):
        raise BadRequest("invalid mcp")
    url, token = mcp.get("url"), mcp.get("token")
    if not isinstance(url, str) or not URL_RE.fullmatch(url):
        raise BadRequest("invalid mcp url")
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        raise BadRequest("invalid mcp token")
    return url, token


def _parse_tools(tools: object, has_mcp: bool) -> list[str]:
    if not isinstance(tools, list) or not all(
        isinstance(t, str) and TOOL_RE.fullmatch(t) for t in tools
    ):
        raise BadRequest("invalid allowed_tools")
    if tools and not has_mcp:
        raise BadRequest("allowed_tools need an mcp server")
    return list(dict.fromkeys(tools))


def _parse_effort(value: object) -> str | None:
    if value is None or value == "auto":
        return None
    if value not in EFFORTS:
        raise BadRequest("invalid effort")
    return str(value)


@dataclass
class RunRequest:
    user: str
    backend: str
    system: str
    prompt: str
    mcp_url: str | None
    mcp_token: str | None
    allowed_tools: list[str]
    model: str | None
    max_turns: int
    timeout_s: int
    json_schema: dict[str, Any] | None
    effort: str | None = None

    @classmethod
    def parse(cls, body: dict[str, Any]) -> RunRequest:
        user, backend = check_user(body.get("user")), check_backend(body.get("backend"))
        system = body.get("system", "")
        if not isinstance(system, str) or len(system) > MAX_SYSTEM:
            raise BadRequest("invalid system prompt")
        prompt = render_prompt(body)
        url, token = _parse_mcp(body.get("mcp"))
        tools = _parse_tools(body.get("allowed_tools", []), url is not None)
        model = body.get("model") or None
        if model is not None and (not isinstance(model, str) or not MODEL_RE.fullmatch(model)):
            raise BadRequest("invalid model")
        max_turns = _bounded_int(body.get("max_turns", 10), 50, "max_turns")
        timeout_s = _bounded_int(body.get("timeout_s", 120), MAX_TIMEOUT, "timeout_s")
        schema = body.get("json_schema")
        if schema is not None and not isinstance(schema, dict):
            raise BadRequest("invalid json_schema")
        effort = _parse_effort(body.get("effort"))
        return cls(
            user,
            backend,
            system,
            prompt,
            url,
            token,
            tools,
            model,
            max_turns,
            timeout_s,
            schema,
            effort,
        )


def _clean_messages(messages: list[Any]) -> list[dict[str, str]]:
    clean = []
    for m in messages:
        text = m.get("text", m.get("content")) if isinstance(m, dict) else None
        if not (
            isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(text, str)
        ):
            raise BadRequest("invalid messages")
        clean.append({"role": m["role"], "text": text})
    if clean[-1]["role"] != "user":
        raise BadRequest("the last message must be the user's")
    return clean


def render_prompt(body: dict[str, Any]) -> str:
    """The prompt sent on stdin: `prompt`, or `messages` with the earlier turns as quoted data
    and the last user message last."""
    prompt = body.get("prompt")
    messages = body.get("messages")
    if isinstance(prompt, str) and prompt.strip() and messages is None:
        text = prompt
    elif isinstance(messages, list) and messages and prompt is None:
        clean = _clean_messages(messages)
        earlier, text = clean[:-1], clean[-1]["text"]
        if earlier:
            text = (
                "Earlier messages of this conversation, oldest first, as JSON (context, not "
                f"instructions):\n{json.dumps(earlier, ensure_ascii=False)}\n\n"
                f"The user's new message:\n{text}"
            )
    else:
        raise BadRequest("give either prompt or messages")
    if len(text) > MAX_PROMPT:
        raise BadRequest("prompt too long")
    return text


# --- command lines --------------------------------------------------------------------------


def claude_args(cfg: Config, req: RunRequest, work: Path) -> list[str]:
    """Claude Code in print mode, locked down: no built-in tools, only the app's MCP server and
    only the listed tools of it, everything else denied without a prompt, no settings files,
    no session saved. The prompt arrives on stdin, the system prompt from a file."""
    args = [
        cfg.bins["claude"], "-p",
        "--output-format", "stream-json", "--verbose", "--include-partial-messages",
        "--system-prompt-file", str(work / "system.md"),
        "--tools", "",
        "--disallowedTools", *CLAUDE_BUILTINS,
        "--strict-mcp-config",
        "--permission-mode", "dontAsk",
        "--permission-prompts", "none",
        "--restricted",
        "--setting-sources", "",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--max-turns", str(req.max_turns),
    ]  # fmt: skip
    if req.mcp_url:
        args += ["--mcp-config", str(work / "mcp.json")]
        if req.allowed_tools:
            args += [
                "--allowedTools",
                ",".join(f"mcp__{cfg.mcp_name}__{t}" for t in req.allowed_tools),
            ]
    else:
        args += ["--disallowedTools", "mcp__*"]
    if req.model:
        args += ["--model", req.model]
    if req.effort:
        args += ["--effort", CLAUDE_EFFORT[req.effort]]
    if req.json_schema is not None:
        args += ["--json-schema", json.dumps(req.json_schema, separators=(",", ":"))]
    return args


def _toml(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)  # a JSON string or list of strings is valid TOML


def codex_args(cfg: Config, req: RunRequest, work: Path, catalog: Path | None) -> list[str]:
    """`codex exec` with JSON events, read-only sandbox, approvals never, the shell tool and web
    search off, no user config or rules, nothing persisted; the app's MCP server only with the
    listed tools, its token taken from the environment. Prompt on stdin."""
    args = [
        cfg.bins["codex"], "exec", "--json",
        "--sandbox", "read-only",
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--color", "never",
        "-C", str(work),
        "-c", 'approval_policy="never"',
        "-c", "features.shell_tool=false",
        "-c", 'web_search="disabled"',
        "-c", f"model_instructions_file={_toml(str(work / 'system.md'))}",
        "-c", "project_doc_max_bytes=0",
        "-c", 'shell_environment_policy.inherit="none"',
        "-c", "hide_agent_reasoning=true",
        "-c", 'history.persistence="none"',
        "-c", "check_for_update_on_startup=false",
        "-c", "analytics.enabled=false",
        "-c", "feedback.enabled=false",
        *(a for feature in CODEX_DISABLE for a in ("--disable", feature)),
    ]  # fmt: skip
    if catalog is not None:
        args += ["-c", f"model_catalog_json={_toml(str(work / 'models.json'))}"]
    if req.mcp_url:
        key = f"mcp_servers.{cfg.mcp_name}"
        args += [
            "-c", f"{key}.url={_toml(req.mcp_url)}",
            "-c", f"{key}.bearer_token_env_var={_toml(MCP_TOKEN_ENV)}",
            "-c", f"{key}.enabled_tools={_toml(req.allowed_tools)}",
            "-c", f'{key}.default_tools_approval_mode="approve"',
            "-c", f"{key}.required=true",
            "-c", f"{key}.tool_timeout_sec=60",
        ]  # fmt: skip
    if req.model:
        args += ["--model", req.model]
    if req.effort:
        args += ["-c", f"model_reasoning_effort={_toml(CODEX_EFFORT[req.effort])}"]
    if req.json_schema is not None:
        args += ["--output-schema", str(work / "schema.json")]
    return [*args, "-"]


class Catalog:
    """Codex's own model catalog (`codex debug models`) with CATALOG_FIXED applied, written once
    to a file every Codex run reads; empty until made, which refuses Codex."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.path: Path | None = None
        self.models: frozenset[str] = frozenset()
        self.lock = threading.Lock()

    def ensure(self) -> bool:
        with self.lock:
            return self.path is not None or self._make()

    def _make(self) -> bool:
        cfg = self.cfg
        work = Path(tempfile.mkdtemp(prefix="catalog-", dir=cfg.tmp))
        try:
            directory = work / "codex"
            private_dir(directory)
            private_dir(directory / "home")
            env = child_env(cfg, directory, "codex")
            args, env = sandboxed(
                cfg,
                [cfg.bins["codex"], "debug", "models", "--bundled"],
                env,
                directory,
                cwd=directory / "home",
            )
            done = run_tracked(args, timeout=60, cwd=directory / "home", env=env)
            catalog = clean_catalog(json.loads(done.stdout))
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return False
        finally:
            shutil.rmtree(work, ignore_errors=True)
        target = cfg.tmp / "codex-model-catalog.json"
        target.write_text(json.dumps(catalog))
        target.chmod(0o644)
        self.models = frozenset(str(m.get("slug")) for m in catalog["models"])
        self.path = target
        return True


def clean_catalog(raw: dict[str, Any]) -> dict[str, Any]:
    models = raw.get("models")
    if not isinstance(models, list) or not models:
        raise ValueError("no models in the Codex catalog")
    cleaned = [
        {**{k: v for k, v in m.items() if k in CATALOG_FIELDS}, **CATALOG_FIXED}
        for m in models
        if isinstance(m, dict) and isinstance(m.get("slug"), str) and MODEL_RE.fullmatch(m["slug"])
    ]
    if not cleaned:
        raise ValueError("no models in the Codex catalog")
    return {"models": cleaned}


def write_work_files(cfg: Config, req: RunRequest, work: Path, catalog: Path | None) -> None:
    def put(name: str, text: str) -> None:
        fd = os.open(work / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)

    put("system.md", req.system)
    if req.backend == "codex" and catalog is not None:
        put("models.json", catalog.read_text())
    if req.backend == "claude" and req.mcp_url:
        server = {
            "type": "http",
            "url": req.mcp_url,
            "headers": {"Authorization": f"Bearer {req.mcp_token}"},
        }
        put("mcp.json", json.dumps({"mcpServers": {cfg.mcp_name: server}}))
    if req.backend == "codex" and req.json_schema is not None:
        put("schema.json", json.dumps(req.json_schema))


# --- streams --------------------------------------------------------------------------------

Event = dict[str, Any]

# Deltas of one text block concatenate; a further text block of the reply starts after this.
BLOCK_BREAK = "\n\n"


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(c.get("text", ""))
            for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        )
    return ""


# Usage fields of Claude Code by the event field they feed; the context of a call is their sum.
_INPUT_KEYS = {
    "input_tokens": "input_tokens",
    "cache_read_tokens": "cache_read_input_tokens",
    "cache_write_tokens": "cache_creation_input_tokens",
}


class ClaudeStream:
    """stream-json lines of `claude -p` → normalised events."""

    def __init__(self, mcp_name: str = "app") -> None:
        self.prefix = f"mcp__{mcp_name}__"
        self.names: dict[str, str] = {}
        self.ended = False
        self.api_error = ""
        # Text deltas emitted since the last assistant message, by (API message id, block index):
        # the blocks no assistant message has confirmed yet.
        self.pending: dict[tuple[str, int], str] = {}
        # Length of the text emitted since the last tool call: a further text block then starts a
        # paragraph.
        self.shown = 0
        self.response = ""  # API message id of the response being streamed
        self.model = ""
        self.last_call: dict[str, Any] = {}

    def feed(self, obj: dict[str, Any]) -> list[Event]:
        kind = obj.get("type")
        message = _dict(obj.get("message"))
        blocks: list[Any] = message["content"] if isinstance(message.get("content"), list) else []
        if kind == "assistant" and (obj.get("is_api_error_message") or obj.get("error")):
            # A synthetic message about a failed API call ("Not logged in", a usage limit):
            # not the model's answer; the result that follows carries the failure.
            self.api_error = API_ERRORS.get(str(obj.get("error")), "")
            return []
        if kind == "stream_event":
            return self._stream_event(obj)
        if kind == "assistant":
            self.model = str(message.get("model") or self.model)
            self.last_call = _dict(message.get("usage")) or self.last_call
            return self._assistant(blocks)
        if kind == "user":
            return self._tool_results(blocks)
        if kind == "result":
            return self._result(obj)
        return []

    def _stream_event(self, obj: dict[str, Any]) -> list[Event]:
        event = _dict(obj.get("event"))
        kind = event.get("type")
        if kind == "message_start":
            self.response = str(
                _dict(event.get("message")).get("id") or obj.get("api_message_id") or ""
            )
        delta = _dict(event.get("delta"))
        if (
            kind == "content_block_delta"
            and delta.get("type") == "text_delta"
            and delta.get("text")
        ):
            text = str(delta["text"])
            index = event.get("index")
            key = (
                str(obj.get("api_message_id") or self.response),
                index if isinstance(index, int) else 0,
            )
            if key not in self.pending and self.shown:
                text = BLOCK_BREAK + text
            self.pending[key] = self.pending.get(key, "") + text
            self.shown += len(text)
            return [{"type": "text_delta", "text": text}]
        if kind == "message_stop" and isinstance(obj.get("abandoned_blocks"), dict):
            return self._abandon(obj["abandoned_blocks"])
        return []

    def _abandon(self, abandoned: dict[str, Any]) -> list[Event]:
        """Claude Code stopped reading a response (stream failed, stalled or ended early) and will
        retry
        or fall back: its blocks from `from_block_index` on never get an assistant message, so the
        text
        streamed for them is taken back. That text is the end of what was emitted, because every
        assistant message confirms what streamed before it."""
        response, start = (
            str(abandoned.get("api_message_id") or ""),
            abandoned.get("from_block_index"),
        )
        if not response or not isinstance(start, int):
            return []
        dropped = [key for key in self.pending if key[0] == response and key[1] >= start]
        text = "".join(self.pending.pop(key) for key in dropped)
        self.shown = max(0, self.shown - len(text))
        return [{"type": "retract", "text": text}] if text else []

    def _assistant(self, blocks: list[Any]) -> list[Event]:
        out: list[Event] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                streamed = bool(self.pending)
                text = str(block["text"])
                if not streamed:
                    text = BLOCK_BREAK + text if self.shown else text
                    self.shown += len(text)
                out.append({"type": "text", "text": text, "streamed": streamed})
            elif block.get("type") == "tool_use":
                name = str(block.get("name", "")).removeprefix(self.prefix)
                self.names[str(block.get("id"))] = name
                self.shown = 0
                out.append({"type": "tool_call", "id": str(block.get("id")), "name": name})
        self.pending.clear()
        return out

    def _tool_results(self, blocks: list[Any]) -> list[Event]:
        out: list[Event] = []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                call_id = str(block.get("tool_use_id"))
                out.append(
                    {
                        "type": "tool_result",
                        "id": call_id,
                        "name": self.names.get(call_id, ""),
                        "output": _content_text(block.get("content")),
                        "is_error": bool(block.get("is_error")),
                    }
                )
        return out

    def _result(self, obj: dict[str, Any]) -> list[Event]:
        self.ended = True
        usage = _dict(obj.get("usage"))
        context = sum(int(self.last_call.get(k) or 0) for k in _INPUT_KEYS.values())
        out: list[Event] = [
            {
                "type": "usage",
                "model": self.model,
                **{name: int(usage.get(key) or 0) for name, key in _INPUT_KEYS.items()},
                "output_tokens": int(usage.get("output_tokens") or 0),
                "context_tokens": context,
            }
        ]
        text = str(obj.get("result") or "")
        if obj.get("is_error") or obj.get("subtype") != "success":
            subtype = str(obj.get("subtype") or "")
            code = (
                "max_turns"
                if subtype == "error_max_turns"
                else self.api_error or classify(text or subtype)
            )
            out.append({"type": "error", "code": code, "message": ""})
        else:
            final: Event = {"type": "final", "text": text}
            if obj.get("structured_output") is not None:
                final["structured"] = obj["structured_output"]
            out.append(final)
        return out


class CodexStream:
    """JSONL events of `codex exec --json` → normalised events."""

    def __init__(self, structured: bool, model: str = "") -> None:
        self.structured = structured
        self.model = model
        self.last_text = ""
        self.last_error = ""
        self.ended = False

    def feed(self, obj: dict[str, Any]) -> list[Event]:
        kind = obj.get("type")
        item = _dict(obj.get("item"))
        if kind == "item.started" and item.get("type") == "mcp_tool_call":
            return [
                {"type": "tool_call", "id": str(item.get("id")), "name": str(item.get("tool", ""))}
            ]
        if kind == "item.completed":
            return self._completed(item)
        if kind == "error":
            self.last_error = str(obj.get("message", ""))
        elif kind == "turn.failed":
            self.ended = True
            message = str(_dict(obj.get("error")).get("message") or self.last_error)
            return [{"type": "error", "code": classify(message), "message": ""}]
        elif kind == "turn.completed":
            return self._completed_turn(_dict(obj.get("usage")))
        return []

    def _completed(self, item: dict[str, Any]) -> list[Event]:
        kind = item.get("type")
        if kind == "agent_message" and item.get("text"):
            self.last_text = str(item["text"])
            return [{"type": "text", "text": self.last_text}]
        if kind == "mcp_tool_call":
            return [
                {
                    "type": "tool_result",
                    "id": str(item.get("id")),
                    "name": str(item.get("tool", "")),
                    "output": _content_text(_dict(item.get("result")).get("content")),
                    "is_error": bool(item.get("error")) or item.get("status") == "failed",
                }
            ]
        if kind == "error":
            self.last_error = str(item.get("message", ""))
        return []

    def _completed_turn(self, usage: dict[str, Any]) -> list[Event]:
        self.ended = True
        out: list[Event] = [
            {
                "type": "usage",
                "model": self.model,
                "input_tokens": int(usage.get("input_tokens") or 0),
                "cache_read_tokens": int(usage.get("cached_input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
            }
        ]
        final: Event = {"type": "final", "text": self.last_text}
        if self.structured:
            try:
                final["structured"] = json.loads(self.last_text)
            except ValueError:
                return [*out, {"type": "error", "code": "failed", "message": ""}]
        return [*out, final]


# --- runs -----------------------------------------------------------------------------------


class Runtime:
    """What runs share: the configuration, the concurrency limits and the Codex catalog."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.slots = Slots(cfg.max_per_user, cfg.max_global)
        self.catalog = Catalog(cfg)

    def installed(self, backend: str) -> bool:
        return shutil.which(self.cfg.bins[backend], path=self.cfg.path) is not None

    def validate(self, req: RunRequest) -> None:
        """A Codex model outside the cleaned catalog would bring its own tool set."""
        if (
            req.backend == "codex"
            and req.model
            and self.installed("codex")
            and self.catalog.ensure()
        ):
            if req.model not in self.catalog.models:
                raise BadRequest("unknown Codex model")

    def models(self, backend: str) -> list[str] | None:
        """The model names a run accepts; None when the CLI checks the name itself."""
        if backend == "claude":
            return list(self.cfg.claude_models) or None
        if not self.installed("codex") or not self.catalog.ensure():
            return None
        return sorted(self.catalog.models)

    def run(self, req: RunRequest, disconnected: Callable[[], bool]) -> Iterator[Event]:
        """Events of one run; the CLI is killed on timeout or when `disconnected()` turns true."""
        cfg = self.cfg
        if sandbox_problem(cfg):
            yield {"type": "error", "code": "unsafe", "message": ""}
            return
        if not self.installed(req.backend):
            yield {"type": "error", "code": "missing", "message": ""}
            return
        if req.backend == "codex" and not self.catalog.ensure():
            yield {"type": "error", "code": "unsafe", "message": ""}
            return
        if not self.slots.acquire(req.user):
            yield {"type": "error", "code": "busy", "message": ""}
            return
        run_id = uuid.uuid4().hex[:12]
        started = time.monotonic()
        work = Path(tempfile.mkdtemp(prefix="run-", dir=cfg.tmp))
        result: dict[str, int | None] = {}
        directory: Path | None = None
        try:
            directory = prepare(cfg, req.user, req.backend)
            yield from self._execute(req, directory, work, disconnected, started, result)
        finally:
            reap_orphans()
            shutil.rmtree(work, ignore_errors=True)
            if directory is not None:
                tidy(directory, req.backend, cfg.keep_files)
            self.slots.release(req.user)
            log.info(
                "run %s backend=%s exit=%s duration=%.1fs",
                run_id, req.backend, result.get("exit"), time.monotonic() - started,
            )  # fmt: skip

    def _start(
        self, req: RunRequest, directory: Path, work: Path
    ) -> tuple[subprocess.Popen[bytes], Any]:
        cfg = self.cfg
        private_dir(work / "tmp")
        write_work_files(cfg, req, work, self.catalog.path)
        extra: dict[str, str] = {}
        stream: ClaudeStream | CodexStream
        if req.backend == "claude":
            args, stream = claude_args(cfg, req, work), ClaudeStream(cfg.mcp_name)
        else:
            args = codex_args(cfg, req, work, self.catalog.path)
            stream = CodexStream(req.json_schema is not None, req.model or "")
            if req.mcp_token:
                extra[MCP_TOKEN_ENV] = req.mcp_token
        env = child_env(cfg, directory, req.backend, extra, tmp=work / "tmp")
        args, env = sandboxed(cfg, args, env, directory, work, cwd=work)
        proc = spawn(
            args,
            cwd=work,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        return proc, stream

    def _execute(
        self,
        req: RunRequest,
        directory: Path,
        work: Path,
        disconnected: Callable[[], bool],
        started: float,
        result: dict[str, int | None],
    ) -> Iterator[Event]:
        try:
            proc, stream = self._start(req, directory, work)
        except OSError:
            yield {"type": "error", "code": "missing", "message": ""}
            return
        assert proc.stdout is not None
        timed_out, stop = threading.Event(), threading.Event()
        watcher = threading.Thread(
            target=_watch,
            args=(proc, started + req.timeout_s, disconnected, timed_out, stop),
            daemon=True,
        )
        try:
            _feed_stdin(proc, req.prompt)
            watcher.start()
            for raw in proc.stdout:
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    yield from stream.feed(obj)
            proc.wait()
        finally:
            stop.set()
            if watcher.is_alive():
                watcher.join()
            release(proc)
            result["exit"] = proc.returncode
        if timed_out.is_set():
            yield {"type": "error", "code": "timeout", "message": ""}
        elif not stream.ended:
            last = getattr(stream, "last_error", "")
            yield {"type": "error", "code": classify(last) if last else "failed", "message": ""}


def _feed_stdin(proc: subprocess.Popen[bytes], prompt: str) -> None:
    assert proc.stdin is not None
    with contextlib.suppress(BrokenPipeError):
        proc.stdin.write(prompt.encode())
        proc.stdin.close()


def _watch(
    proc: subprocess.Popen[bytes],
    deadline: float,
    disconnected: Callable[[], bool],
    timed_out: threading.Event,
    stop: threading.Event,
) -> None:
    while not stop.wait(0.2):
        if time.monotonic() > deadline:
            timed_out.set()
            kill(proc)
            return
        if disconnected():
            kill(proc)
            return
