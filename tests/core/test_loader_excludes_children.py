"""Belt-and-suspenders test for the handbook §6 Step 2.5 audit.

The orchestrator loader ``rolemesh.main._load_state`` funnels every
conversation into ``_state.coworkers[*].conversations`` via the
``db.chat.get_all_conversations(include_children=False)`` default.
Every grep-A site in §6 Step 2.5 relies on this dict containing only
user-facing conversations.

A future PR that flips the default to ``include_children=True`` would
silently slip child convs into the dict — breaking trigger gating,
idle accounting, and broadcast fan-out — without any of the grep-A
sites observably failing in unit tests. This test pins the invariant
end-to-end so the flip surfaces as a deterministic failure.

We seed: tenant + frontdesk + target + parent conv + child conv,
call ``_load_state``, and assert the target coworker's
``CoworkerState.conversations`` dict contains the parent's entry
(when the parent belongs to the target — it doesn't here, so we use
the frontdesk for that assertion) and does NOT contain the child's.
"""

from __future__ import annotations

import uuid

import pytest

import rolemesh.main as orch_main
from rolemesh.auth.permissions import AgentPermissions
from rolemesh.db import (
    create_channel_binding,
    create_child_conversation,
    create_conversation,
    create_coworker,
    create_tenant,
    get_or_create_internal_binding,
)

pytestmark = pytest.mark.usefixtures("test_db")


async def test_load_state_excludes_delegation_children() -> None:
    tenant = await create_tenant(
        name="LoaderCorp", slug=f"loader-{uuid.uuid4().hex[:8]}"
    )
    frontdesk = await create_coworker(
        tenant_id=tenant.id,
        name="Frontdesk",
        folder=f"frontdesk-{uuid.uuid4().hex[:8]}",
        is_frontdesk=True,
        permissions=AgentPermissions(agent_delegate=True),
    )
    target = await create_coworker(
        tenant_id=tenant.id,
        name="Trading",
        folder=f"trading-{uuid.uuid4().hex[:8]}",
    )
    parent_binding = await create_channel_binding(
        coworker_id=frontdesk.id,
        tenant_id=tenant.id,
        channel_type="telegram",
        credentials={"bot_token": "x"},
    )
    parent_conv = await create_conversation(
        tenant_id=tenant.id,
        coworker_id=frontdesk.id,
        channel_binding_id=parent_binding.id,
        channel_chat_id=f"chat-{uuid.uuid4().hex[:8]}",
    )

    internal_binding = await get_or_create_internal_binding(
        tenant_id=tenant.id, coworker_id=target.id,
    )
    child_conv = await create_child_conversation(
        tenant_id=tenant.id,
        parent_conversation_id=parent_conv.id,
        target_coworker_id=target.id,
        target_internal_binding_id=internal_binding.id,
        user_id=None,
        mode="sticky",
    )

    assert child_conv.parent_conversation_id == parent_conv.id, (
        "fixture sanity: child must reference parent"
    )

    await orch_main._load_state()

    state = orch_main._state
    assert frontdesk.id in state.coworkers
    assert target.id in state.coworkers

    frontdesk_convs = state.coworkers[frontdesk.id].conversations
    target_convs = state.coworkers[target.id].conversations

    # The parent (user-facing) conv attached to the frontdesk MUST be loaded.
    assert parent_conv.id in frontdesk_convs, (
        "Parent (user-facing) conversation must be present on the frontdesk."
    )

    # The child (delegation) conv attached to the target MUST NOT be loaded.
    assert child_conv.id not in target_convs, (
        "Child delegation conversation leaked into _state.coworkers[target].conversations. "
        "If get_all_conversations / get_conversations_for_coworker default flipped to "
        "include_children=True, the entire handbook §6 Step 2.5 audit invariant is broken."
    )
    # Also: nothing else parent-linked should sneak in either.
    for cw_state in state.coworkers.values():
        for conv_state in cw_state.conversations.values():
            assert conv_state.conversation.parent_conversation_id is None, (
                f"Conversation {conv_state.conversation.id} has "
                f"parent_conversation_id={conv_state.conversation.parent_conversation_id!r} "
                "but was loaded into _state. Loader default must keep child convs out."
            )
