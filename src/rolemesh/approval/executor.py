"""ApprovalWorker — the only MCP-executing component in the system.

Subscribes to ``approval.decided.*`` (durable JetStream consumer in the
orchestrator process). Payload: ``{"status": "approved"|"rejected",
"note": ...}``.

Execution model:
  - status=approved: atomic claim (approved → executing), then
    sequentially POST each action to the credential proxy's
    ``/mcp-proxy/<server>/`` endpoint with the agent turn's user_id as
    ``X-RoleMesh-User-Id`` and the precomputed action_hash as
    ``X-Idempotency-Key``. Best-effort batching: one action failing
    does not abort the others. The audit 'executing' and terminal
    ('executed' | 'execution_failed') rows are written by the DB
    trigger in the same transaction as the status change, so the log
    is atomic with the transition.
  - status=rejected: send the rejection notification to the originating
    conversation. (The state transition pending→rejected was already
    done inside ApprovalEngine.handle_decision; the Worker only
    reaches the user.)

Responsibilities deliberately kept out of this module:
  - No policy matching. By the time we get here, decide has already
    landed.
  - No retry on 5xx. A failed action is recorded and the batch status
    becomes ``execution_failed``; the admin decides whether to re-run.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Any, Protocol

import aiohttp
from nats.js.api import ConsumerConfig

from rolemesh.core.config import CREDENTIAL_PROXY_PORT
from rolemesh.core.logger import get_logger
from rolemesh.db import pg

from .notification import (
    ChannelSender,
    format_decision_message,
    format_execution_report,
)

if TYPE_CHECKING:
    from nats.js.client import JetStreamContext

logger = get_logger()

# The credential proxy lives inside the orchestrator process; address
# it over localhost. Containers use host.docker.internal, but the Worker
# runs co-located so 127.0.0.1 is correct and avoids a DNS round trip.
_CREDENTIAL_PROXY_URL = f"http://127.0.0.1:{CREDENTIAL_PROXY_PORT}"

# Per-action HTTP timeout against the credential proxy. Set high enough
# for realistic MCP calls (e.g. an ERP refund that writes to a ledger).
_ACTION_TIMEOUT_SECONDS = 30

# NATS AckWait: must exceed the realistic worst-case batch duration so
# that long batches don't trigger spurious redelivery. Safe to keep well
# above _ACTION_TIMEOUT_SECONDS * max_batch — redelivery only hurts log
# signal quality, not correctness (the atomic claim deduplicates), so
# we set a generous ceiling and additionally call msg.in_progress()
# between actions as belt-and-braces.
_ACK_WAIT_SECONDS = 600


class _HasRequestId(Protocol):
    @property
    def data(self) -> bytes: ...
    @property
    def subject(self) -> str: ...
    async def ack(self) -> None: ...
    async def in_progress(self) -> None: ...


class ApprovalWorker:
    def __init__(
        self,
        *,
        js: JetStreamContext,
        channel_sender: ChannelSender,
        proxy_base_url: str | None = None,
    ) -> None:
        self._js = js
        self._channel = channel_sender
        self._proxy_base = (proxy_base_url or _CREDENTIAL_PROXY_URL).rstrip("/")
        self._sub: Any = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        # Shared across all messages — avoids building a TCP pool per
        # decided event. Created on start(), closed on stop().
        self._http: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        # Durable consumer with an explicit AckWait so long-running
        # batches don't trigger redelivery mid-execution.
        self._sub = await self._js.subscribe(
            "approval.decided.*",
            durable="orch-approval-worker",
            manual_ack=True,
            config=ConsumerConfig(ack_wait=_ACK_WAIT_SECONDS),
        )
        self._http = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._sub is not None:
            with contextlib.suppress(Exception):
                await self._sub.unsubscribe()
        if self._http is not None:
            await self._http.close()
            self._http = None

    async def _run_loop(self) -> None:
        assert self._sub is not None
        while not self._stop.is_set():
            try:
                msg = await self._sub.next_msg(timeout=1.0)
            except TimeoutError:
                continue
            except Exception as exc:  # noqa: BLE001 — keep the loop alive
                logger.warning("approval worker: subscription error", error=str(exc))
                await asyncio.sleep(1.0)
                continue
            request_id = msg.subject.rsplit(".", 1)[-1]
            try:
                await self._handle_message(msg)
            except Exception as exc:
                logger.exception(
                    "approval worker: handler crashed",
                    request_id=request_id,
                    error=str(exc),
                )
                with contextlib.suppress(Exception):
                    await msg.ack()

    async def _handle_message(self, msg: _HasRequestId) -> None:
        request_id = msg.subject.rsplit(".", 1)[-1]
        try:
            body = json.loads(msg.data.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = {}
        status = str(body.get("status") or "approved")

        if status == "rejected":
            # The engine has already written the 'rejected' state + audit.
            # We just deliver the notification.
            req = await pg.get_approval_request(request_id)
            if req is not None and req.conversation_id:
                try:
                    await self._channel.send_to_conversation(
                        req.conversation_id,
                        format_decision_message(
                            request=req,
                            decision="rejected",
                            note=body.get("note"),
                        ),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "approval worker: reject notify failed",
                        request_id=request_id,
                        conversation_id=req.conversation_id,
                        error=str(exc),
                    )
            await msg.ack()
            return

        # status == "approved": claim and execute.
        req = await pg.claim_approval_for_execution(request_id)
        if req is None:
            # Another Worker got it first, or the state moved on. Ack so
            # JetStream does not redeliver indefinitely.
            await msg.ack()
            return

        # The claim's audit row ('executing') was written by the DB trigger
        # atomically with the status change. No manual write needed here.
        results = await self._execute_actions(
            req.actions,
            req.user_id,
            request_id=req.id,
            msg=msg,
        )
        terminal = (
            "executed"
            if all(not r.get("error") for r in results)
            else "execution_failed"
        )
        # Pass metadata through the CRUD call so the trigger attaches it
        # to the terminal audit row in the same transaction.
        await pg.set_approval_status(
            request_id, terminal, metadata={"results": results}
        )
        if req.conversation_id:
            try:
                await self._channel.send_to_conversation(
                    req.conversation_id,
                    format_execution_report(
                        request=req, results=results, status=terminal
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "approval worker: report send failed",
                    request_id=request_id,
                    conversation_id=req.conversation_id,
                    error=str(exc),
                )
        await msg.ack()

    async def _execute_actions(
        self,
        actions: list[dict[str, Any]],
        user_id: str,
        *,
        request_id: str,
        msg: _HasRequestId | None = None,
    ) -> list[dict[str, Any]]:
        """Execute each action; return a per-action result dict.

        Idempotency key contract (v2):
          We send ``X-Idempotency-Key = f"{request_id}:{i}"``.

          ``request_id`` is a UUID scoped per approval request, which
          is itself scoped per tenant — so two tenants issuing the
          same semantic action produce distinct keys, preventing the
          cross-tenant cache-replay attack that arises when the key
          is only ``sha256(tool, params)``. This replaces the earlier
          design that reused the pre-computed ``action_hashes`` (still
          used as the dedup key for auto-intercept inside the
          engine — two different roles for two different data).
        """
        results: list[dict[str, Any]] = []
        # Reuse the long-lived session. Fallback to a transient session
        # only if start() was skipped (e.g. unit tests driving
        # _handle_message directly).
        owned = self._http is None
        session = self._http or aiohttp.ClientSession()
        try:
            for i, action in enumerate(actions):
                # Extend the NATS ack deadline between actions so long
                # batches don't trigger spurious redelivery.
                if msg is not None:
                    with contextlib.suppress(Exception):
                        await msg.in_progress()
                server = str(action.get("mcp_server") or "")
                tool = str(action.get("tool_name") or "")
                params = action.get("params") or {}
                if not server or not tool:
                    results.append(
                        {"error": f"malformed action at index {i}"}
                    )
                    continue
                url = f"{self._proxy_base}/mcp-proxy/{server}/"
                headers: dict[str, str] = {
                    "Content-Type": "application/json",
                    "X-RoleMesh-User-Id": user_id,
                    "X-Idempotency-Key": f"{request_id}:{i}",
                }
                body = {
                    "jsonrpc": "2.0",
                    "id": i + 1,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": params},
                }
                try:
                    async with session.post(
                        url,
                        headers=headers,
                        json=body,
                        timeout=aiohttp.ClientTimeout(total=_ACTION_TIMEOUT_SECONDS),
                    ) as resp:
                        text = await resp.text()
                        if resp.status >= 400:
                            # Transport-level failure. Per-action error.
                            results.append(
                                {"error": f"MCP {resp.status}: {text[:200]}"}
                            )
                        else:
                            try:
                                parsed = json.loads(text)
                            except json.JSONDecodeError:
                                # Non-JSON 2xx body — treat as an opaque
                                # success so admins can still inspect it.
                                results.append({"ok": True, "response": {"raw": text}})
                            # JSON-RPC 2.0 §5.1: a response object with
                            # ``error`` set and no ``result`` is an
                            # application-level failure. HTTP status
                            # alone is not enough — the MCP server
                            # returns 200 with a body that carries the
                            # failure.
                            else:
                                if (
                                    isinstance(parsed, dict)
                                    and parsed.get("error") is not None
                                    and parsed.get("result") is None
                                ):
                                    err = parsed["error"]
                                    if isinstance(err, dict):
                                        msg_text = err.get("message") or str(err)
                                        code = err.get("code")
                                        results.append(
                                            {
                                                "error": (
                                                    f"MCP error"
                                                    f"{f' {code}' if code is not None else ''}: "
                                                    f"{msg_text}"
                                                ),
                                                "jsonrpc_error": err,
                                            }
                                        )
                                    else:
                                        results.append({"error": f"MCP error: {err!r}"})
                                else:
                                    results.append({"ok": True, "response": parsed})
                except Exception as exc:  # noqa: BLE001 — record per-action
                    results.append({"error": str(exc)})
        finally:
            if owned:
                await session.close()
        return results


__all__ = ["ApprovalWorker"]
