"""HTTP forward proxy — CONNECT tunnel + plain HTTP (EC-2).

The forward proxy is the enforcement point for every TCP/HTTP(S)
egress attempt other than the reverse-proxy LLM/MCP calls. Agents
inherit ``HTTP_PROXY`` / ``HTTPS_PROXY`` pointing at this listener
(set by the orchestrator in container/runner.py), so urllib, httpx,
requests, curl, wget, pip, and git all route through here without
any per-client configuration.

Flow:

    client ─(TCP)─> forward proxy (this module) ─(decision)─> Safety
                                                    │
                                                    ▼
                                           block: 403
                                           allow: open upstream TCP
                                                  splice bytes both ways

TLS is end-to-end between client and upstream — the gateway never
sees cleartext. SNI/host is extracted from the CONNECT request line
(HTTPS) or the Host header (plain HTTP). TLS interception is
explicitly V2+.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rolemesh.core.logger import get_logger

from .safety_call import EgressRequest

if TYPE_CHECKING:
    from .identity import IdentityResolver
    from .safety_call import EgressSafetyCaller

logger = get_logger()


# Bytes the upstream read/write splice copies in one go. 16 KiB is the
# typical TCP buffer size on Linux; larger chunks increase latency
# asymmetry between upstream and downstream without improving
# throughput on a single flow. Kept as a module constant so benchmarks
# can dial it.
_BUFFER_SIZE = 16 * 1024

# Upper bound on the initial HTTP request line + headers we're willing
# to read before refusing. A sane client sends the CONNECT line in
# under a KiB; an 8 KiB cap still accommodates the longest Chrome-
# style CONNECT we could plausibly see and prevents slow-loris reads
# from consuming unbounded memory.
_MAX_HEADER_BYTES = 8 * 1024

# Deadline on the handshake read. An idle client that connects and
# never sends bytes should not hold a file descriptor forever.
_HEADER_READ_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class ParsedRequest:
    """First-line + headers view of an incoming forward-proxy request."""

    method: str
    target: str  # "host:port" for CONNECT; absolute URL or path for HTTP
    headers: dict[str, str]
    raw: bytes  # verbatim bytes — used to replay non-CONNECT requests upstream


class ForwardProxy:
    """CONNECT + plain HTTP forward proxy.

    Thin wrapper around ``asyncio.start_server``; kept as a class only
    so the caller can stash the state it needs (identity resolver,
    safety caller) without reaching for module globals.
    """

    def __init__(
        self,
        *,
        identity_resolver: IdentityResolver,
        safety_caller: EgressSafetyCaller,
    ) -> None:
        self._identity = identity_resolver
        self._safety = safety_caller

    async def serve(self, host: str, port: int) -> asyncio.Server:
        """Bind and return the running server. Caller owns shutdown via
        ``server.close() + await server.wait_closed()``."""
        server = await asyncio.start_server(self._handle_client, host=host, port=port)
        sockets = server.sockets or []
        bound = ", ".join(str(s.getsockname()) for s in sockets)
        logger.info("forward proxy listening", bind=bound)
        return server

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Per-connection handler. Owns the lifecycle of a single client."""
        peer = writer.get_extra_info("peername") or ("?", 0)
        source_ip = peer[0] if peer else "?"
        try:
            parsed = await _read_request(reader)
        except (TimeoutError, ConnectionError, ValueError) as exc:
            logger.debug("forward proxy: bad initial request", source_ip=source_ip, error=str(exc))
            await _close(writer)
            return

        identity = self._identity.resolve(source_ip)
        if parsed.method == "CONNECT":
            await self._handle_connect(reader, writer, source_ip, identity, parsed)
        else:
            await self._handle_plain_http(reader, writer, source_ip, identity, parsed)

    async def _handle_connect(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        source_ip: str,
        identity: object | None,
        parsed: ParsedRequest,
    ) -> None:
        try:
            host, port = _split_host_port(parsed.target, default_port=443)
        except ValueError:
            await _respond(writer, 400, "Invalid CONNECT target")
            return

        decision = await self._safety.decide(
            identity=identity,  # type: ignore[arg-type]
            request=EgressRequest(host=host, port=port, mode="forward", method="CONNECT"),
        )
        if decision.action == "block":
            logger.info(
                "forward proxy: CONNECT blocked",
                source_ip=source_ip,
                host=host,
                port=port,
                reason=decision.reason,
            )
            await _respond(
                writer,
                403,
                "Forbidden",
                extra_headers={"X-Egress-Reason": decision.reason[:200]},
            )
            return

        try:
            up_reader, up_writer = await asyncio.open_connection(host, port)
        except (TimeoutError, OSError) as exc:
            logger.warning(
                "forward proxy: upstream connect failed",
                source_ip=source_ip,
                host=host,
                port=port,
                error=str(exc),
            )
            await _respond(writer, 502, "Bad Gateway")
            return

        # IMPORTANT: do NOT use ``_respond`` here — that helper closes the
        # writer after flushing, which is right for error paths but
        # wrong for CONNECT. After a 200 reply the TCP connection MUST
        # stay open so the pipe can splice client↔upstream bytes.
        await _write_status_line_only(writer, 200, "Connection Established")

        # Bidirectional pipe. Cancelling one leg terminates the other so
        # a dead upstream doesn't keep the client's file descriptor
        # pinned — matches the standard squid/envoy half-close handling.
        await _pipe(reader, up_writer, writer, up_reader)

    async def _handle_plain_http(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        source_ip: str,
        identity: object | None,
        parsed: ParsedRequest,
    ) -> None:
        """Plain HTTP (non-CONNECT) forward.

        Target host comes from either the absolute-URI in the request
        line (``GET http://github.com/...``) or the Host header. We
        prefer the absolute URI when present because it's what RFC 7230
        §5.3.2 mandates for HTTP-proxy requests.
        """
        host, port, path = _extract_plain_http_target(parsed)
        if not host:
            await _respond(writer, 400, "Missing target host")
            return

        decision = await self._safety.decide(
            identity=identity,  # type: ignore[arg-type]
            request=EgressRequest(
                host=host, port=port, mode="forward", method=parsed.method
            ),
        )
        if decision.action == "block":
            logger.info(
                "forward proxy: HTTP blocked",
                source_ip=source_ip,
                host=host,
                method=parsed.method,
                reason=decision.reason,
            )
            await _respond(
                writer,
                403,
                "Forbidden",
                extra_headers={"X-Egress-Reason": decision.reason[:200]},
            )
            return

        try:
            up_reader, up_writer = await asyncio.open_connection(host, port)
        except (TimeoutError, OSError) as exc:
            logger.warning(
                "forward proxy: upstream connect failed",
                host=host,
                port=port,
                error=str(exc),
            )
            await _respond(writer, 502, "Bad Gateway")
            return

        # Replay the original request with the path rewritten to
        # origin-form (absolute-URI is only legal on the proxy hop).
        rewritten = _rewrite_request_line(parsed.raw, parsed.method, path)
        up_writer.write(rewritten)
        await up_writer.drain()

        # Splice remaining bytes both ways. Downstream reader may
        # already hold the body we haven't consumed — pipe continues
        # from where _read_request left off.
        await _pipe(reader, up_writer, writer, up_reader)


# ---------------------------------------------------------------------------
# Lower-level helpers (module-level so they're testable in isolation)
# ---------------------------------------------------------------------------


async def _read_request(reader: asyncio.StreamReader) -> ParsedRequest:
    """Read the request line + headers up to the terminating blank line.

    Raises ``ValueError`` on malformed input, ``asyncio.TimeoutError``
    on stalled reads, ``ConnectionError`` on peer reset.
    """
    raw = await asyncio.wait_for(
        reader.readuntil(b"\r\n\r\n"),
        timeout=_HEADER_READ_TIMEOUT_S,
    )
    if len(raw) > _MAX_HEADER_BYTES:
        raise ValueError(f"Header block exceeds {_MAX_HEADER_BYTES} bytes")

    lines = raw.split(b"\r\n")
    if not lines:
        raise ValueError("Empty request")
    try:
        request_line = lines[0].decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("Non-ASCII in request line") from exc
    parts = request_line.split(" ")
    if len(parts) < 3:
        raise ValueError(f"Malformed request line: {request_line!r}")
    method, target = parts[0].upper(), parts[1]

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        if b":" not in line:
            continue
        k, _, v = line.partition(b":")
        try:
            headers[k.decode("ascii").strip().lower()] = v.decode("ascii").strip()
        except UnicodeDecodeError:
            continue

    return ParsedRequest(method=method, target=target, headers=headers, raw=raw)


def _split_host_port(target: str, *, default_port: int) -> tuple[str, int]:
    """Parse ``host:port`` from a CONNECT target; host alone defaults to 443."""
    if not target:
        raise ValueError("Empty target")
    if ":" in target:
        host, _, port_str = target.rpartition(":")
        try:
            port = int(port_str)
        except ValueError as exc:
            raise ValueError(f"Non-numeric port in {target!r}") from exc
        # Strip IPv6 brackets if present
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        return host, port
    return target, default_port


_ABSOLUTE_URI = re.compile(r"^(?P<scheme>https?)://(?P<host>[^/:?#]+)(?::(?P<port>\d+))?(?P<path>/[^ ]*)?$")


def _extract_plain_http_target(parsed: ParsedRequest) -> tuple[str, int, str]:
    """Return (host, port, origin-form path) for a plain HTTP forward.

    Tries the absolute-URI form first (``GET http://host:port/path``),
    then falls back to Host header + the request-line path.
    """
    m = _ABSOLUTE_URI.match(parsed.target)
    if m:
        scheme = m.group("scheme")
        host = m.group("host")
        port_str = m.group("port")
        port = int(port_str) if port_str else (443 if scheme == "https" else 80)
        path = m.group("path") or "/"
        return host, port, path

    # Fallback: Host header + path from target.
    host_header = parsed.headers.get("host", "")
    if not host_header:
        return "", 80, ""
    try:
        host, port = _split_host_port(host_header, default_port=80)
    except ValueError:
        return "", 80, ""
    # Assume the target is already origin-form (``/path?query``) in this branch.
    return host, port, parsed.target


def _rewrite_request_line(raw: bytes, method: str, path: str) -> bytes:
    """Replace the absolute-URI in the request line with the origin-form path.

    Keeps the rest of the headers intact; only the first line changes.
    RFC 7230 §5.3.2 requires the proxy hop to use absolute-URI and the
    origin hop to use origin-form, so we have to rewrite on forward.
    """
    newline = raw.find(b"\r\n")
    if newline < 0:
        return raw  # caller gave us malformed bytes; pass through
    # Extract the HTTP version from the original request line.
    original = raw[:newline].decode("ascii", errors="replace")
    version = original.rsplit(" ", 1)[-1] if " " in original else "HTTP/1.1"
    new_line = f"{method} {path} {version}".encode("ascii")
    return new_line + raw[newline:]


async def _write_status_line_only(
    writer: asyncio.StreamWriter,
    status: int,
    reason: str,
) -> None:
    """Write the CONNECT success response without closing the socket.

    For 200 Connection Established we must leave the TCP half-open so
    the pipe can splice bytes through. Content-Length:0 is included so
    HTTP-aware intermediaries don't keep looking for a body that will
    never arrive.
    """
    lines = [
        f"HTTP/1.1 {status} {reason}",
        "Content-Length: 0",
        "",
        "",
    ]
    writer.write("\r\n".join(lines).encode("ascii"))
    with contextlib.suppress(ConnectionError, OSError):
        await writer.drain()


async def _respond(
    writer: asyncio.StreamWriter,
    status: int,
    reason: str,
    *,
    extra_headers: dict[str, str] | None = None,
) -> None:
    """Write a minimal HTTP/1.1 response and close."""
    lines = [f"HTTP/1.1 {status} {reason}"]
    if extra_headers:
        for k, v in extra_headers.items():
            lines.append(f"{k}: {v}")
    lines.append("Content-Length: 0")
    lines.append("Connection: close")
    lines.append("")
    lines.append("")
    try:
        writer.write("\r\n".join(lines).encode("ascii"))
        await writer.drain()
    except (ConnectionError, OSError):
        pass
    await _close(writer)


async def _close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
        await writer.wait_closed()
    except (ConnectionError, OSError):
        pass


async def _pipe(
    client_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
) -> None:
    """Full-duplex byte splice between client and upstream.

    Two tasks, one per direction. Whichever finishes first (EOF or
    error) triggers cancellation of the other — matches how TLS
    half-closes are handled in squid/envoy. Without the cancel,
    a dead upstream would keep the client's descriptor pinned.
    """
    async def _copy(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            while True:
                chunk = await r.read(_BUFFER_SIZE)
                if not chunk:
                    break
                w.write(chunk)
                await w.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            with contextlib.suppress(BaseException):
                w.close()

    c2u = asyncio.create_task(_copy(client_reader, upstream_writer))
    u2c = asyncio.create_task(_copy(upstream_reader, client_writer))
    _done, pending = await asyncio.wait({c2u, u2c}, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    # Drain the cancelled tasks so exceptions don't spawn spurious logs.
    for t in pending:
        with contextlib.suppress(BaseException):
            await t
