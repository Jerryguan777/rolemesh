"""Orchestrator-side NATS subscriber that executes slow safety checks.

Subscribes to ``agent.*.safety.detect`` (core NATS request-reply).
The container's ``RemoteCheck`` sends one request per slow-check
invocation; this server decodes the context, runs the check against
the process-wide orchestrator registry, and replies with the verdict.

Trust boundary:

  The request carries a claimed ``tenant_id`` and ``coworker_id``. A
  malicious or buggy container must not be able to make the
  orchestrator run a check against the WRONG tenant's view. Before
  execution, the claimed identifiers are checked against the
  in-memory coworker map — same pattern ``SafetyEventsSubscriber``
  uses. Mismatches reply with an error and are logged WARNING.

Concurrency:

  Checks marked ``_sync = True`` block the CPU (e.g. Presidio NLP,
  LLM-Guard tokenizers). Running those on the orchestrator's asyncio
  loop would stall every other request. They are dispatched to the
  supplied ``ThreadPoolExecutor`` so the loop stays responsive. Async
  checks (HTTP-style, e.g. OpenAI Moderation) run directly on the
  loop.

  One orchestrator event loop services N container requests; the
  thread pool caps the number of concurrent sync checks.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, Protocol

from rolemesh.core.logger import get_logger

from .errors import UnknownCheckError
from .rpc_codec import deserialize_context, serialize_verdict

if TYPE_CHECKING:
    from collections.abc import Callable
    from concurrent.futures import ThreadPoolExecutor

    from .registry import CheckRegistry
    from .types import Verdict

logger = get_logger()


class TrustedCoworker(Protocol):
    tenant_id: str
    id: str


class SafetyRpcServer:
    def __init__(
        self,
        *,
        nats_client: Any,
        registry: CheckRegistry,
        thread_pool: ThreadPoolExecutor,
        coworker_lookup: Callable[[str], TrustedCoworker | None],
    ) -> None:
        self._nc = nats_client
        self._registry = registry
        self._thread_pool = thread_pool
        self._lookup = coworker_lookup
        self._sub: Any = None

    async def start(self) -> None:
        """Begin servicing requests. Idempotent — a second call is a no-op.

        Uses a wildcard core-NATS subscribe. The orchestrator process
        owns exactly one SafetyRpcServer; there is no need for a
        durable consumer because slow checks are synchronous from the
        container's perspective and a missed reply causes a timeout +
        fail-open there.
        """
        if self._sub is not None:
            return
        self._sub = await self._nc.subscribe(
            "agent.*.safety.detect", cb=self._handle_request
        )

    async def stop(self) -> None:
        if self._sub is not None:
            with _suppress_unsubscribe_errors():
                await self._sub.unsubscribe()
            self._sub = None

    async def _handle_request(self, msg: Any) -> None:
        """Process one core-NATS request and respond.

        Every branch below MUST ``await msg.respond(...)`` before
        returning, otherwise the container's ``RemoteCheck`` waits
        until its deadline and surfaces a RPC_TIMEOUT finding even
        though the server processed the request synchronously.
        """
        try:
            request = json.loads(msg.data)
        except (json.JSONDecodeError, ValueError) as exc:
            await _respond(msg, _error_reply("", f"malformed JSON: {exc}"))
            return
        if not isinstance(request, dict):
            await _respond(msg, _error_reply("", "request is not a JSON object"))
            return

        request_id = str(request.get("request_id") or "")
        ctx_data = request.get("context")
        if not isinstance(ctx_data, dict):
            await _respond(
                msg,
                _error_reply(request_id, "missing or non-dict context"),
            )
            return
        try:
            ctx = deserialize_context(ctx_data)
        except (KeyError, ValueError) as exc:
            await _respond(
                msg,
                _error_reply(request_id, f"bad context: {exc}"),
            )
            return

        # Trust check: the claimed coworker must exist and its
        # authoritative tenant_id must match the claim. Mismatches are
        # logged so anomaly detection can spot a misbehaving
        # container attempting cross-tenant reads.
        trusted = self._lookup(ctx.coworker_id)
        if trusted is None:
            logger.warning(
                "safety.rpc: unknown coworker_id — dropping request",
                component="safety",
                claimed_coworker=ctx.coworker_id,
            )
            await _respond(
                msg,
                _error_reply(request_id, "unknown coworker_id"),
            )
            return
        if trusted.tenant_id != ctx.tenant_id:
            logger.warning(
                "safety.rpc: tenant_id mismatch — dropping request",
                component="safety",
                claimed_tenant=ctx.tenant_id,
                trusted_tenant=trusted.tenant_id,
                coworker_id=ctx.coworker_id,
            )
            await _respond(
                msg,
                _error_reply(request_id, "tenant_id mismatch"),
            )
            return

        check_id = str(request.get("check_id") or "")
        try:
            check = self._registry.get(check_id)
        except UnknownCheckError:
            await _respond(
                msg,
                _error_reply(
                    request_id, f"unknown check_id: {check_id!r}"
                ),
            )
            return

        config = request.get("config") or {}
        if not isinstance(config, dict):
            config = {}

        try:
            verdict = await self._run_check(check, ctx, config)
        except Exception as exc:  # noqa: BLE001 — any check error is user-facing
            logger.warning(
                "safety.rpc: check raised — returning error reply",
                component="safety",
                check_id=check_id,
                error=str(exc),
            )
            await _respond(
                msg,
                _error_reply(
                    request_id, f"check raised: {exc}"
                ),
            )
            return

        await _respond(
            msg,
            {
                "request_id": request_id,
                "verdict": serialize_verdict(verdict),
                "error": None,
            },
        )

    async def _run_check(
        self, check: Any, ctx: Any, config: dict[str, Any]
    ) -> Verdict:
        if getattr(check, "_sync", False):
            # Block the thread pool, not the event loop. Each invocation
            # spins a fresh event loop inside the worker because
            # ``asyncio.run`` is the standard way to bridge back into
            # an async-style check interface; check implementations
            # can still use ``await`` internally (useful for the
            # orchestrator's own async libraries) while the outer
            # call sits on a thread.
            loop = asyncio.get_running_loop()
            verdict: Verdict = await loop.run_in_executor(
                self._thread_pool,
                _run_async_in_thread,
                check,
                ctx,
                config,
            )
            return verdict
        verdict = await check.check(ctx, config)
        return verdict


def _run_async_in_thread(
    check: Any, ctx: Any, config: dict[str, Any]
) -> Verdict:
    """Bridge the thread-pool worker back into ``check.check`` (async).

    Kept module-level so ``ThreadPoolExecutor.submit`` can pickle the
    task under interpreter reset weirdness. ``asyncio.run`` creates
    and tears down a fresh loop per call; reuse across requests is
    not worth the contention risk.
    """
    result: Verdict = asyncio.run(check.check(ctx, config))
    return result


def _error_reply(request_id: str, error: str) -> dict[str, Any]:
    return {"request_id": request_id, "verdict": None, "error": error}


async def _respond(msg: Any, payload: dict[str, Any]) -> None:
    try:
        await msg.respond(json.dumps(payload).encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — transport failure is observable via client timeout
        logger.warning(
            "safety.rpc: respond failed; client will time out",
            component="safety",
            error=str(exc),
        )


class _suppress_unsubscribe_errors:  # noqa: N801 — mimics contextlib.suppress naming
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc_info: object) -> bool:
        return True


__all__ = ["SafetyRpcServer", "TrustedCoworker"]
