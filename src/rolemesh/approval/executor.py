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
    does not abort the others.
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


class _HasRequestId(Protocol):
    @property
    def data(self) -> bytes: ...
    @property
    def subject(self) -> str: ...
    async def ack(self) -> None: ...


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

    async def start(self) -> None:
        # Durable consumer: if the orchestrator restarts, we pick up
        # undelivered decisions from the last unacked offset instead of
        # skipping to NEW and silently losing work.
        self._sub = await self._js.subscribe(
            "approval.decided.*",
            durable="orch-approval-worker",
            manual_ack=True,
        )
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
            try:
                await self._handle_message(msg)
            except Exception as exc:
                logger.exception("approval worker: handler crashed", error=str(exc))
                with contextlib.suppress(Exception):
                    await msg.ack()

    async def _handle_message(self, msg: _HasRequestId) -> None:
        request_id = msg.subject.rsplit(".", 1)[-1]
        try:
            body = json.loads(msg.data.decode() or "{}")
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

        await pg.write_approval_audit(
            request_id=request_id, action="executing", actor_user_id=None
        )
        results = await self._execute_actions(req.actions, req.action_hashes, req.user_id)
        terminal = "executed" if all(not r.get("error") for r in results) else "execution_failed"
        await pg.set_approval_status(request_id, terminal)
        await pg.write_approval_audit(
            request_id=request_id,
            action=terminal,
            actor_user_id=None,
            metadata={"results": results},
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
                    conversation_id=req.conversation_id,
                    error=str(exc),
                )
        await msg.ack()

    async def _execute_actions(
        self,
        actions: list[dict[str, Any]],
        hashes: list[str],
        user_id: str,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        async with aiohttp.ClientSession() as session:
            for i, action in enumerate(actions):
                server = str(action.get("mcp_server") or "")
                tool = str(action.get("tool_name") or "")
                params = action.get("params") or {}
                action_hash = hashes[i] if i < len(hashes) else ""
                if not server or not tool:
                    results.append(
                        {"error": f"malformed action at index {i}"}
                    )
                    continue
                url = f"{self._proxy_base}/mcp-proxy/{server}/"
                headers = {
                    "Content-Type": "application/json",
                    "X-RoleMesh-User-Id": user_id,
                    "X-Idempotency-Key": action_hash,
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
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        text = await resp.text()
                        if resp.status >= 400:
                            results.append(
                                {
                                    "error": f"MCP {resp.status}: {text[:200]}",
                                }
                            )
                        else:
                            try:
                                parsed = json.loads(text)
                            except json.JSONDecodeError:
                                parsed = {"raw": text}
                            results.append({"ok": True, "response": parsed})
                except Exception as exc:  # noqa: BLE001 — record per-action
                    results.append({"error": str(exc)})
        return results


__all__ = ["ApprovalWorker"]
