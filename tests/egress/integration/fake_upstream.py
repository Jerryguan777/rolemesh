#!/usr/bin/env python3
"""Fake upstream server for egress integration tests.

Serves HTTP on port 443 (CONNECT target) and 80. Every request is
echoed back as JSON — tests can assert that the gateway injected the
expected credentials, rewrote the Host header, and preserved the body
byte-for-byte.

Deliberately plain HTTP even on port 443: after CONNECT opens the
tunnel, whatever the client sends through is what reaches us. Tests
that care about TLS termination would be a separate concern (the
gateway does not do TLS intercept; that's V2+).
"""

from __future__ import annotations

import hashlib
import json

from aiohttp import web


async def handle(request: web.Request) -> web.Response:
    body = await request.read()
    digest = hashlib.sha256(body).hexdigest() if body else ""
    payload = {
        "path": request.path,
        "method": request.method,
        "headers": {k: v for k, v in request.headers.items()},
        "query": dict(request.query),
        "body_sha": digest,
        "body_len": len(body),
    }
    return web.Response(
        status=200,
        body=json.dumps(payload).encode("utf-8"),
        content_type="application/json",
    )


def main() -> None:
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    app.router.add_route("*", "/", handle)
    # Run on both 80 (plain forward-proxy target) and 443 (CONNECT
    # target). Root inside the fake container so binding <1024 is
    # unrestricted; the container is isolated and ephemeral.
    runner = web.AppRunner(app)
    import asyncio
    import sys

    async def _run() -> None:
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", 80).start()
        await web.TCPSite(runner, "0.0.0.0", 443).start()
        # Log an explicit banner — aiohttp's AppRunner skips the
        # default "Running on ..." line that web.run_app prints, and
        # silent fake upstreams mask test-infra bugs.
        print("fake_upstream: listening on 0.0.0.0:80 and 0.0.0.0:443", flush=True)
        sys.stdout.flush()
        await asyncio.Event().wait()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
