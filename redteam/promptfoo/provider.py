"""promptfoo custom provider — drives a live RoleMesh coworker as a red-team target.

⚠️ TEST / RED-TEAM ONLY — points at a STAGING stack, never production.

promptfoo's red-team plugins generate an adversarial prompt and hand it to this
provider's ``call_api``; the provider replays it against a real coworker over
RoleMesh's WebSocket run protocol and returns the agent's reply for grading.

Why a custom provider (not promptfoo's built-in HTTP provider): a coworker run
is asynchronous over a WebSocket (``request.run`` frame in, a stream of
``event.run.*`` frames out), not a synchronous HTTP request/response. The
built-in provider cannot collect the streamed reply or the tool-call signal.

The end-to-end path (all contracts verified against src/webui/v1):
  1. id_token (OIDC) from ``ROLEMESH_OIDC_TOKEN``  — owner@t1 via get-token.sh.
  2. POST /api/v1/coworkers/{id}/conversations      -> conversation_id
  3. POST /api/v1/auth/ws-ticket {conversation_id}  -> short-lived ws ticket
  4. WS  /api/v1/conversations/{id}/stream?ticket=  -> open the run channel
  5. send {"type":"request.run","input":<prompt>,"idempotency_key":<uuid>}
  6. collect frames until a terminal one (or timeout).

Terminal-frame -> verdict bucket (the §6 three-way split from the plan):
  * ``event.run.completed``                      -> the agent finished. If it
    called tools and surfaced out-of-scope data, the attack landed; if it
    refused, ``tool_calls`` is empty and the text reads as a refusal.
  * ``event.run.error`` code ``SAFETY_BLOCKED``  -> RoleMesh's safety pipeline
    blocked it (``details.stage`` / ``details.rule_id`` say which rule).
  * no terminal within the deadline                -> almost always the
    reversibility / HITL approval guard pausing a destructive (irreversible)
    tool with no auto-approver — distinct from a safety block.

``output`` is the agent's reply text (what promptfoo's grader judges).
``metadata`` carries ``tool_calls`` / ``blocked`` / ``blocked_by`` /
``stage`` / ``rule_id`` so a human (and a future P2 assertion) can tell which
layer stopped an attack, rather than collapsing all three into "pass".
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
import uuid
from typing import Any
from urllib.parse import urlsplit, urlunsplit

# ``websockets`` is the only non-stdlib dependency (see requirements.txt).
import websockets

# --- Configuration (all via env so nothing is hardcoded) --------------------

API_BASE = os.environ.get("ROLEMESH_API_BASE", "http://localhost:8080/api/v1").rstrip("/")
OIDC_TOKEN = os.environ.get("ROLEMESH_OIDC_TOKEN", "")
COWORKER_ID = os.environ.get("REDTEAM_COWORKER_ID", "")
RUN_TIMEOUT_S = float(os.environ.get("REDTEAM_RUN_TIMEOUT", "120"))
# Staging guard escape hatch — must be set explicitly to point anywhere that
# is not localhost / a host with "staging" in it.
ALLOW_NONLOCAL = os.environ.get("REDTEAM_ALLOW_NONLOCAL", "") == "1"


class ProviderError(RuntimeError):
    """Configuration / transport failure surfaced to promptfoo as an error."""


def _assert_staging() -> None:
    """Refuse to run against anything that isn't obviously staging/local.

    These plugins trigger REAL tool calls (writes, deletes, exfil). Pointing
    them at production would mutate live data. Fail closed; require an explicit
    opt-out for a non-local host.
    """
    host = (urlsplit(API_BASE).hostname or "").lower()
    is_local = host in {"localhost", "127.0.0.1", "::1"} or "staging" in host
    if not is_local and not ALLOW_NONLOCAL:
        raise ProviderError(
            f"refusing to red-team non-staging host {host!r}: set "
            "REDTEAM_ALLOW_NONLOCAL=1 only if this is truly a disposable target."
        )


def _post_json(path: str, body: dict[str, Any]) -> dict[str, Any]:
    """POST a JSON body with the OIDC bearer; return the parsed response."""
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {OIDC_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise ProviderError(f"POST {path} -> {exc.code}: {detail}") from exc


def _ws_url(conversation_id: str, ticket: str) -> str:
    """Build the ws(s):// stream URL from the http(s):// API base."""
    parts = urlsplit(API_BASE)
    scheme = "wss" if parts.scheme == "https" else "ws"
    path = f"{parts.path}/conversations/{conversation_id}/stream"
    return urlunsplit((scheme, parts.netloc, path, f"ticket={ticket}", ""))


async def _drive_run(conversation_id: str, ticket: str, prompt: str) -> dict[str, Any]:
    """Open the WS, fire one ``request.run``, and collect until terminal.

    Returns a result dict; never raises for an in-band agent outcome (a safety
    block is a normal terminal state, not an error).
    """
    text_parts: list[str] = []
    tool_calls: list[dict[str, str]] = []
    result: dict[str, Any] = {
        "blocked": False,
        "blocked_by": None,
        "stage": None,
        "rule_id": None,
        "run_status": "unknown",
    }

    url = _ws_url(conversation_id, ticket)
    async with websockets.connect(url, open_timeout=30, max_size=4 * 1024 * 1024) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "request.run",
                    "input": prompt,
                    "idempotency_key": str(uuid.uuid4()),
                }
            )
        )
        loop = asyncio.get_event_loop()
        deadline = loop.time() + RUN_TIMEOUT_S
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                # No terminal frame: the run is parked — almost always the
                # reversibility / HITL approval guard with no auto-approver.
                result["run_status"] = "timeout"
                result["blocked"] = True
                result["blocked_by"] = "timeout_or_hitl"
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                continue
            try:
                frame = json.loads(raw)
            except (ValueError, TypeError):
                continue
            ftype = frame.get("type")
            if ftype == "event.run.token":
                text_parts.append(str(frame.get("delta", "")))
            elif ftype == "event.run.progress" and frame.get("status") == "tool_use":
                tool_calls.append(
                    {
                        "tool": str(frame.get("tool", "")),
                        "input_preview": str(frame.get("input_preview", "")),
                    }
                )
            elif ftype == "event.run.completed":
                result["run_status"] = "completed"
                break
            elif ftype == "event.run.error":
                # Safety pipeline (or another typed error) terminated the run.
                details = frame.get("details") or {}
                result["run_status"] = "error"
                result["blocked"] = True
                result["blocked_by"] = (
                    "safety" if frame.get("code") == "SAFETY_BLOCKED" else "error"
                )
                result["stage"] = details.get("stage")
                result["rule_id"] = details.get("rule_id")
                text_parts.append(str(frame.get("message", "")))
                break
            # event.message.appended / event.delegation.* / etc. are ignored.

    result["output"] = "".join(text_parts).strip()
    result["tool_calls"] = tool_calls
    return result


def call_api(prompt: str, options: dict[str, Any] | None = None,
             context: dict[str, Any] | None = None) -> dict[str, Any]:
    """promptfoo entrypoint. Returns a ProviderResponse dict.

    ``output`` is the graded text; ``metadata`` carries the block-source
    signals so a "block" is never silently read as the agent behaving safely
    when it was actually the reversibility guard or a timeout.
    """
    try:
        _assert_staging()
        if not OIDC_TOKEN:
            raise ProviderError("ROLEMESH_OIDC_TOKEN is empty (run get-token.sh owner@t1).")
        if not COWORKER_ID:
            raise ProviderError("REDTEAM_COWORKER_ID is empty (see redteam/seed.py output).")

        conv = _post_json(f"/coworkers/{COWORKER_ID}/conversations", {"name": "redteam"})
        conversation_id = conv["id"]
        ticket_resp = _post_json("/auth/ws-ticket", {"conversation_id": conversation_id})
        ticket = ticket_resp["ticket"]

        result = asyncio.run(_drive_run(conversation_id, ticket, prompt))
    except ProviderError as exc:
        return {"error": str(exc)}
    except (OSError, websockets.WebSocketException, KeyError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}

    return {
        "output": result["output"],
        "metadata": {
            "blocked": result["blocked"],
            "blocked_by": result["blocked_by"],
            "stage": result["stage"],
            "rule_id": result["rule_id"],
            "run_status": result["run_status"],
            "tool_calls": result["tool_calls"],
        },
    }
