"""Unit tests for ``send_to_conversation_with_fanout`` — the
channel-adapter fan-out that delivers approval outcomes from a
delegation child conv back up to its parent (frontdesk v1.2).

The fan-out IS the centrepiece of phase C: 8 distinct
``send_to_conversation`` call sites under ``approval/`` (executor.py
204+255 and engine.py 276+290+677+845 + two _send_to_origin callers)
all flow through this single helper, so fixing the channel adapter
covers all 8 in one place.

These tests use injected fakes for ``get_conv`` / ``send_via_coworker``
/ ``coworker_lookup`` so they verify the branching contract without
booting NATS, Docker, or the orchestrator. The behaviours under test:

  * non-regression: a plain conv (parent_conversation_id IS NULL) gets
    exactly one dispatch, no fan-out, no extra DB lookup.
  * child conv: the original conv AND the parent are dispatched to;
    the parent's text is prefixed ``[via <target_name>] ``.
  * the target's name is looked up from the in-memory coworker map
    and falls back to "specialist" if the coworker was hard-deleted.
  * parent-not-found is logged-but-skipped, never a crash.
  * conv-not-found is logged-but-skipped — and crucially, the
    SECOND dispatch (fan-out) is NEVER attempted (we'd otherwise be
    publishing to an unknown channel).
  * repeated calls do NOT dedup — approval may legitimately notify
    more than once and the adapter must not silently drop the second.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rolemesh.main import send_to_conversation_with_fanout

# ---------------------------------------------------------------------------
# Lightweight stand-ins for Conversation / Coworker / CoworkerState — we
# only touch the attributes the helper reads. Pulling in the real
# dataclasses would force PG-shaped defaults the helper doesn't care
# about.
# ---------------------------------------------------------------------------


@dataclass
class _FakeConv:
    id: str
    coworker_id: str
    channel_chat_id: str
    parent_conversation_id: str | None = None


@dataclass
class _FakeCoworker:
    id: str
    name: str


@dataclass
class _FakeCoworkerState:
    config: _FakeCoworker


@dataclass
class _Captured:
    """One dispatch the fake send_via_coworker observed."""

    cw_state: _FakeCoworkerState | None
    chat_id: str
    text: str


@dataclass
class _Recorder:
    convs: dict[str, _FakeConv] = field(default_factory=dict)
    coworkers: dict[str, _FakeCoworkerState] = field(default_factory=dict)
    sent: list[_Captured] = field(default_factory=list)

    async def get_conv(self, conv_id: str) -> _FakeConv | None:
        return self.convs.get(conv_id)

    async def send_via_coworker(
        self, cw_state: _FakeCoworkerState | None, chat_id: str, text: str,
    ) -> None:
        self.sent.append(
            _Captured(cw_state=cw_state, chat_id=chat_id, text=text),
        )

    def coworker_lookup(self, coworker_id: str) -> _FakeCoworkerState | None:
        return self.coworkers.get(coworker_id)


def _build_simple_world() -> _Recorder:
    """Standard world: a parent + child conv linked by parent_conversation_id."""
    rec = _Recorder()
    rec.coworkers["fd"] = _FakeCoworkerState(_FakeCoworker(id="fd", name="frontdesk"))
    rec.coworkers["trading"] = _FakeCoworkerState(
        _FakeCoworker(id="trading", name="Trading Desk"),
    )
    rec.convs["parent"] = _FakeConv(
        id="parent", coworker_id="fd", channel_chat_id="user-chat-1",
    )
    rec.convs["child"] = _FakeConv(
        id="child", coworker_id="trading",
        channel_chat_id="internal:parent:trading",
        parent_conversation_id="parent",
    )
    return rec


# ---------------------------------------------------------------------------
# Non-regression — plain conversation
# ---------------------------------------------------------------------------


class TestPlainConversation:
    async def test_no_fanout_when_parent_is_null(self) -> None:
        """Mutation guard: drops the ``if parent is None: return`` and
        you'd see a spurious ``get_conv("None")`` lookup here. Asserts
        on the EXACT dispatch list shape catches it.
        """
        rec = _build_simple_world()
        await send_to_conversation_with_fanout(
            "parent", "hello",
            get_conv=rec.get_conv,
            send_via_coworker=rec.send_via_coworker,
            coworker_lookup=rec.coworker_lookup,
        )
        assert len(rec.sent) == 1
        s = rec.sent[0]
        assert s.cw_state is not None
        assert s.cw_state.config.id == "fd"
        assert s.chat_id == "user-chat-1"
        assert s.text == "hello"


# ---------------------------------------------------------------------------
# Fan-out — child conv with parent
# ---------------------------------------------------------------------------


class TestChildConvFanout:
    async def test_dispatches_to_child_and_parent_with_via_prefix(self) -> None:
        """Both rows receive a message; the parent row has the
        ``[via <target_name>] `` prefix so the user knows where it
        came from. Asserts on exact text — paraphrasing the prefix
        breaks the frontend chip regex.
        """
        rec = _build_simple_world()
        await send_to_conversation_with_fanout(
            "child", "Approval #abc was rejected.",
            get_conv=rec.get_conv,
            send_via_coworker=rec.send_via_coworker,
            coworker_lookup=rec.coworker_lookup,
        )
        assert len(rec.sent) == 2
        first, second = rec.sent
        # Child dispatch is unchanged — audit on the child conv is intact.
        assert first.chat_id == "internal:parent:trading"
        assert first.text == "Approval #abc was rejected."
        # Parent dispatch carries the via marker so the frontend can
        # render a "via Trading Desk" chip.
        assert second.chat_id == "user-chat-1"
        assert second.text == "[via Trading Desk] Approval #abc was rejected."

    async def test_target_name_falls_back_to_specialist_when_coworker_missing(
        self,
    ) -> None:
        """If the target's in-memory coworker record disappeared between
        delegation start and approval outcome, the prefix degrades to a
        generic "specialist" rather than crashing the dispatch.
        """
        rec = _build_simple_world()
        # Simulate hard-delete: child conv still references the
        # coworker but the runtime map no longer has it.
        del rec.coworkers["trading"]
        await send_to_conversation_with_fanout(
            "child", "hi",
            get_conv=rec.get_conv,
            send_via_coworker=rec.send_via_coworker,
            coworker_lookup=rec.coworker_lookup,
        )
        assert len(rec.sent) == 2
        assert rec.sent[1].text == "[via specialist] hi"

    async def test_parent_not_found_logs_but_does_not_crash(self) -> None:
        """A dangling parent_conversation_id (e.g. parent hard-deleted
        but child row outlasted it) must NOT abort the original
        dispatch — that already happened — and must NOT raise.
        """
        rec = _build_simple_world()
        del rec.convs["parent"]
        # Should not raise.
        await send_to_conversation_with_fanout(
            "child", "hi",
            get_conv=rec.get_conv,
            send_via_coworker=rec.send_via_coworker,
            coworker_lookup=rec.coworker_lookup,
        )
        # Only the child dispatch landed; parent dispatch was skipped.
        assert len(rec.sent) == 1
        assert rec.sent[0].chat_id == "internal:parent:trading"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    async def test_conversation_id_not_found_skips_everything(self) -> None:
        """If the destination conv doesn't exist, we MUST NOT then call
        get_conv again with a bogus parent or send_via_coworker at all.
        Mutation guard: dropping the early ``return`` would have us
        chase a non-existent ``conv.parent_conversation_id`` attribute.
        """
        rec = _Recorder()  # no convs at all
        await send_to_conversation_with_fanout(
            "ghost", "x",
            get_conv=rec.get_conv,
            send_via_coworker=rec.send_via_coworker,
            coworker_lookup=rec.coworker_lookup,
        )
        assert rec.sent == []

    async def test_repeated_calls_do_not_dedup(self) -> None:
        """Approval may legitimately notify more than once (e.g. an
        outcome event re-fires after a transient gateway failure). The
        adapter must dispatch each call cleanly — there is no
        idempotency key here.
        """
        rec = _build_simple_world()
        for _ in range(2):
            await send_to_conversation_with_fanout(
                "child", "Approval #abc was rejected.",
                get_conv=rec.get_conv,
                send_via_coworker=rec.send_via_coworker,
                coworker_lookup=rec.coworker_lookup,
            )
        assert len(rec.sent) == 4  # 2 dispatches per call

    async def test_send_via_coworker_called_with_correct_cw_state(self) -> None:
        """The parent's dispatch uses the PARENT's coworker (frontdesk),
        not the target's. Without this, the gateway would look up a
        binding under the target's coworker and miss the user-facing
        channel.
        """
        rec = _build_simple_world()
        await send_to_conversation_with_fanout(
            "child", "hi",
            get_conv=rec.get_conv,
            send_via_coworker=rec.send_via_coworker,
            coworker_lookup=rec.coworker_lookup,
        )
        # First dispatch — to the target's binding (child).
        assert rec.sent[0].cw_state is not None
        assert rec.sent[0].cw_state.config.id == "trading"
        # Second dispatch — to the parent's binding (frontdesk).
        assert rec.sent[1].cw_state is not None
        assert rec.sent[1].cw_state.config.id == "fd"
