"""Forward-auth identity from a trusted reverse proxy."""

from dataclasses import dataclass, field
from ipaddress import IPv4Network, IPv6Network, ip_address
from typing import Protocol

EXEMPT_PREFIXES = (
    "/api/v1/agent",
    "/cli/",
    "/oauth/token",
    "/oauth/register",
    "/.well-known/",
    "/mcp",
)


class _Client(Protocol):
    host: str


class _Request(Protocol):
    @property
    def client(self) -> _Client | None: ...

    @property
    def headers(self) -> "_Headers": ...


class _Headers(Protocol):
    def get(self, key: str, default: None = None, /) -> str | None: ...


@dataclass(frozen=True)
class ForwardAuthHeaders:
    """Header names per field, first match wins; aliases keep an app's older names working."""

    email: tuple[str, ...] = ("X-Forwarded-Email",)
    user: tuple[str, ...] = ("X-Forwarded-User",)
    groups: tuple[str, ...] = ("X-Forwarded-Groups",)


@dataclass
class ForwardAuthConfig:
    enabled: bool = False
    trusted_proxies: list[IPv4Network | IPv6Network] = field(default_factory=list)
    headers: ForwardAuthHeaders = field(default_factory=ForwardAuthHeaders)


@dataclass(frozen=True)
class Identity:
    email: str | None
    user: str | None
    groups: tuple[str, ...] = ()


def is_exempt(path: str) -> bool:
    """True when `path` is an exempt prefix or below it, matched on whole segments.

    Paths with `.`, `..` or empty segments (a single trailing slash aside) are never exempt.
    """
    segments = path.split("/")
    if segments[0] != "" or any(s in ("", ".", "..") for s in segments[1:-1]):
        return False
    if segments[-1] in (".", ".."):
        return False
    return any(
        path == (base := prefix.rstrip("/")) or path.startswith(base + "/")
        for prefix in EXEMPT_PREFIXES
    )


def _first(request: _Request, names: tuple[str, ...]) -> str | None:
    for name in names:
        value = request.headers.get(name)
        if value:
            return value.strip()
    return None


def _trusted(request: _Request, cfg: ForwardAuthConfig) -> bool:
    if request.client is None:
        return False
    try:
        peer = ip_address(request.client.host)
    except ValueError:
        return False
    return any(peer in net for net in cfg.trusted_proxies if peer.version == net.version)


def forwarded_identity(request: _Request, cfg: ForwardAuthConfig) -> Identity | None:
    """Identity from the proxy headers; None unless the direct peer is a trusted proxy.

    `request.client` must be the socket peer: run uvicorn with `--no-proxy-headers` (or
    `forwarded_allow_ips` limited to the proxy), or a caller can forge it via X-Forwarded-For.
    """
    if not cfg.enabled or not _trusted(request, cfg):
        return None
    email = _first(request, cfg.headers.email)
    user = _first(request, cfg.headers.user)
    if not email and not user:
        return None
    raw_groups = _first(request, cfg.headers.groups) or ""
    groups = tuple(g.strip() for g in raw_groups.split(",") if g.strip())
    return Identity(email=email, user=user, groups=groups)
