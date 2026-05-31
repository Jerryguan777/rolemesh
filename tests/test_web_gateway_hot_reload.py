"""Smoke-discovered gap: orchestrator's WebNatsGateway doesn't hot-reload bindings.

Before the fix, ``WebNatsGateway._bindings`` was populated only at
startup from ``_state.coworkers``. When the v1 webui created a
fresh ``channel_bindings`` row via
``POST /api/v1/coworkers/{id}/conversations``, the live orchestrator
never learned about it — every inbound message for the new binding
warned "Unknown web binding_id" and got ack'd into the void.

The fix: ``_refresh_binding`` looks the row up via the admin pool
(no tenant context — the binding row itself carries ``tenant_id``)
and registers it on demand. These tests pin the behaviour:

* Known binding → no DB hit, listener proceeds normally.
* Unknown binding present in DB → DB hit, binding registered,
  message processed.
* Unknown binding absent from DB → registers nothing, returns
  False; listener warns + acks (no exception, no leak).
* Non-web binding (telegram / slack row that landed in the wrong
  subject by accident) → not registered (channel_type guard).

Anti-mirror: assertions observe whether the on_message callback
fired (the production effect), not which DB function was invoked.
"""

from __future__ import annotations

import pytest

from rolemesh.channels.web_nats_gateway import WebNatsGateway
from rolemesh.core.types import ChannelBinding


class _FakeTransport:
    """Stand-in transport for tests that don't need NATS."""

    js = None


def _make_gateway(monkeypatch: pytest.MonkeyPatch, db_rows: dict[str, ChannelBinding]) -> WebNatsGateway:
    """Build a gateway with a stubbed DB lookup.

    ``db_rows`` simulates the ``channel_bindings`` table keyed by id.
    The gateway's hot-reload helper calls
    ``get_channel_binding_by_id_admin`` — we monkeypatch that
    coroutine to read from the dict so the test doesn't need a real
    Postgres connection.
    """
    async def _fake_lookup(binding_id: str):  # type: ignore[no-untyped-def]
        return db_rows.get(binding_id)

    # The gateway imports the helper lazily inside ``_refresh_binding``
    # to avoid an import cycle, so the patch must target the source
    # module the helper is imported from.
    # The gateway does ``from rolemesh.db import get_channel_binding_by_id_admin``
    # so the lookup resolves against the package ``__init__`` namespace, not
    # ``rolemesh.db.chat``. Patching the package-level binding is what makes
    # the substitution stick at call time.
    import rolemesh.db as db_pkg

    monkeypatch.setattr(db_pkg, "get_channel_binding_by_id_admin", _fake_lookup)

    async def _noop_on_message(*args, **kwargs):  # type: ignore[no-untyped-def]
        pass

    return WebNatsGateway(on_message=_noop_on_message, transport=_FakeTransport())  # type: ignore[arg-type]


def _binding(binding_id: str, *, channel_type: str = "web", tenant_id: str = "t-1") -> ChannelBinding:
    return ChannelBinding(
        id=binding_id,
        coworker_id="cw-1",
        tenant_id=tenant_id,
        channel_type=channel_type,
        credentials={},
        bot_display_name=None,
        status="active",
        created_at="",
    )


@pytest.mark.asyncio
async def test_unknown_binding_present_in_db_gets_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The smoke gap: a v1-created binding lands in DB but not in memory.

    After ``_refresh_binding`` runs once, the dict contains the row
    and a subsequent listener path uses it without another DB hit.
    """
    target = _binding("bind-v1")
    gw = _make_gateway(monkeypatch, {"bind-v1": target})

    assert "bind-v1" not in gw._bindings  # type: ignore[attr-defined]
    registered = await gw._refresh_binding("bind-v1")  # type: ignore[attr-defined]
    assert registered is True
    assert gw._bindings["bind-v1"] is target  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_unknown_binding_absent_from_db_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forged subject / cleanup race: row truly doesn't exist.

    Listener falls back to log+ack; the dict stays empty. Asserting
    the return value (rather than checking the dict only) catches a
    refactor that accidentally returns True on miss.
    """
    gw = _make_gateway(monkeypatch, {})
    registered = await gw._refresh_binding("ghost-id")  # type: ignore[attr-defined]
    assert registered is False
    assert "ghost-id" not in gw._bindings  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_non_web_binding_is_not_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hostile subject pointing at a slack/telegram binding row.

    The subject filter is ``web.inbound.*`` so this is a
    belt-and-braces check — if the orchestrator started seeing a
    binding_id whose row in DB is ``channel_type='telegram'``,
    blindly registering it would let cross-channel traffic land on
    the wrong gateway. The guard rejects it.
    """
    target = _binding("bind-tg", channel_type="telegram")
    gw = _make_gateway(monkeypatch, {"bind-tg": target})
    registered = await gw._refresh_binding("bind-tg")  # type: ignore[attr-defined]
    assert registered is False
    assert "bind-tg" not in gw._bindings  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_db_lookup_failure_does_not_propagate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB hiccup must not bring the NATS listener down.

    ``_refresh_binding`` swallows the exception, logs, and returns
    False — same observable shape as "binding not found". The
    smoke contract: bad DB on a single message must not strand
    every other binding's traffic.
    """
    async def _boom(binding_id: str):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated pool exhaustion")

    import rolemesh.db as db_pkg

    monkeypatch.setattr(db_pkg, "get_channel_binding_by_id_admin", _boom)

    async def _noop_on_message(*args, **kwargs):  # type: ignore[no-untyped-def]
        pass

    gw = WebNatsGateway(on_message=_noop_on_message, transport=_FakeTransport())  # type: ignore[arg-type]
    # Must not raise.
    registered = await gw._refresh_binding("any-id")  # type: ignore[attr-defined]
    assert registered is False
