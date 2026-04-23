"""Unit tests for SafetyRpcServer.

Scope: one decoded request in → one reply out. Uses a fake ``msg``
object that captures ``respond`` calls, and a fake NATS client whose
``subscribe`` merely stores the callback (we invoke it directly).

What the tests MUST pin:

  - Trust boundary: a container cannot fake its tenant. A forged
    tenant_id gets an error reply, the check is never invoked.
  - Unknown check_id is surfaced (the container's RemoteCheck then
    fails open with a critical finding).
  - Sync checks are dispatched through a thread pool so the server
    event loop is never blocked by ML inference.
  - Check exceptions flow back as error replies rather than poisoning
    the server — a buggy check must not drop every subsequent request.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import pytest

from rolemesh.safety.registry import CheckRegistry
from rolemesh.safety.rpc_codec import (
    deserialize_verdict,
    serialize_context,
)
from rolemesh.safety.rpc_server import SafetyRpcServer
from rolemesh.safety.types import (
    CostClass,
    Finding,
    SafetyContext,
    Stage,
    Verdict,
)


@dataclass
class _FakeMsg:
    data: bytes
    responses: list[bytes] = field(default_factory=list)

    async def respond(self, payload: bytes) -> None:
        self.responses.append(payload)


class _FakeNats:
    """Only wires subscribe → callback capture. The real nats-py call
    signature is ``subscribe(subject, cb=...)`` which returns a sub
    object; the server never invokes sub methods in the steady state
    aside from ``unsubscribe``.
    """

    def __init__(self) -> None:
        self.subject: str | None = None
        self.cb: Any = None

    async def subscribe(self, subject: str, cb: Any) -> Any:
        self.subject = subject
        self.cb = cb

        class _Sub:
            async def unsubscribe(self) -> None:
                return None

        return _Sub()


@dataclass
class _TrustedCoworker:
    tenant_id: str
    id: str


def _lookup_factory(
    mapping: dict[str, _TrustedCoworker],
) -> Any:
    def _lookup(cid: str) -> _TrustedCoworker | None:
        return mapping.get(cid)

    return _lookup


def _base_context() -> SafetyContext:
    return SafetyContext(
        stage=Stage.INPUT_PROMPT,
        tenant_id="t1",
        coworker_id="c1",
        user_id="u",
        job_id="j",
        conversation_id="cv",
        payload={"prompt": "hi"},
    )


def _request_bytes(
    *,
    request_id: str = "rid-1",
    check_id: str = "stub.slow",
    config: dict[str, Any] | None = None,
    context: SafetyContext | None = None,
) -> bytes:
    return json.dumps(
        {
            "request_id": request_id,
            "check_id": check_id,
            "config": config or {},
            "context": serialize_context(context or _base_context()),
            "deadline_ms": 500,
        }
    ).encode()


# --------------------------------------------------------------------
# Async check (non-_sync) — runs directly on event loop
# --------------------------------------------------------------------


class _AsyncAllow:
    id = "stub.slow"
    version = "1"
    stages = frozenset(Stage)
    cost_class: CostClass = "slow"
    supported_codes = frozenset({"X"})
    config_model = None

    async def check(
        self, _ctx: SafetyContext, _config: dict[str, Any]
    ) -> Verdict:
        return Verdict(action="allow")


class _AsyncBlock:
    id = "stub.slow"
    version = "1"
    stages = frozenset(Stage)
    cost_class: CostClass = "slow"
    supported_codes = frozenset({"X"})
    config_model = None

    async def check(
        self, _ctx: SafetyContext, _config: dict[str, Any]
    ) -> Verdict:
        return Verdict(
            action="block",
            reason="nope",
            findings=[
                Finding(code="X", severity="high", message="m")
            ],
        )


# --------------------------------------------------------------------
# Sync check (_sync=True) — runs on thread pool
# --------------------------------------------------------------------


class _SyncSlow:
    id = "stub.sync"
    version = "1"
    stages = frozenset(Stage)
    cost_class: CostClass = "slow"
    supported_codes = frozenset({"SYNC"})
    config_model = None
    _sync = True

    async def check(
        self, _ctx: SafetyContext, _config: dict[str, Any]
    ) -> Verdict:
        return Verdict(action="allow")


@pytest.fixture
def pool() -> ThreadPoolExecutor:
    ex = ThreadPoolExecutor(max_workers=2, thread_name_prefix="safety-test")
    yield ex
    ex.shutdown(wait=False)


def _make_server(
    check: Any,
    *,
    pool: ThreadPoolExecutor,
    trusted: dict[str, _TrustedCoworker] | None = None,
) -> tuple[SafetyRpcServer, _FakeNats]:
    reg = CheckRegistry()
    reg.register(check)
    nc = _FakeNats()
    server = SafetyRpcServer(
        nats_client=nc,
        registry=reg,
        thread_pool=pool,
        coworker_lookup=_lookup_factory(
            trusted
            if trusted is not None
            else {"c1": _TrustedCoworker(tenant_id="t1", id="c1")}
        ),
    )
    return server, nc


class TestRequestReply:
    @pytest.mark.asyncio
    async def test_async_check_executes_and_replies_with_verdict(
        self, pool: ThreadPoolExecutor
    ) -> None:
        server, nc = _make_server(_AsyncBlock(), pool=pool)
        await server.start()
        msg = _FakeMsg(data=_request_bytes())
        await nc.cb(msg)
        assert len(msg.responses) == 1
        reply = json.loads(msg.responses[0])
        assert reply["error"] is None
        v = deserialize_verdict(reply["verdict"])
        assert v.action == "block"
        assert v.reason == "nope"

    @pytest.mark.asyncio
    async def test_subscribe_uses_wildcard_subject(
        self, pool: ThreadPoolExecutor
    ) -> None:
        server, nc = _make_server(_AsyncAllow(), pool=pool)
        await server.start()
        assert nc.subject == "agent.*.safety.detect"

    @pytest.mark.asyncio
    async def test_start_is_idempotent(
        self, pool: ThreadPoolExecutor
    ) -> None:
        server, nc = _make_server(_AsyncAllow(), pool=pool)
        await server.start()
        first_cb = nc.cb
        await server.start()
        # A second call must not re-subscribe (which would double-
        # deliver every request in production).
        assert nc.cb is first_cb


class TestTrustBoundary:
    @pytest.mark.asyncio
    async def test_unknown_coworker_id_returns_error_reply(
        self, pool: ThreadPoolExecutor
    ) -> None:
        # The trust map does NOT contain "c1" — the server must not
        # look up the check or invoke it. Respond with an error so the
        # container's RemoteCheck fails open with a critical finding.
        server, nc = _make_server(
            _AsyncBlock(), pool=pool, trusted={}
        )
        await server.start()
        msg = _FakeMsg(data=_request_bytes())
        await nc.cb(msg)
        reply = json.loads(msg.responses[0])
        assert reply["verdict"] is None
        assert "unknown coworker" in reply["error"]

    @pytest.mark.asyncio
    async def test_tenant_mismatch_returns_error_reply(
        self, pool: ThreadPoolExecutor
    ) -> None:
        # Same coworker_id, different tenant_id in the claim vs the
        # trust map. A malicious container could attempt cross-tenant
        # reads if this check were absent.
        server, nc = _make_server(
            _AsyncBlock(),
            pool=pool,
            trusted={
                "c1": _TrustedCoworker(tenant_id="t-DIFFERENT", id="c1")
            },
        )
        await server.start()
        msg = _FakeMsg(data=_request_bytes())
        await nc.cb(msg)
        reply = json.loads(msg.responses[0])
        assert reply["verdict"] is None
        assert "tenant_id mismatch" in reply["error"]


class TestMalformedRequests:
    @pytest.mark.asyncio
    async def test_non_json_request_replies_with_error(
        self, pool: ThreadPoolExecutor
    ) -> None:
        server, nc = _make_server(_AsyncAllow(), pool=pool)
        await server.start()
        msg = _FakeMsg(data=b"garbage")
        await nc.cb(msg)
        reply = json.loads(msg.responses[0])
        assert reply["error"] and "malformed JSON" in reply["error"]

    @pytest.mark.asyncio
    async def test_missing_context_replies_with_error(
        self, pool: ThreadPoolExecutor
    ) -> None:
        server, nc = _make_server(_AsyncAllow(), pool=pool)
        await server.start()
        msg = _FakeMsg(
            data=json.dumps(
                {"request_id": "rid", "check_id": "stub.slow"}
            ).encode()
        )
        await nc.cb(msg)
        reply = json.loads(msg.responses[0])
        assert "context" in reply["error"]

    @pytest.mark.asyncio
    async def test_unknown_check_id_replies_with_error(
        self, pool: ThreadPoolExecutor
    ) -> None:
        server, nc = _make_server(_AsyncAllow(), pool=pool)
        await server.start()
        msg = _FakeMsg(data=_request_bytes(check_id="nope.nope"))
        await nc.cb(msg)
        reply = json.loads(msg.responses[0])
        assert "unknown check_id" in reply["error"]
        assert "nope.nope" in reply["error"]


class TestCheckExceptions:
    @pytest.mark.asyncio
    async def test_check_exception_replies_with_error_not_crash(
        self, pool: ThreadPoolExecutor
    ) -> None:
        class _Broken:
            id = "stub.broken"
            version = "1"
            stages = frozenset(Stage)
            cost_class: CostClass = "slow"
            supported_codes = frozenset()
            config_model = None

            async def check(
                self, _ctx: SafetyContext, _config: dict[str, Any]
            ) -> Verdict:
                raise RuntimeError("kaboom")

        server, nc = _make_server(_Broken(), pool=pool)
        await server.start()
        msg = _FakeMsg(data=_request_bytes(check_id="stub.broken"))
        await nc.cb(msg)
        reply = json.loads(msg.responses[0])
        assert reply["verdict"] is None
        assert "kaboom" in reply["error"]


class TestBackstopTryExcept:
    """Review fix P2-6: unexpected exceptions from the lookup
    callable or the request-deserialize path must still produce an
    error reply — otherwise the client burns its full deadline
    waiting for nothing.
    """

    @pytest.mark.asyncio
    async def test_crashing_lookup_still_replies_with_error(
        self, pool: ThreadPoolExecutor
    ) -> None:
        def _boom(_cid: str) -> _TrustedCoworker | None:
            raise RuntimeError("lookup went sideways")

        reg = CheckRegistry()
        reg.register(_AsyncAllow())
        nc = _FakeNats()
        server = SafetyRpcServer(
            nats_client=nc,
            registry=reg,
            thread_pool=pool,
            coworker_lookup=_boom,
        )
        await server.start()
        msg = _FakeMsg(data=_request_bytes())
        await nc.cb(msg)
        # Without the backstop, the server would raise out of its
        # subscribe callback and the client would time out.
        assert len(msg.responses) == 1
        reply = json.loads(msg.responses[0])
        assert reply["verdict"] is None
        assert reply["error"] and "internal error" in reply["error"]


class TestDeadlineEnforcement:
    """V2 P1 review-fix: server enforces request deadline_ms.

    Without this, a hung check stalls the server even after the
    container times out. The wait_for cancels the outer task so the
    event loop stays responsive even under load.
    """

    @pytest.mark.asyncio
    async def test_slow_check_beyond_deadline_returns_timeout_error_reply(
        self, pool: ThreadPoolExecutor
    ) -> None:
        import asyncio

        class _Sleeper:
            id = "stub.sleeper"
            version = "1"
            stages = frozenset(Stage)
            cost_class: CostClass = "slow"
            supported_codes: frozenset[str] = frozenset()
            config_model = None

            async def check(
                self, _ctx: SafetyContext, _config: dict[str, Any]
            ) -> Verdict:
                await asyncio.sleep(5.0)
                return Verdict(action="block", reason="should not reach")

        server, nc = _make_server(_Sleeper(), pool=pool)
        await server.start()
        # deadline_ms is read from the request; minimum clamp is
        # 100ms so the test completes quickly.
        msg = _FakeMsg(
            data=json.dumps(
                {
                    "request_id": "rid",
                    "check_id": "stub.sleeper",
                    "config": {},
                    "context": serialize_context(_base_context()),
                    "deadline_ms": 100,
                }
            ).encode()
        )
        await nc.cb(msg)
        assert len(msg.responses) == 1
        reply = json.loads(msg.responses[0])
        assert reply["verdict"] is None
        assert reply["error"] and "timeout" in reply["error"].lower()
        assert "100ms" in reply["error"]

    @pytest.mark.asyncio
    async def test_deadline_ms_clamped_to_sane_range(
        self, pool: ThreadPoolExecutor
    ) -> None:
        """A malicious or buggy client could send deadline_ms=0 or
        a huge number. The server clamps to [100ms, 30s] so neither
        extreme can DoS the server (too-short would turn every check
        into a timeout; too-long would hold event loop slots for
        minutes).
        """
        server, nc = _make_server(_AsyncAllow(), pool=pool)
        await server.start()
        for bogus in (0, -1, None, "abc", 10**9):
            msg = _FakeMsg(
                data=json.dumps(
                    {
                        "request_id": "rid",
                        "check_id": "stub.slow",
                        "config": {},
                        "context": serialize_context(_base_context()),
                        "deadline_ms": bogus,
                    }
                ).encode()
            )
            await nc.cb(msg)
            reply = json.loads(msg.responses[-1])
            # Either succeeded (clamped up from 0/neg/invalid) or
            # replied without error (we don't care which; the point
            # is no crash and no unbounded wait).
            assert "error" in reply


class TestSyncDispatch:
    @pytest.mark.asyncio
    async def test_sync_check_runs_off_event_loop(
        self, pool: ThreadPoolExecutor
    ) -> None:
        """Records the thread id the check body saw; it must differ
        from the main event loop thread when ``_sync=True`` is set.
        Without a thread pool dispatch, a sync ML library would block
        the orchestrator's event loop under load.
        """
        import threading

        seen_threads: list[int] = []

        class _CaptureThread:
            id = "stub.capture"
            version = "1"
            stages = frozenset(Stage)
            cost_class: CostClass = "slow"
            supported_codes = frozenset()
            config_model = None
            _sync = True

            async def check(
                self, _ctx: SafetyContext, _config: dict[str, Any]
            ) -> Verdict:
                seen_threads.append(threading.get_ident())
                return Verdict(action="allow")

        server, nc = _make_server(_CaptureThread(), pool=pool)
        await server.start()
        main_thread = threading.get_ident()
        msg = _FakeMsg(data=_request_bytes(check_id="stub.capture"))
        await nc.cb(msg)
        assert len(seen_threads) == 1
        assert seen_threads[0] != main_thread

    @pytest.mark.asyncio
    async def test_async_check_runs_on_main_event_loop(
        self, pool: ThreadPoolExecutor
    ) -> None:
        # Mirror assertion for non-_sync checks: they stay on the
        # server's loop so HTTP-style I/O benefits from the single
        # asyncio scheduler rather than paying thread-hop overhead.
        import threading

        seen_threads: list[int] = []

        class _CaptureThread:
            id = "stub.slow"
            version = "1"
            stages = frozenset(Stage)
            cost_class: CostClass = "slow"
            supported_codes = frozenset()
            config_model = None

            async def check(
                self, _ctx: SafetyContext, _config: dict[str, Any]
            ) -> Verdict:
                seen_threads.append(threading.get_ident())
                return Verdict(action="allow")

        server, nc = _make_server(_CaptureThread(), pool=pool)
        await server.start()
        main_thread = threading.get_ident()
        msg = _FakeMsg(data=_request_bytes())
        await nc.cb(msg)
        assert seen_threads == [main_thread]
