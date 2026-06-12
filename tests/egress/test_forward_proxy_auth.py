"""Behavioural tests for the forward proxy's 407 auth challenge.

Drives ``_handle_connect`` / ``_handle_plain_http`` with a fake writer so
the full asyncio Server loop isn't needed. Pins the token-only identity
contract: a CONNECT (or plain HTTP) carrying no/invalid identity token
gets ``407 Proxy Authentication Required`` with ``Proxy-Authenticate:
Basic`` — never a tunnel, never a safety call.
"""

from __future__ import annotations

import base64

import pytest

from rolemesh.egress.forward_proxy import ForwardProxy, ParsedRequest
from rolemesh.egress.token_identity import Identity, TokenAuthority

pytestmark = pytest.mark.asyncio

_SECRET = "forward-auth-test-secret-16+chars"


class _FakeWriter:
    """Captures everything written; satisfies the StreamWriter surface
    the response helpers touch."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buf.extend(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass

    def get_extra_info(self, _name: str) -> tuple[str, int]:
        return ("172.30.0.9", 40000)

    @property
    def text(self) -> str:
        return self.buf.decode("latin-1")


class _BoomSafety:
    """Safety caller that must never be invoked on the 407 path."""

    async def decide(self, **_kw: object) -> object:  # pragma: no cover
        raise AssertionError("safety pipeline must not run without identity")


def _connect_request(token: str | None) -> ParsedRequest:
    headers: dict[str, str] = {}
    if token is not None:
        cred = base64.b64encode(f"job:{token}".encode()).decode()
        headers["proxy-authorization"] = f"Basic {cred}"
    return ParsedRequest(method="CONNECT", target="example.com:443", headers=headers, raw=b"")


async def test_connect_without_token_gets_407() -> None:
    proxy = ForwardProxy(
        safety_caller=_BoomSafety(),  # type: ignore[arg-type]
        token_authority=TokenAuthority(secret=_SECRET, ttl_seconds=3600),
    )
    writer = _FakeWriter()
    await proxy._handle_connect(
        reader=None,  # type: ignore[arg-type]
        writer=writer,  # type: ignore[arg-type]
        source_ip="172.30.0.9",
        identity=None,
        parsed=_connect_request(token=None),
    )
    assert "407 Proxy Authentication Required" in writer.text
    assert "Proxy-Authenticate: Basic" in writer.text
    assert writer.closed


async def test_connect_with_invalid_token_gets_407() -> None:
    authority = TokenAuthority(secret=_SECRET, ttl_seconds=3600)
    proxy = ForwardProxy(
        safety_caller=_BoomSafety(),  # type: ignore[arg-type]
        token_authority=authority,
    )
    # A real token signed with a different secret won't verify here.
    other = TokenAuthority(secret="a-totally-different-secret", ttl_seconds=3600)
    bad = other.mint(Identity("t", "c", "u", "v", "j", "n"))

    writer = _FakeWriter()
    # _handle_client would set identity=None for an unverifiable token;
    # emulate that by passing identity=None with the bad token present.
    await proxy._handle_connect(
        reader=None,  # type: ignore[arg-type]
        writer=writer,  # type: ignore[arg-type]
        source_ip="172.30.0.9",
        identity=authority.verify(bad),  # -> None
        parsed=_connect_request(token=bad),
    )
    assert "407 Proxy Authentication Required" in writer.text
