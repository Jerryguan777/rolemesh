"""T-NET: agent network isolation + egress-control reachability
(docs/21 §3 rows "Agent has no direct egress" / "Metadata protection",
§6.3 DNS contract: internal names resolve, non-allowlisted domains do
not, every HTTP egress needs the in-band identity token).

Each probe runs inside a real agent sandbox on the isolated agent
network with DNS pinned to the gateway resolver — the exact topology
production spawns get — and reports observed facts as JSON on stderr.
"""

from __future__ import annotations

import json
import textwrap
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .conftest import Topology

pytestmark = pytest.mark.integration


async def test_direct_connection_to_external_ip_is_denied(
    run_python: Callable[..., Awaitable[tuple[int, str]]],
) -> None:
    """T-NET-1: a TCP connection straight to a public IP (1.1.1.1:443,
    bypassing DNS and proxies entirely) must fail — the deny is enforced
    by the network fabric, not by configuration the agent could unset."""
    code = textwrap.dedent("""
        import json, socket, sys
        try:
            socket.create_connection(("1.1.1.1", 443), timeout=5).close()
            result = {"connected": True}
        except OSError as exc:
            result = {"connected": False, "error": type(exc).__name__}
        sys.stderr.write(json.dumps(result))
    """)
    exit_code, stderr = await run_python("net-direct", code)
    assert exit_code == 0
    result = json.loads(stderr)
    assert result["connected"] is False, (
        "agent reached the public internet directly — egress control is void"
    )


async def test_cloud_metadata_endpoint_is_unreachable(
    run_python: Callable[..., Awaitable[tuple[int, str]]],
    topology: Topology,
) -> None:
    """T-NET-2: the IMDS attack surface is closed on both planes: the
    bare IP 169.254.169.254 has no route, and the metadata hostname is
    blackholed to loopback by the production extra_hosts entries."""
    code = textwrap.dedent("""
        import json, socket, sys
        try:
            socket.create_connection(("169.254.169.254", 80), timeout=3).close()
            ip_probe = {"connected": True}
        except OSError as exc:
            ip_probe = {"connected": False, "error": type(exc).__name__}
        sys.stderr.write(json.dumps({
            "ip": ip_probe,
            "hostname_resolves_to": socket.gethostbyname("metadata.google.internal"),
        }))
    """)
    exit_code, stderr = await run_python(
        "net-metadata", code, extra_hosts=topology.metadata_extra_hosts
    )
    assert exit_code == 0
    result = json.loads(stderr)
    assert result["ip"]["connected"] is False, "IMDS IP reachable from agent"
    assert result["hostname_resolves_to"] == "127.0.0.1"


async def test_dns_resolves_internal_names_but_not_external_domains(
    run_python: Callable[..., Awaitable[tuple[int, str]]],
    topology: Topology,
) -> None:
    """T-NET-3: with DNS pinned to the gateway resolver, internal
    service names (nats, gateway) resolve while a non-allowlisted
    external domain does not (gateway policy: NXDOMAIN, the query never
    reaches the upstream — the DNS-exfil tripwire, docs/21 §6.3)."""
    code = textwrap.dedent(f"""
        import json, socket, sys
        def resolve(name):
            try:
                return {{"ok": True, "addr": socket.gethostbyname(name)}}
            except OSError as exc:
                return {{"ok": False, "error": type(exc).__name__}}
        sys.stderr.write(json.dumps({{
            "nats": resolve({topology.nats_host!r}),
            "gateway": resolve({topology.gateway_host!r}),
            "external": resolve("contract-denied.example.com"),
        }}))
    """)
    exit_code, stderr = await run_python("net-dns", code)
    assert exit_code == 0
    result = json.loads(stderr)
    # Internal plumbing must resolve — agents dial NATS and the gateway
    # by service name. A failure here is a product bug, not a test
    # environment problem (docs/21 §6.3 locks exactly this).
    assert result["nats"]["ok"] is True, f"internal name lookup broke: {result}"
    assert result["gateway"]["ok"] is True, f"internal name lookup broke: {result}"
    # ... while arbitrary external domains must not.
    assert result["external"]["ok"] is False, (
        f"non-allowlisted domain resolved: {result['external']}"
    )


async def test_forward_proxy_challenges_tokenless_connect_with_407(
    run_python: Callable[..., Awaitable[tuple[int, str]]],
    topology: Topology,
) -> None:
    """T-NET-4: a CONNECT without the signed identity token gets 407
    Proxy Authentication Required — egress identity is in-band and
    fail-closed, never inferred from the source address."""
    code = textwrap.dedent(f"""
        import json, socket, sys
        s = socket.create_connection(
            ({topology.gateway_host!r}, {topology.forward_port}), timeout=10
        )
        s.sendall(b"CONNECT example.com:443 HTTP/1.1\\r\\n"
                  b"Host: example.com:443\\r\\n\\r\\n")
        status_line = s.recv(4096).split(b"\\r\\n")[0].decode()
        s.close()
        sys.stderr.write(json.dumps({{"status_line": status_line}}))
    """)
    exit_code, stderr = await run_python("net-407-connect", code)
    assert exit_code == 0
    status_line = json.loads(stderr)["status_line"]
    assert " 407 " in status_line, f"expected 407 challenge, got: {status_line}"


async def test_forward_proxy_challenges_tokenless_plain_http_with_407(
    run_python: Callable[..., Awaitable[tuple[int, str]]],
    topology: Topology,
) -> None:
    """T-NET-5: the plain-HTTP forward path (absolute-URI GET) is held
    to the same identity requirement as CONNECT — no cheaper sibling."""
    code = textwrap.dedent(f"""
        import json, socket, sys
        s = socket.create_connection(
            ({topology.gateway_host!r}, {topology.forward_port}), timeout=10
        )
        s.sendall(b"GET http://example.com/ HTTP/1.1\\r\\n"
                  b"Host: example.com\\r\\n\\r\\n")
        status_line = s.recv(4096).split(b"\\r\\n")[0].decode()
        s.close()
        sys.stderr.write(json.dumps({{"status_line": status_line}}))
    """)
    exit_code, stderr = await run_python("net-407-get", code)
    assert exit_code == 0
    status_line = json.loads(stderr)["status_line"]
    assert " 407 " in status_line, f"expected 407 challenge, got: {status_line}"


async def test_credential_proxy_healthz_reachable_from_agent_network(
    run_python: Callable[..., Awaitable[tuple[int, str]]],
    topology: Topology,
) -> None:
    """T-NET-6: the gateway's reverse proxy answers /healthz with 200
    from inside the agent network — the one HTTP endpoint agents are
    entitled to reach without a token."""
    code = textwrap.dedent(f"""
        import json, sys, urllib.request
        url = "http://{topology.gateway_host}:{topology.reverse_port}/healthz"
        with urllib.request.urlopen(url, timeout=10) as resp:
            sys.stderr.write(json.dumps({{"status": resp.status}}))
    """)
    exit_code, stderr = await run_python("net-healthz", code)
    assert exit_code == 0
    assert json.loads(stderr)["status"] == 200
