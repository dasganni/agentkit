from ipaddress import ip_network
from types import SimpleNamespace

from agentkit.forward_auth import (
    EXEMPT_PREFIXES,
    ForwardAuthConfig,
    ForwardAuthHeaders,
    forwarded_identity,
    is_exempt,
)


class Headers(dict):
    def get(self, key, default=None, /):  # type: ignore[no-untyped-def]
        return super().get(key, default)


def req(host: str | None, **headers: str):  # type: ignore[no-untyped-def]
    client = SimpleNamespace(host=host) if host else None
    return SimpleNamespace(client=client, headers=Headers(headers))


def cfg(**kw) -> ForwardAuthConfig:  # type: ignore[no-untyped-def]
    return ForwardAuthConfig(enabled=True, trusted_proxies=[ip_network("10.0.0.0/8")], **kw)


def test_untrusted_peer_ignored() -> None:
    r = req("203.0.113.5", **{"X-Forwarded-Email": "a@b.c", "X-Forwarded-For": "10.0.0.1"})
    assert forwarded_identity(r, cfg()) is None


def test_trusted_peer_identity() -> None:
    r = req(
        "10.1.2.3",
        **{"X-Forwarded-Email": "a@b.c", "X-Forwarded-User": "ann", "X-Forwarded-Groups": "x, y"},
    )
    ident = forwarded_identity(r, cfg())
    assert ident is not None
    assert (ident.email, ident.user, ident.groups) == ("a@b.c", "ann", ("x", "y"))


def test_disabled_or_no_headers() -> None:
    assert forwarded_identity(req("10.1.2.3"), cfg()) is None
    off = ForwardAuthConfig(enabled=False, trusted_proxies=[ip_network("10.0.0.0/8")])
    assert forwarded_identity(req("10.1.2.3", **{"X-Forwarded-Email": "a@b.c"}), off) is None
    assert forwarded_identity(req(None, **{"X-Forwarded-Email": "a@b.c"}), cfg()) is None


def test_alias_header() -> None:
    headers = ForwardAuthHeaders(email=("X-Forwarded-Email", "Remote-Email"))
    ident = forwarded_identity(req("10.1.2.3", **{"Remote-Email": "a@b.c"}), cfg(headers=headers))
    assert ident is not None and ident.email == "a@b.c"


def test_exempt_prefixes() -> None:
    assert "/mcp" in EXEMPT_PREFIXES
    for path in ("/api/v1/agent/tools", "/cli/version", "/oauth/token", "/.well-known/x", "/mcp"):
        assert is_exempt(path)
    assert not is_exempt("/api/v1/other")
