"""Frontdesk v1.2 catalog rendering and list_agents responder.

Renders the same-tenant delegatable-specialist roster for a frontdesk
and ships the ``FRONTDESK_RULES`` system-prompt snippet that pairs with
it. ``handle_list_agents_request`` answers the
``agent.*.list_agents.request`` core NATS RPC (request-reply, reusing
the existing connection — no new JetStream consumer; see handbook
§4 #17).

Naming contract (handbook §4 #16, §8 #26): the catalog renders
``(id: <folder>)``, NOT ``(folder: <folder>)``. ``FRONTDESK_RULES`` uses
the term "agent id", NOT "folder slug". The frontdesk inherits
broad bash perms; a "folder" label nudges the model into
filesystem operations like ``ls trading/`` instead of the
``delegate_to_agent`` tool — a real observed bug, not a theoretical
concern.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rolemesh.core.orchestrator_state import OrchestratorState


logger = logging.getLogger(__name__)


def render_agent_catalog(
    state: OrchestratorState,
    tenant_id: str,
    *,
    exclude: str,
) -> str:
    """Render the delegatable-specialist roster for ``tenant_id``.

    Filters:
      - same tenant as the caller
      - ``status == 'active'``
      - NOT ``is_frontdesk`` (frontdesks are routers, not delegation
        targets; a specialist is any non-frontdesk coworker — the old
        ``agent_role == 'agent'`` axis was removed upstream)
      - id != ``exclude`` (no self-delegation in the catalog)

    Empty result returns the literal directive
    "No specialists available. Answer the user directly." rather than
    an empty list so the LLM has an unambiguous signal.
    """
    lines: list[str] = ["Domain specialists available in this tenant:"]
    for cs in state.coworkers.values():
        c = cs.config
        if (
            c.tenant_id == tenant_id
            and c.status == "active"
            and not c.is_frontdesk
            and c.id != exclude
        ):
            desc = c.routing_description or "(no description provided)"
            lines.append(f"- {c.name} (id: {c.folder}) — {desc}")
    if len(lines) == 1:
        return "No specialists available. Answer the user directly."
    return "\n".join(lines)


FRONTDESK_RULES = """\
You are the front desk of this organization.

Specialists are OTHER AGENTS reachable ONLY through the delegate_to_agent
tool. They are NOT files, directories, processes, or anything you can
access via bash/ls/read/edit. Do NOT try filesystem operations to find
them.

Routing rules:
- For simple greetings or status questions, answer yourself.
- For domain-specific requests, call delegate_to_agent with the
  specialist's agent id (e.g. "trading"). The agent id is a routing
  identifier passed verbatim through the tool, not a filesystem path.
- Write self-contained delegation prompts; specialists cannot see this
  conversation.
- For multi-domain requests, call delegate_to_agent multiple times —
  in parallel within one assistant message if requests are independent,
  or sequentially across turns if a later one needs the earlier one's
  result.
- If you don't see a matching specialist in the catalog above, call
  list_agents first to refresh — the catalog above is from your spawn
  time and may be stale.
- When a specialist returns isError=true (error, safety_blocked, or
  timeout), your reply MUST include both the specialist's name and the
  literal reason text from the tool response. Paraphrasing the reason
  is acceptable; omitting it or replacing it with vague phrasing like
  "had some trouble" is not.

  Example acceptable: "I asked Trading to place the order, but it
  declined: Order size exceeds daily limit for unverified accounts.
  Would you like to try a smaller size?"

  Example NOT acceptable: "I had some trouble; let me try again."
"""


def compose_frontdesk_system_prompt(
    *,
    is_frontdesk: bool,
    base_system_prompt: str | None,
    catalog_body: str,
) -> str | None:
    """Append the catalog + ``FRONTDESK_RULES`` to ``base_system_prompt``
    when ``is_frontdesk`` is True; return ``base_system_prompt``
    unchanged otherwise.

    Pure function so the Phase B Step 6 injection logic can be unit-
    tested without standing up a container. The caller (the
    ``ContainerAgentExecutor.execute`` spawn path) renders the catalog
    via the ``render_catalog`` callback it was constructed with and
    feeds the body in here.

    Returns ``None`` if ``base_system_prompt`` is None AND
    ``is_frontdesk`` is False — preserves the "no system prompt set"
    signal for non-frontdesk agents.
    """
    if not is_frontdesk:
        return base_system_prompt
    appended = f"{catalog_body}\n\n{FRONTDESK_RULES}"
    if base_system_prompt:
        return f"{base_system_prompt}\n\n{appended}"
    return appended


async def handle_list_agents_request(
    msg: object,
    *,
    state: OrchestratorState,
) -> None:
    """Core NATS responder for ``agent.*.list_agents.request``.

    Payload: ``{"tenantId": str, "fromCoworkerId": str}``.
    Reply:   ``{"text": <rendered catalog>}`` (always present).

    Never raises: a handler exception would silently break the agent's
    in-turn refresh path. On any failure we log + return an empty text
    plus ``"error"`` field, and the calling tool surfaces a normal
    timeout-style error to the LLM.
    """
    body: bytes
    try:
        data = json.loads(msg.data.decode())  # type: ignore[attr-defined]
        tenant_id = str(data["tenantId"])
        from_id = str(data["fromCoworkerId"])
        text = render_agent_catalog(state, tenant_id, exclude=from_id)
        body = json.dumps({"text": text}).encode("utf-8")
    except Exception as exc:
        # Never let handler death leak: an unhandled raise would silently
        # break ``list_agents`` for every frontdesk turn until the
        # subscriber gets reinstated.
        logger.exception("list_agents handler failed: %s", exc)
        body = json.dumps({"text": "", "error": str(exc)}).encode("utf-8")
    try:
        await msg.respond(body)  # type: ignore[attr-defined]
    except Exception:
        logger.exception("list_agents respond failed")
