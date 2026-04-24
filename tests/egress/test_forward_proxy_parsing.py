"""Pure-function tests for the forward-proxy HTTP parser.

Kept separate from test_forward_proxy.py (which would exercise the
full asyncio Server loop) because these helpers are pure synchronous
code with no I/O — catching a regex regression here is far cheaper
than spinning up an asyncio server.
"""

from __future__ import annotations

import asyncio

import pytest

from rolemesh.egress.forward_proxy import (
    ParsedRequest,
    _extract_plain_http_target,
    _read_request,
    _rewrite_request_line,
    _split_host_port,
)


class TestSplitHostPort:
    def test_host_with_explicit_port(self) -> None:
        assert _split_host_port("github.com:8080", default_port=443) == ("github.com", 8080)

    def test_host_without_port_uses_default(self) -> None:
        assert _split_host_port("github.com", default_port=443) == ("github.com", 443)

    def test_empty_target_raises(self) -> None:
        with pytest.raises(ValueError):
            _split_host_port("", default_port=443)

    def test_non_numeric_port_raises(self) -> None:
        with pytest.raises(ValueError):
            _split_host_port("github.com:abc", default_port=443)

    def test_ipv6_brackets_stripped(self) -> None:
        assert _split_host_port("[::1]:443", default_port=443) == ("::1", 443)


class TestExtractPlainHttpTarget:
    def test_absolute_uri_parsed(self) -> None:
        req = ParsedRequest(
            method="GET", target="http://github.com/path", headers={}, raw=b""
        )
        host, port, path = _extract_plain_http_target(req)
        assert (host, port, path) == ("github.com", 80, "/path")

    def test_absolute_uri_https_default_port(self) -> None:
        req = ParsedRequest(
            method="GET", target="https://github.com/api", headers={}, raw=b""
        )
        _host, port, _path = _extract_plain_http_target(req)
        assert port == 443

    def test_absolute_uri_explicit_port(self) -> None:
        req = ParsedRequest(
            method="GET", target="http://github.com:8080/", headers={}, raw=b""
        )
        _host, port, _path = _extract_plain_http_target(req)
        assert port == 8080

    def test_falls_back_to_host_header(self) -> None:
        req = ParsedRequest(
            method="GET",
            target="/api",
            headers={"host": "github.com"},
            raw=b"",
        )
        host, port, path = _extract_plain_http_target(req)
        assert (host, port, path) == ("github.com", 80, "/api")

    def test_missing_host_returns_empty(self) -> None:
        req = ParsedRequest(method="GET", target="/api", headers={}, raw=b"")
        host, _, _ = _extract_plain_http_target(req)
        assert host == ""


class TestReadRequestErrorPaths:
    """_read_request raises a specific set of exceptions on bad input.

    Regression: pre-fix version didn't propagate LimitOverrunError or
    IncompleteReadError to callers, so _handle_client's except tuple
    missed them and leaked StreamWriter instances on oversized-header
    slow-loris input and mid-header disconnects. These tests pin the
    raise-types so any future refactor of StreamReader semantics gets
    caught.
    """

    @pytest.mark.asyncio
    async def test_oversized_header_block_raises_limit_overrun(self) -> None:
        """readuntil's default 64 KiB buffer: client sends 128 KiB of
        garbage without CRLFCRLF → LimitOverrunError before our own
        _MAX_HEADER_BYTES guard can even fire."""
        reader = asyncio.StreamReader()
        # No terminator — floods readuntil's buffer.
        reader.feed_data(b"X" * (128 * 1024))
        reader.feed_eof()
        with pytest.raises(asyncio.LimitOverrunError):
            await _read_request(reader)

    @pytest.mark.asyncio
    async def test_mid_headers_close_raises_incomplete_read(self) -> None:
        """Client sends a partial request then closes. readuntil
        raises IncompleteReadError (EOFError subclass, NOT
        ConnectionError) — the original except tuple would have leaked."""
        reader = asyncio.StreamReader()
        reader.feed_data(b"GET /path HTTP/1.1\r\nHost: example\r\n")  # no CRLFCRLF
        reader.feed_eof()
        with pytest.raises(asyncio.IncompleteReadError):
            await _read_request(reader)


class TestRewriteRequestLine:
    def test_absolute_uri_becomes_origin_form(self) -> None:
        raw = b"GET http://github.com/path HTTP/1.1\r\nHost: github.com\r\n\r\n"
        rewritten = _rewrite_request_line(raw, "GET", "/path")
        assert rewritten.startswith(b"GET /path HTTP/1.1\r\n")
        # Headers must be preserved intact when Host-rewrite is not requested.
        assert b"Host: github.com\r\n" in rewritten

    def test_malformed_raw_passed_through(self) -> None:
        """Defensive — malformed bytes should not crash the proxy."""
        raw = b"garbage"
        assert _rewrite_request_line(raw, "GET", "/x") == raw

    def test_host_header_rewrite_defeats_smuggling(self) -> None:
        """Regression for P2 finding: client sends absolute-URI to
        ``allowed.com`` but smuggles ``Host: forbidden-internal.corp``.
        Without the rewrite, upstream routes based on the smuggled
        Host. With the rewrite, upstream sees the allowed host."""
        raw = (
            b"GET http://allowed.com/path HTTP/1.1\r\n"
            b"Host: forbidden-internal.corp\r\n"
            b"User-Agent: probe\r\n"
            b"\r\n"
        )
        rewritten = _rewrite_request_line(
            raw, "GET", "/path", host_header_value="allowed.com"
        )
        assert b"Host: allowed.com\r\n" in rewritten
        assert b"forbidden-internal.corp" not in rewritten
        # Other headers untouched.
        assert b"User-Agent: probe\r\n" in rewritten

    def test_host_rewrite_case_insensitive(self) -> None:
        """Host is case-insensitive per RFC; must match ``host:``, ``Host:``,
        ``HOST:`` alike. A client using ``HOST`` in uppercase to evade our
        regex would otherwise still smuggle."""
        raw = (
            b"POST http://allowed.com/ HTTP/1.1\r\n"
            b"HOST: evil.corp\r\n"
            b"\r\n"
        )
        rewritten = _rewrite_request_line(
            raw, "POST", "/", host_header_value="allowed.com"
        )
        assert b"evil.corp" not in rewritten
        assert b"Host: allowed.com\r\n" in rewritten

    def test_host_rewrite_inserts_when_missing(self) -> None:
        """HTTP/1.0 requests may omit Host entirely. We still inject the
        decided host so the upstream has unambiguous routing."""
        raw = b"GET http://allowed.com/ HTTP/1.1\r\nUser-Agent: x\r\n\r\n"
        rewritten = _rewrite_request_line(
            raw, "GET", "/", host_header_value="allowed.com"
        )
        assert b"Host: allowed.com\r\n" in rewritten

    def test_host_rewrite_preserves_port_when_nondefault(self) -> None:
        """When the target port isn't 80/443 the Host header MUST include
        it — otherwise upstream virtual-host routing breaks for services
        listening on ``8080`` etc."""
        raw = b"GET http://allowed.com:8080/ HTTP/1.1\r\nHost: evil.corp\r\n\r\n"
        # Caller passes host:port as it decided from the absolute URI.
        rewritten = _rewrite_request_line(
            raw, "GET", "/", host_header_value="allowed.com:8080"
        )
        assert b"Host: allowed.com:8080\r\n" in rewritten
