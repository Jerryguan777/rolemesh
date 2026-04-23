"""Attack simulation fixtures.

Each attack file picks its own integration level:

  * **Spec-level** (e.g. A. Container Escape, H. Config): uses
    ``build_container_spec`` / pydantic validators directly, no DB,
    no NATS. Fast (<1s/test).

  * **Engine-level** (e.g. F. Approval, E. Tenant Isolation): uses
    the real approval engine with a fake NATS publisher + fake
    channel sender, backed by a real testcontainers Postgres.
    Inherits the ``test_db`` fixture from repo-level conftest.

  * **Pipeline-level** (e.g. C. Prompt Injection, D. Data Exfil):
    drives ``pipeline_run`` against the real orchestrator registry.
    Needs ``[safety-ml]`` extra installed for slow checks.

We deliberately do NOT spin up a full orchestrator harness with NATS
here (unlike tests/approval/e2e/). Attack simulations should be as
cheap as possible so the full suite runs in <30s; wiring real NATS
adds 10-30s per test for little incremental confidence —
tests/approval/e2e already verifies the NATS path.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

from rolemesh.db import pg

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


# ---------------------------------------------------------------------------
# Fake NATS publisher and channel sender — reused across attack files
# ---------------------------------------------------------------------------


@dataclass
class _FakePub:
    """Capture NATS publishes for assertion without a real broker."""

    publishes: list[tuple[str, bytes]]

    def __init__(self) -> None:
        self.publishes = []

    async def publish(self, subject: str, data: bytes) -> Any:
        self.publishes.append((subject, data))


@dataclass
class _FakeChannel:
    """Capture channel_sender.send_to_conversation calls."""

    sent: list[tuple[str, str]]

    def __init__(self) -> None:
        self.sent = []

    async def send_to_conversation(self, conversation_id: str, text: str) -> None:
        self.sent.append((conversation_id, text))


@pytest.fixture
def fake_publisher() -> _FakePub:
    return _FakePub()


@pytest.fixture
def fake_channel() -> _FakeChannel:
    return _FakeChannel()


# ---------------------------------------------------------------------------
# Seed helpers — thin wrappers over pg.* so each attack test reads as
# an attack narrative, not as CRUD plumbing.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VictimTenant:
    """A single-tenant fixture with all the chained entities an attack
    needs to target: tenant, owner user, coworker, conversation."""

    tenant_id: str
    owner_user_id: str
    coworker_id: str
    conversation_id: str


async def seed_victim(name: str = "victim") -> VictimTenant:
    tenant = await pg.create_tenant(
        name=name.capitalize(), slug=f"{name}-{uuid.uuid4().hex[:8]}"
    )
    owner = await pg.create_user(
        tenant_id=tenant.id,
        name=f"{name}-owner",
        email=f"{name}@example.com",
        role="owner",
    )
    coworker = await pg.create_coworker(
        tenant_id=tenant.id, name="CW", folder=f"cw-{uuid.uuid4().hex[:8]}"
    )
    binding = await pg.create_channel_binding(
        coworker_id=coworker.id,
        tenant_id=tenant.id,
        channel_type="telegram",
        credentials={"bot_token": "x"},
    )
    conv = await pg.create_conversation(
        tenant_id=tenant.id,
        coworker_id=coworker.id,
        channel_binding_id=binding.id,
        channel_chat_id=str(uuid.uuid4()),
    )
    return VictimTenant(
        tenant_id=tenant.id,
        owner_user_id=owner.id,
        coworker_id=coworker.id,
        conversation_id=conv.id,
    )


# ---------------------------------------------------------------------------
# Attacker identity helpers (for REST-level attacks)
# ---------------------------------------------------------------------------


def make_authed_user(
    *, tenant_id: str, user_id: str, role: str = "owner"
) -> Any:
    """Build an AuthenticatedUser that can impersonate an attacker across
    tenant boundaries. Tests use this to drive the FastAPI dependency
    override path, NOT to actually bypass auth — the REST endpoint
    itself must reject cross-tenant access."""
    from rolemesh.auth.provider import AuthenticatedUser

    return AuthenticatedUser(
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,
        email="x@x.com",
        name="X",
    )


# ---------------------------------------------------------------------------
# Skip markers for conditionally-available dependencies
# ---------------------------------------------------------------------------


def _has_safety_ml() -> bool:
    """True when the [safety-ml] extra is installed (llm-guard + presidio).

    Slow ML checks are gated behind this to keep the default pytest run
    lightweight. The attack simulation suite declares pytest.importorskip
    on the specific module each test needs.
    """
    try:
        import llm_guard  # noqa: F401

        return True
    except ImportError:
        return False


skip_without_safety_ml = pytest.mark.skipif(
    not _has_safety_ml(),
    reason="requires [safety-ml] extra; install with 'uv sync --extra safety-ml'",
)


# ---------------------------------------------------------------------------
# Tell pytest to mark every test in this directory
# ---------------------------------------------------------------------------

# A subset of attack tests need a real DB to drive engine paths. Those
# files declare ``pytestmark = pytest.mark.usefixtures("test_db")``
# explicitly; spec-level tests avoid the DB for speed.
_ = os  # keep import for future env-var-driven skips
