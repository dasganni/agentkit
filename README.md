# agentkit

Shared library for the apps: provider and model catalog, effort mapping, detached runs, history
budget, titles, the claude/codex sidecar, the agent API router, forward-auth, CLI distribution and
the generic Go CLI. Python package `agentkit` (src layout, Python 3.13, uv) and Go module `cli/`.
Rules and gate: see AGENTS.md.

## Modules and interfaces

All under `src/agentkit/`; one pytest file per module in `tests/`.

### effort.py

- `Effort = Literal["auto", "minimal", "low", "medium", "high", "xhigh", "max"]`, `LEVELS`
- `nearest(wanted, supported) -> Effort | None`: the supported level closest to `wanted`, ties
  downward; `"auto"` stays `"auto"`, `None` when nothing is supported.

### catalog.py

- `ModelInfo` (frozen dataclass): `id, label, provider, efforts, thinking, input_per_mtok,
  output_per_mtok, cache_read_per_mtok, cache_write_per_mtok` (`Decimal | None`), `context`.
- `FALLBACK: dict[kind, list[ModelInfo]]`: static table with prices.
- `await list_models(provider, *, sidecar=None, transport=None) -> list[ModelInfo]`: live list from
  Anthropic, OpenRouter/OpenAI-compatible or the sidecar, merged with the `FALLBACK` prices;
  `FALLBACK[kind]` on any error or an empty list.
- `cost(model, usage) -> Decimal | None`: price of a `Usage`; `None` without prices.

### providers.py

- `ProviderKind = Literal["anthropic", "openai", "claude", "codex"]`
- `ProviderConfig(kind, api_key: SecretStr | None, base_url: str | None)`
- `RunOptions(model, effort="auto", max_tokens=None)`
- `build_chat_model(cfg, opts, *, model_info) -> BaseChatModel` for `anthropic` (ChatAnthropic with
  `output_config.effort`, adaptive thinking, prompt `cache_control`) and `openai` (ChatOpenAI with
  `base_url`; `reasoning {effort}` on OpenRouter, `reasoning_effort` otherwise). `claude` and
  `codex` raise `ValueError`: they run through the sidecar.
- `Usage(input, output, cache_read, cache_write)`, `usage_from_message(msg)`,
  `usage_from_sidecar(event)`.

### history.py

- `estimate_tokens(text)`, `trim_to_budget(messages, budget)` (newest messages that fit),
  `should_compact(messages, limit=COMPACT_LIMIT)`, `summary_message(summary)`, `COMPACT_PROMPT`,
  `COMPACT_LIMIT`, `CLI_HISTORY_TOKENS`. A message is `{"role": ..., "text": ...}`.

### runs.py

- `RunRegistry(keep_seconds=60)`: `start(key, source) -> Run` (`source` is a callable returning an
  async iterator of events), `attach(key, start=0)` (async iterator of events from an offset),
  `cancel(key) -> bool` (awaits the end), `cancel_all()`, `get(key)`, `active(key)`. A run outlives
  the connection that started it and stays attachable for `keep_seconds` after it ended.

### titles.py

- `make_title(model, message) -> str`: title from a small chat model; `fallback_title(message)` when
  the call fails. `summarise(model, messages) -> str | None`: history summary with `COMPACT_PROMPT`.
  Prompts and replies are never logged.

### sidecar_client.py

`SidecarClient(socket_path, secret="")` talks to the sidecar over its unix socket. `kind` is
`"claude"` or `"codex"`; `user` is a keyword argument (the sidecar's per-user key).

- `run(kind, prompt, *, user, model, effort, system, messages, mcp_config, allowed_tools,
  json_schema, max_turns=30, timeout=180)`: async iterator of NDJSON event dicts; closing it makes
  the sidecar kill the CLI.
- `login(kind, *, user)` returns `{url, code?}`, `login_code(kind, code, *, user)` returns
  `{connected}`, `status(kind, *, user)` returns `{connected, account_hint?}`,
  `logout(kind, *, user)`, `models(kind)` returns model names or `None`.
- Errors: `SidecarUnavailable`, `SidecarBusy` (a subclass), `SidecarRefused`.

### sidecar (`python -m agentkit.sidecar`)

HTTP on a unix socket; every request carries `X-Agent-Secret`. Routes: `POST /run` (NDJSON),
`POST /login/start`, `POST /login/code`, `GET /login/status`, `POST /logout`,
`GET /models?backend=`, `GET /healthz`. Modules: `server.py` (HTTP, limits), `runner.py` (claude and
codex argv, effort flags, stream parsing, usage events), `login.py` (pty sign-in), `sandbox.py`
(Landlock, plus bubblewrap when present). Standard library only.

Environment (the full list is the `agentkit.sidecar.server` docstring):

| Variable | Meaning (default) |
|---|---|
| `AGENT_SOCKET` | socket path (`/run/agent/agent.sock`); `agent.secret` is written next to it when `AGENT_SECRET` is unset |
| `AGENT_SECRET` | shared secret (fresh at each start) |
| `AGENT_HOME` | directory of the CLI logins (`/home/agent`); per user `<home>/users/<key>/<backend>` |
| `AGENT_MCP_NAME` | name the CLIs see the app MCP server under (`app`) |
| `AGENT_CLAUDE_BIN`, `AGENT_CODEX_BIN` | CLI executables (`claude`, `codex`) |
| `AGENT_CLAUDE_MODELS` | comma list of model names `/models` reports for Claude Code (none) |
| `AGENT_MAX_RUNS`, `AGENT_MAX_PER_USER`, `AGENT_MAX_THREADS` | concurrency limits (3, 1, 32) |
| `AGENT_REQUIRE_SANDBOX` | `0` accepts a kernel without Landlock ABI 3 (`1`: refuse to run) |
| `AGENT_USERNS`, `AGENT_BWRAP` | bubblewrap layer `auto`/`on`/`off` (`auto`), executable (`bwrap`) |
| `AGENT_RO_PATHS` | more read-only paths for the CLIs, colon separated |

The sidecar image installs agentkit and starts `python -m agentkit.sidecar`.

### agentapi.py

- `PROTOCOL = 1`, `PROTOCOL_MIN = 1`, `PROTOCOL_HEADER = "Agent-Protocol"`; a request with a
  protocol outside `PROTOCOL_MIN..PROTOCOL` gets 426.
- `Caller(user_id, role: "read" | "write" | "admin", agent: str | None)`,
  `ToolResult(text, images=[])`, `ToolError(status, message, field=None)` (400/403/404/409/422/500).
- `ToolRegistry` protocol: `schemas(caller) -> list[dict]` (`name, description, ops?, params` JSON
  schema, `write`) and `async call(name, params, caller) -> ToolResult`.
- `agent_router(registry, authenticate, revoke, *, prefix="/api/v1/agent", resource_metadata=None)`:
  `GET {prefix}/tools`, `POST {prefix}/tools/{name}`, `DELETE {prefix}/token`.
  `authenticate(request) -> Caller` raises `HTTPException(401)` for a bad token (the router adds the
  `WWW-Authenticate` challenge, with `resource_metadata(request)` when given; the CLI exits 3).
  `revoke(request, caller)` revokes the caller's own token.
- `fence(text, source) -> str` wraps untrusted text as `{"untrusted_text": ...}`; use it for bank,
  document, import and tool-output text.

### forward_auth.py

- `ForwardAuthHeaders(email, user, groups)` (header-name tuples, first match wins, aliases keep
  older names working), `ForwardAuthConfig(enabled, trusted_proxies, headers)`,
  `Identity(email, user, groups)`.
- `forwarded_identity(request, cfg) -> Identity | None`: `None` unless forward-auth is enabled, the
  direct peer is in `trusted_proxies` and an email or user header is present.
- `EXEMPT_PREFIXES`, `is_exempt(path)`: paths the forward-auth gate lets through
  (`/api/v1/agent`, `/cli/`, `/oauth/token`, `/oauth/register`, `/.well-known/`, `/mcp`).

### cli_dist.py

- `cli_router(dist_dir) -> APIRouter` with `GET /cli/version`, `GET /cli/{os}-{arch}` (gzip) and
  `GET /cli/{os}-{arch}.sha256`; 404 while the directory or file is missing (dev).

### skillcheck.py

- `check_skills(skill_dirs, schemas, app) -> list[str]` lists every `<app> <tool> [<op>] --<flag>`
  in `SKILL.md`/`reference.md` that the tool schema lacks; built-in commands and global flags pass.
- `scan_tokens(path) -> list[str]`: token-looking strings (`tt_`, `rw_`, `fit_`, `tto_` prefixes,
  Bearer values) in a file or tree. Use both in a test in the app.

## Go CLI (`cli/`)

One generic binary per app: name, environment prefix and default URL are set with `-ldflags`
(`main.app`, `main.envPrefix`, `main.defaultURL`); the tool schema `tools.json` is embedded at
build. Commands: `login`, `logout`, `status`, `token revoke`, `version`, `tools`, one command per
schema tool (`<app> <tool> [<op>] --<field>`), global `--json`. Exit codes: 1 error, 2 usage, 3 not
signed in or no server. Login is OAuth with dynamic client registration, PKCE S256 and a loopback
callback; tokens refresh one minute early and once after a 401. The config file (0600, user config
dir) holds the URL and token; `<PREFIX>_URL` and `<PREFIX>_TOKEN` override it. Every request sends
`Agent-Protocol`.

## Adopting agentkit in an app

### 1. Dependency

Pin the wheel of a release with its hash (uv lock or pip-compile `--generate-hashes`):

```
agentkit @ https://github.com/dasganni/agentkit/releases/download/vX.Y.Z/agentkit-X.Y.Z-py3-none-any.whl
```

### 2. Mount the routers

```python
import os
from pathlib import Path

from fastapi import FastAPI

from agentkit.agentapi import agent_router
from agentkit.cli_dist import cli_router

app = FastAPI()
app.include_router(agent_router(MyRegistry(), bearer_auth, revoke_own_token))  # /api/v1/agent
app.include_router(cli_router(Path(os.environ.get("MYAPP_CLI_DIST", "/srv/cli-dist"))))  # /cli
```

`MyRegistry` is an adapter over the app's tools implementing `ToolRegistry`; `bearer_auth` resolves
the bearer token to a `Caller`; `revoke_own_token` revokes the caller's token. Let the forward-auth
gate skip `EXEMPT_PREFIXES` so the CLI and its OAuth endpoints stay reachable. Accept the OAuth
resource `<public>/api/v1/agent` next to `/mcp`.

### 3. Schema snapshot and CLI build

The app dumps all of its tools, admin ones included (the server filters per caller), for example
`python -m app.agent_schema > tools.json`. The Dockerfile builds the CLI in a Go stage from the
release tarball of the pinned tag and copies it to `/srv/cli-dist`:

```dockerfile
FROM golang:1.26.9-alpine AS cli
ADD --checksum=sha256:<tarball sha256> \
    https://github.com/dasganni/agentkit/archive/refs/tags/vX.Y.Z.tar.gz /agentkit.tar.gz
COPY tools.json /tools.json
RUN tar -xzf /agentkit.tar.gz -C / && cd /agentkit-X.Y.Z \
    && sh scripts/cli-dist.sh <app> <APP> /tools.json /dist X.Y.Z

FROM python:3.13-slim
WORKDIR /srv
COPY --from=cli /dist /srv/cli-dist
ENV MYAPP_CLI_DIST=/srv/cli-dist
```

`scripts/cli-dist.sh APP ENV_PREFIX SCHEMA OUT VERSION` writes `<os>-<arch>` (gzip) and
`<os>-<arch>.sha256` for linux, darwin and windows on amd64 and arm64, plus a `version` file.

### 4. Plugin shim

Copy `integrations/shim.sh` to the plugin's `bin/<app>` (mode 755) and `integrations/hooks.json` to
the plugin, replacing `@APP@` with the app name in both (the hook runs `bin/<app> --warm` in the
background at SessionStart). The shim reads the server URL from `<APP>_URL` or the CLI config file,
downloads `/cli/<os>-<arch>`, verifies the sha256, caches it per version under
`$XDG_CACHE_HOME/<app>/cli` (two versions kept), re-checks `/cli/version` hourly and execs the
binary. Without a server and cache it exits 3. Skills call `<app> <tool> ...`; test them with
`check_skills` and `scan_tokens`.

### 5. Sidecar

Run the sidecar image next to the app with a volume shared for the socket only. The app creates
`SidecarClient(socket, secret)` with the same `AGENT_SECRET` (or reads `agent.secret` from the
socket directory). Set the environment variables above as needed.

## Versioning

Releases are git tags `vX.Y.Z`; the wheel and the source tarball of a tag are what apps pin. The
agent protocol is a separate integer: `PROTOCOL` in `agentapi.py` and `Protocol` in
`cli/internal/version`. The CLI sends it in `Agent-Protocol`; a server answers 426 outside
`PROTOCOL_MIN..PROTOCOL`, which tells the user to update the CLI. A breaking protocol change raises
`PROTOCOL`; `PROTOCOL_MIN` stays as long as old CLIs are still served.
