"""Pure-function tests for the forward-proxy HTTP parser.

Kept separate from test_forward_proxy.py (which would exercise the
full asyncio Server loop) because these helpers are pure synchronous
code with no I/O — catching a regex regression here is far cheaper
than spinning up an asyncio server.
"""

from __future__ import annotations

import pytest

from rolemesh.egress.forward_proxy import (
    ParsedRequest,
    _extract_plain_http_target,
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


class TestRewriteRequestLine:
    def test_absolute_uri_becomes_origin_form(self) -> None:
        raw = b"GET http://github.com/path HTTP/1.1\r\nHost: github.com\r\n\r\n"
        rewritten = _rewrite_request_line(raw, "GET", "/path")
        assert rewritten.startswith(b"GET /path HTTP/1.1\r\n")
        # Headers must be preserved intact.
        assert b"Host: github.com\r\n" in rewritten

    def test_malformed_raw_passed_through(self) -> None:
        """Defensive — malformed bytes should not crash the proxy."""
        raw = b"garbage"
        assert _rewrite_request_line(raw, "GET", "/x") == raw
