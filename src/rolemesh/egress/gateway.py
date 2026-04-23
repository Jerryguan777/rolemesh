"""Egress gateway container entry point.

``python -m rolemesh.egress.gateway`` is the Docker ``ENTRYPOINT`` for
``rolemesh-egress-gateway``. In EC-1 the gateway is functionally
equivalent to the host-side credential proxy, just relocated into its
own container — this lets EC-1 validate the network topology
(agent-net is internal, egress-net routes out, gateway is dual-homed,
agents reach the gateway by service name) before EC-2 layers the
forward proxy, DNS resolver, and Safety pipeline on top.

Port plan:
    3001  reverse proxy (credential injection for LLM + MCP) — EC-1
    3128  forward proxy (HTTP CONNECT)                        — EC-2
    53    authoritative DNS resolver                          — EC-2

EC-1 only binds 3001. Binding on ``0.0.0.0`` is intentional: the
container is reachable from two isolated bridges (agent-net internal,
egress-net external). Agent bridge is the only place actual traffic
originates; egress bridge is the gateway's own exit.
"""

from __future__ import annotations

import asyncio
import signal

from rolemesh.core.config import CREDENTIAL_PROXY_PORT
from rolemesh.core.logger import get_logger
from rolemesh.security.credential_proxy import start_credential_proxy

logger = get_logger()


async def main() -> None:
    runner = await start_credential_proxy(port=CREDENTIAL_PROXY_PORT, host="0.0.0.0")
    logger.info(
        "egress gateway started",
        reverse_proxy_port=CREDENTIAL_PROXY_PORT,
        stage="EC-1",
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    try:
        await stop.wait()
    finally:
        logger.info("egress gateway stopping")
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
