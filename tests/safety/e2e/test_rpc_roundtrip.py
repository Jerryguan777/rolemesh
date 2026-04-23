"""End-to-end round-trip test for the slow-check NATS RPC channel.

Starts a real NATS connection and exercises RemoteCheck → core NATS
request/reply → SafetyRpcServer → check execution → reply → verdict
parse. Unit tests for each side use a fake NATS client; this file
catches regressions those miss:

  - wildcard subject parsing (``agent.*.safety.detect``) across the
    real nats-py client
  - request/reply timeouts on an actual server round-trip
  - trust boundary behaviour under real NATS message flow
  - thread-pool dispatch vs event-loop dispatch on a live server

Skips automatically when NATS is not reachable — CI without the dev
stack sees no false failures; local runs with docker-compose up
pick it up. Same pattern as ``tests/approval/e2e/conftest.py``.
"""

from __future__ import annotations

import asyncio
import os
import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import nats
import pytest

from agent_runner.safety.remote import RemoteCheck
from rolemesh.safety.registry import CheckRegistry
from rolemesh.safety.rpc_server import SafetyRpcServer, TrustedCoworker
from rolemesh.safety.types import (
    CostClass,
    Finding,
    SafetyContext,
    Stage,
    Verdict,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


_NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")


def _nats_reachable(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 4222
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


if not _nats_reachable(_NATS_URL):
    pytest.skip(
        f"NATS not reachable at {_NATS_URL}; skip RPC E2E. "
        "Start with: docker compose -f docker-compose.dev.yml up -d",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Stub checks for the RPC server
# ---------------------------------------------------------------------------


class _AsyncAllow:
    id = "stub.async.allow"
    version = "1"
    stages = frozenset({Stage.INPUT_PROMPT})
    cost_class: CostClass = "slow"
    supported_codes = frozenset({"STUB"})
    config_model = None

    async def check(
        self, _ctx: SafetyContext, _config: dict[str, Any]
    ) -> Verdict:
        return Verdict(action="allow")


class _AsyncBlock:
    id = "stub.async.block"
    version = "1"
    stages = frozenset({Stage.INPUT_PROMPT})
    cost_class: CostClass = "slow"
    supported_codes = frozenset({"STUB"})
    config_model = None

    async def check(
        self, ctx: SafetyContext, config: dict[str, Any]
    ) -> Verdict:
        return Verdict(
            action="block",
            reason=f"stub block for {ctx.tenant_id}/{ctx.stage.value}",
            findings=[
                Finding(
                    code="STUB", severity="high", message="rpc round-trip ok",
                    metadata={"from_config": config.get("marker")},
                )
            ],
        )


class _SyncSlow:
    id = "stub.sync.slow"
    version = "1"
    stages = frozenset({Stage.INPUT_PROMPT})
    cost_class: CostClass = "slow"
    supported_codes = frozenset({"STUB"})
    config_model = None
    _sync = True

    async def check(
        self, _ctx: SafetyContext, _config: dict[str, Any]
    ) -> Verdict:
        import threading

        # Thread-id surfaces via finding metadata so the test can
        # prove _sync=True checks ran off the main event loop.
        return Verdict(
            action="allow",
            findings=[
                Finding(
                    code="STUB", severity="info", message="sync ran",
                    metadata={"thread_id": threading.get_ident()},
                )
            ],
        )


class _Slow:
    """Sleeps longer than the client timeout — exercises the timeout
    → fail-open path."""

    id = "stub.slow.sleeper"
    version = "1"
    stages = frozenset({Stage.INPUT_PROMPT})
    cost_class: CostClass = "slow"
    supported_codes = frozenset({"STUB"})
    config_model = None

    async def check(
        self, _ctx: SafetyContext, _config: dict[str, Any]
    ) -> Verdict:
        await asyncio.sleep(1.0)
        return Verdict(action="block", reason="should not reach")


@dataclass(frozen=True)
class _TrustedRec:
    tenant_id: str
    id: str


def _lookup(cid: str) -> TrustedCoworker | None:
    if cid == "cw-ok":
        return _TrustedRec(tenant_id="tenant-ok", id="cw-ok")
    return None


# ---------------------------------------------------------------------------
# Fixtures — one server per test (fresh subscription + reg per test)
# ---------------------------------------------------------------------------


@pytest.fixture
async def nats_client() -> AsyncIterator[Any]:
    client = await nats.connect(_NATS_URL)
    try:
        yield client
    finally:
        await client.close()


@pytest.fixture
async def server_client() -> AsyncIterator[Any]:
    # Separate NATS connection for the server so the subscribe loop
    # is isolated from the client's request callbacks. Mirrors
    # orchestrator + container being separate processes.
    client = await nats.connect(_NATS_URL)
    try:
        yield client
    finally:
        await client.close()


@pytest.fixture
def thread_pool() -> AsyncIterator[ThreadPoolExecutor]:
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="rpc-e2e")
    try:
        yield pool
    finally:
        pool.shutdown(wait=False)


def _ctx(
    tenant_id: str = "tenant-ok",
    coworker_id: str = "cw-ok",
    job_id: str = "job-e2e",
) -> SafetyContext:
    return SafetyContext(
        stage=Stage.INPUT_PROMPT,
        tenant_id=tenant_id,
        coworker_id=coworker_id,
        user_id="u",
        job_id=job_id,
        conversation_id="cv",
        payload={"prompt": "hello"},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHappyRoundTrip:
    @pytest.mark.asyncio
    async def test_async_check_roundtrip_returns_block_verdict(
        self,
        nats_client: Any,
        server_client: Any,
        thread_pool: ThreadPoolExecutor,
    ) -> None:
        reg = CheckRegistry()
        reg.register(_AsyncBlock())
        server = SafetyRpcServer(
            nats_client=server_client,
            registry=reg,
            thread_pool=thread_pool,
            coworker_lookup=_lookup,
        )
        await server.start()
        try:
            # The subscribe needs a tick to fully register on the NATS
            # side — request-before-subscribe would get NoRespondersError.
            await asyncio.sleep(0.05)

            remote = RemoteCheck(
                check_id="stub.async.block",
                version="1",
                stages=frozenset({Stage.INPUT_PROMPT}),
                cost_class="slow",
                supported_codes=frozenset({"STUB"}),
                nats_client=nats_client,
                default_timeout_ms=3000,
            )
            verdict = await remote.check(_ctx(), {"marker": "xyz"})
            assert verdict.action == "block"
            assert verdict.reason and "tenant-ok" in verdict.reason
            assert verdict.findings
            # Config round-tripped from client → server → back.
            assert verdict.findings[0].metadata["from_config"] == "xyz"
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_sync_check_runs_off_main_event_loop(
        self,
        nats_client: Any,
        server_client: Any,
        thread_pool: ThreadPoolExecutor,
    ) -> None:
        """``_sync=True`` checks dispatch to the thread pool. Capture
        the thread id the check body saw and assert it's not the
        server's main event-loop thread. Without the thread-pool
        dispatch a blocking ML library would stall the server loop
        under concurrent load — regression would surface here as
        thread_id == main_thread_id.
        """
        import threading

        reg = CheckRegistry()
        reg.register(_SyncSlow())
        server = SafetyRpcServer(
            nats_client=server_client,
            registry=reg,
            thread_pool=thread_pool,
            coworker_lookup=_lookup,
        )
        await server.start()
        main_thread = threading.get_ident()
        try:
            await asyncio.sleep(0.05)
            remote = RemoteCheck(
                check_id="stub.sync.slow",
                version="1",
                stages=frozenset({Stage.INPUT_PROMPT}),
                cost_class="slow",
                supported_codes=frozenset({"STUB"}),
                nats_client=nats_client,
                default_timeout_ms=3000,
            )
            verdict = await remote.check(_ctx(), {})
            assert verdict.action == "allow"
            assert verdict.findings
            worker_thread = verdict.findings[0].metadata.get("thread_id")
            assert worker_thread != main_thread, (
                "sync check must run on the thread pool, not the event loop "
                f"(both observed thread {main_thread})"
            )
        finally:
            await server.stop()


class TestFailOpenPaths:
    @pytest.mark.asyncio
    async def test_timeout_fails_open_with_critical_finding(
        self,
        nats_client: Any,
        server_client: Any,
        thread_pool: ThreadPoolExecutor,
    ) -> None:
        reg = CheckRegistry()
        reg.register(_Slow())
        server = SafetyRpcServer(
            nats_client=server_client,
            registry=reg,
            thread_pool=thread_pool,
            coworker_lookup=_lookup,
        )
        await server.start()
        try:
            await asyncio.sleep(0.05)
            remote = RemoteCheck(
                check_id="stub.slow.sleeper",
                version="1",
                stages=frozenset({Stage.INPUT_PROMPT}),
                cost_class="slow",
                supported_codes=frozenset({"STUB"}),
                nats_client=nats_client,
                default_timeout_ms=100,  # way shorter than the sleep
            )
            verdict = await remote.check(_ctx(), {})
            # Fail-open: no block even though the check would have
            # blocked if given time. Critical finding records the
            # outage for audit.
            assert verdict.action == "allow"
            assert verdict.findings
            assert verdict.findings[0].code == "SAFETY.RPC_TIMEOUT"
            assert verdict.findings[0].severity == "critical"
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_tenant_mismatch_fails_open_via_server_error_reply(
        self,
        nats_client: Any,
        server_client: Any,
        thread_pool: ThreadPoolExecutor,
    ) -> None:
        """Client sends a request with a coworker_id the server does
        NOT trust for that tenant. Server replies with an error;
        client fails open with a critical finding. A buggy or
        malicious container must never get a live check result back
        for another tenant's coworker.
        """
        reg = CheckRegistry()
        reg.register(_AsyncAllow())
        server = SafetyRpcServer(
            nats_client=server_client,
            registry=reg,
            thread_pool=thread_pool,
            coworker_lookup=_lookup,
        )
        await server.start()
        try:
            await asyncio.sleep(0.05)
            remote = RemoteCheck(
                check_id="stub.async.allow",
                version="1",
                stages=frozenset({Stage.INPUT_PROMPT}),
                cost_class="slow",
                supported_codes=frozenset({"STUB"}),
                nats_client=nats_client,
                default_timeout_ms=2000,
            )
            # tenant-evil doesn't match _lookup's tenant-ok for cw-ok.
            verdict = await remote.check(
                _ctx(tenant_id="tenant-evil"), {}
            )
            assert verdict.action == "allow"
            assert verdict.findings
            f = verdict.findings[0]
            assert f.code == "SAFETY.RPC_ERROR"
            assert "tenant" in f.message.lower()
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_unknown_check_id_fails_open(
        self,
        nats_client: Any,
        server_client: Any,
        thread_pool: ThreadPoolExecutor,
    ) -> None:
        reg = CheckRegistry()
        reg.register(_AsyncAllow())
        server = SafetyRpcServer(
            nats_client=server_client,
            registry=reg,
            thread_pool=thread_pool,
            coworker_lookup=_lookup,
        )
        await server.start()
        try:
            await asyncio.sleep(0.05)
            remote = RemoteCheck(
                check_id="stub.does.not.exist",
                version="1",
                stages=frozenset({Stage.INPUT_PROMPT}),
                cost_class="slow",
                supported_codes=frozenset({"STUB"}),
                nats_client=nats_client,
                default_timeout_ms=2000,
            )
            verdict = await remote.check(_ctx(), {})
            assert verdict.action == "allow"
            assert verdict.findings[0].code == "SAFETY.RPC_ERROR"
            assert "unknown check_id" in verdict.findings[0].message
        finally:
            await server.stop()


class TestSubjectWildcard:
    @pytest.mark.asyncio
    async def test_different_job_ids_share_one_server_subscription(
        self,
        nats_client: Any,
        server_client: Any,
        thread_pool: ThreadPoolExecutor,
    ) -> None:
        """One ``agent.*.safety.detect`` subscription on the server
        handles requests from any job_id. Regression: if the server
        accidentally subscribed to a concrete job subject, only the
        first container's requests would reach it.
        """
        reg = CheckRegistry()
        reg.register(_AsyncAllow())
        server = SafetyRpcServer(
            nats_client=server_client,
            registry=reg,
            thread_pool=thread_pool,
            coworker_lookup=_lookup,
        )
        await server.start()
        try:
            await asyncio.sleep(0.05)
            remote = RemoteCheck(
                check_id="stub.async.allow",
                version="1",
                stages=frozenset({Stage.INPUT_PROMPT}),
                cost_class="slow",
                supported_codes=frozenset({"STUB"}),
                nats_client=nats_client,
                default_timeout_ms=2000,
            )
            for job_id in ("job-alpha", "job-beta", "job-gamma"):
                verdict = await remote.check(_ctx(job_id=job_id), {})
                assert verdict.action == "allow", f"job_id={job_id}"
        finally:
            await server.stop()
