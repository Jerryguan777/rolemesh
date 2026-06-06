"""``/api/v1/credentials`` REST surface (design §3 Phase 2, §8.1).

Tenant-plane resource (tenant is implicit, derived from the session —
no ``/tenant/`` path prefix; cf. the platform-plane counterpart at
``/api/v1/platform/credentials``).

Stores tenant-scoped LLM provider API keys behind envelope
encryption. ``GET`` returns metadata only — the encrypted payload
never leaves the DB column. ``PUT`` runs the plaintext through
:class:`rolemesh.auth.credential_vault.CredentialVault` before
INSERT/UPDATE and publishes one ``web.coworker.restart`` event per
affected coworker so the orchestrator hot-reloads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Response

from rolemesh.auth.credential_vault import get_credential_vault
from rolemesh.core.logger import get_logger
from rolemesh.db import (
    CredentialRow,
    delete_tenant_credential,
    get_coworker_ids_for_tenant_provider,
    list_tenant_credentials,
    set_tenant_credential_pool,
    upsert_tenant_credential,
)
from webui.dependencies import require_action
from webui.schemas_v1 import CredentialResponse, CredentialUpsert, ModelProvider
from webui.v1 import coworker_events
from webui.v1._log_sanitize import sanitize_for_log
from webui.v1.errors import raise_error_response

if TYPE_CHECKING:
    from rolemesh.auth.provider import AuthenticatedUser

logger = get_logger()

router = APIRouter(prefix="/credentials", tags=["Credentials"])


def _credential_to_response(row: CredentialRow) -> CredentialResponse:
    return CredentialResponse(
        provider=row.provider,  # type: ignore[arg-type]
        mode=row.mode,  # type: ignore[arg-type]
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


async def _restart_affected_coworkers(
    *, tenant_id: str, provider: str, reason: str
) -> None:
    """Publish one ``web.coworker.restart`` per coworker on ``provider``.

    Best-effort: a publish failure is logged but does not fail the
    request — the DB row is the source of truth and the next process
    boot picks it up. Shared by the byok-PUT and pool-election paths
    since both change which key a running coworker resolves to.
    """
    coworker_ids = await get_coworker_ids_for_tenant_provider(
        tenant_id=tenant_id, provider=provider,
    )
    for cid in coworker_ids:
        try:
            await coworker_events.publish_coworker_restart(
                coworker_id=cid, tenant_id=tenant_id,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "Failed to publish web.coworker.restart",
                reason=reason,
                coworker_id=cid,
                tenant_id=tenant_id,
                provider=provider,
                exc_info=True,
            )


@router.get("", response_model=list[CredentialResponse])
async def list_credentials_endpoint(
    user: AuthenticatedUser = Depends(require_action("credential.byok.manage")),
) -> list[CredentialResponse]:
    rows = await list_tenant_credentials(user.tenant_id)
    return [_credential_to_response(r) for r in rows]


@router.put("/{provider}", response_model=CredentialResponse)
async def put_credential_endpoint(
    provider: ModelProvider,
    body: CredentialUpsert,
    user: AuthenticatedUser = Depends(require_action("credential.byok.manage")),
) -> CredentialResponse:
    """Set or rotate the credential for one provider.

    The plaintext ``api_key`` is encrypted by the process-wide
    :class:`CredentialVault` before any DB write. Logging is done
    against a sanitised view so an accidental ``logger.info(body)``
    elsewhere in the stack cannot leak the key.

    On success we look up every coworker in the tenant whose model
    uses ``provider`` and publish one ``web.coworker.restart`` event
    per coworker. The events are best-effort: a publish failure is
    logged but does not fail the request — the DB row is the source
    of truth and the next process boot picks it up.
    """
    logger.info(
        "PUT credential",
        tenant_id=user.tenant_id,
        provider=provider,
        body=sanitize_for_log(body.model_dump()),
    )

    payload: dict[str, object] = {"api_key": body.api_key}
    if body.extras:
        payload["extras"] = body.extras

    vault = get_credential_vault()
    blob = vault.encrypt_json(payload)
    row = await upsert_tenant_credential(
        tenant_id=user.tenant_id,
        provider=provider,
        credential_data=blob,
    )

    await _restart_affected_coworkers(
        tenant_id=user.tenant_id, provider=provider, reason="credential PUT",
    )

    return _credential_to_response(row)


@router.put("/{provider}/pool", response_model=CredentialResponse)
async def elect_pool_endpoint(
    provider: ModelProvider,
    user: AuthenticatedUser = Depends(require_action("credential.byok.manage")),
) -> CredentialResponse:
    """Elect the platform credential pool for one provider.

    Sets the row's mode to ``'pool'`` so the resolver uses the platform
    key instead of the tenant's own. Any existing BYOK ciphertext is
    retained *dormant* (the tenant can flip back via ``PUT /{provider}``
    without re-entering it). This is the explicit opt-in: until a tenant
    calls this (or sets a BYOK key) the provider stays unconfigured and
    agents on it fail closed — the platform pool is never consumed
    silently.

    Like the BYOK path, we restart every affected coworker so a running
    agent picks up the new key source.
    """
    row = await set_tenant_credential_pool(
        tenant_id=user.tenant_id, provider=provider,
    )
    await _restart_affected_coworkers(
        tenant_id=user.tenant_id, provider=provider, reason="pool election",
    )
    return _credential_to_response(row)


@router.delete("/{provider}", status_code=204)
async def delete_credential_endpoint(
    provider: ModelProvider,
    user: AuthenticatedUser = Depends(require_action("credential.byok.manage")),
) -> Response:
    """Delete a credential.

    Returns 409 with ``RESOURCE_IN_USE`` (carrying the offending
    coworker ids in ``details``) when at least one coworker still
    references this provider. Computing the reference list before
    the DELETE is the right order — checking afterwards races with a
    concurrent POST coworker that adds a new reference, and the
    409-after-success would already have an unrecoverable user
    experience.
    """
    referencing = await get_coworker_ids_for_tenant_provider(
        tenant_id=user.tenant_id, provider=provider,
    )
    if referencing:
        raise_error_response(
            "RESOURCE_IN_USE",
            (
                f"Credential is in use by {len(referencing)} "
                f"coworker(s); detach them before deleting."
            ),
            status_code=409,
            details={"coworker_ids": referencing, "provider": provider},
        )
    removed = await delete_tenant_credential(
        tenant_id=user.tenant_id, provider=provider,
    )
    if not removed:
        raise_error_response(
            "NOT_FOUND",
            "Credential not found.",
            status_code=404,
            details={"provider": provider},
        )
    return Response(status_code=204)
