"""INV-4 pinned test: ``resolve_actor_user_id`` produces a real UUID
suitable for the audit FK column, or fails loudly.

Anti-mirror discipline:
- The expected behavior was written down BEFORE the helper was
  implemented; the tests below describe what we want, not what the
  current code happens to do.
- No mocks: we use the real testcontainer Postgres so the resolver
  hits a real ``users`` table (including FK and indexes). A mock
  would not catch a regression where the query starts returning
  ``NULL`` rows under, e.g., a typo in the WHERE clause.
"""

from __future__ import annotations

import uuid

import pytest

from rolemesh.auth.bootstrap_actor import (
    BootstrapActorError,
    resolve_actor_user_id,
)
from rolemesh.db import create_tenant, create_user

pytestmark = pytest.mark.usefixtures("test_db")


async def _make_tenant() -> str:
    t = await create_tenant(
        name="acme", slug=f"acme-{uuid.uuid4().hex[:8]}"
    )
    return t.id


async def test_real_uuid_is_returned_unchanged() -> None:
    tenant_id = await _make_tenant()
    real = await create_user(tenant_id, name="alice", role="member")
    # Some other user happens to be the caller — should pass through
    # exactly as given, without any DB round-trip (we can't assert
    # "no round-trip" here without mocking; we assert the return
    # value contract instead).
    out = await resolve_actor_user_id(tenant_id, real.id)
    assert out == real.id
    # And: the returned string is itself a valid UUID, so the
    # audit-FK ::uuid cast won't blow up downstream.
    uuid.UUID(out)


async def test_bootstrap_literal_resolves_to_first_owner() -> None:
    tenant_id = await _make_tenant()
    # Insert two owners in a known order so we can pin which one is
    # picked (the *oldest* by created_at, per the resolver contract).
    first = await create_user(tenant_id, name="owner1", role="owner")
    second = await create_user(tenant_id, name="owner2", role="owner")
    out = await resolve_actor_user_id(tenant_id, "bootstrap")
    assert out == first.id
    assert out != second.id


async def test_bootstrap_literal_with_no_owner_raises_503_error() -> None:
    tenant_id = await _make_tenant()
    # Only a non-owner exists — the owner-only lookup must fail.
    await create_user(tenant_id, name="member1", role="member")
    with pytest.raises(BootstrapActorError) as excinfo:
        await resolve_actor_user_id(tenant_id, "bootstrap")
    # The exception carries the structured fields the HTTP handler
    # surfaces — pin them here so the contract is locked.
    assert excinfo.value.code == "BOOTSTRAP_NEEDS_TENANT_OWNER"
    assert excinfo.value.status == 503
    assert excinfo.value.tenant_id == tenant_id


async def test_non_uuid_non_bootstrap_string_is_treated_as_bootstrap() -> None:
    # If a future regression introduces a second pseudo-user, the
    # resolver must NOT slip it through as a "valid UUID"; anything
    # that does not pass ``uuid.UUID`` should land on the bootstrap
    # path. Pin that fallback so the failure mode is loud.
    tenant_id = await _make_tenant()
    owner = await create_user(tenant_id, name="owner-x", role="owner")
    out = await resolve_actor_user_id(tenant_id, "system")
    assert out == owner.id


async def test_real_uuid_in_different_tenant_still_passes_through() -> None:
    # The resolver does NOT cross-check that the UUID belongs to the
    # tenant — it only normalizes the bootstrap literal. If a real
    # UUID is passed in, return it. Cross-tenant FK checks live in
    # the DB.
    tenant_a = await _make_tenant()
    tenant_b = await _make_tenant()
    user_b = await create_user(tenant_b, name="bob", role="member")
    out = await resolve_actor_user_id(tenant_a, user_b.id)
    assert out == user_b.id


async def test_bootstrap_with_only_admin_role_raises() -> None:
    # The lookup is owner-only. ``admin`` is a separate role and must
    # not silently substitute for owner — that would silently change
    # the audit attribution semantics across deployments.
    tenant_id = await _make_tenant()
    await create_user(tenant_id, name="admin1", role="admin")
    with pytest.raises(BootstrapActorError):
        await resolve_actor_user_id(tenant_id, "bootstrap")
