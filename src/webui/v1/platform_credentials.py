"""``/api/v1/platform/credentials`` REST surface (credential pool §5).

The platform credential pool holds one Fernet-encrypted key per
provider that any tenant electing ``credential_mode = 'pool'`` resolves
against. This surface is platform-plane only: every handler is gated on
``credential.pool.manage``, which lives in ``_PLATFORM_ONLY_ACTIONS`` so
no tenant role can reach it (see :mod:`rolemesh.auth.permissions`).

Mirrors the tenant credentials module's secret posture — ``GET``
returns metadata only, ``PUT`` runs the plaintext through the same
process-wide :class:`CredentialVault` tenants use (so the resolver
decrypts pool and byok blobs with one key). Unlike the tenant path,
there is no per-coworker restart fan-out: a pool key change affects
every tenant electing it, so we let the resolver's 60s cache TTL carry
the rotation rather than enumerate the world.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Response

from rolemesh.auth.credential_vault import get_credential_vault
from rolemesh.core.logger import get_logger
from rolemesh.db import (
    PlatformCredentialRow,
    delete_platform_credential,
    list_platform_credentials,
    upsert_platform_credential,
)
from webui.dependencies import require_action
from webui.schemas_v1 import (
    CredentialUpsert,
    ModelProvider,
    PlatformCredentialResponse,
)
from webui.v1._log_sanitize import sanitize_for_log
from webui.v1.errors import raise_error_response

if TYPE_CHECKING:
    from rolemesh.auth.provider import AuthenticatedUser

logger = get_logger()

router = APIRouter(prefix="/platform/credentials", tags=["Platform"])


def _to_response(row: PlatformCredentialRow) -> PlatformCredentialResponse:
    return PlatformCredentialResponse(
        provider=row.provider,  # type: ignore[arg-type]
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


@router.get("", response_model=list[PlatformCredentialResponse])
async def list_platform_credentials_endpoint(
    user: AuthenticatedUser = Depends(require_action("credential.pool.manage")),
) -> list[PlatformCredentialResponse]:
    rows = await list_platform_credentials()
    return [_to_response(r) for r in rows]


@router.put("/{provider}", response_model=PlatformCredentialResponse)
async def put_platform_credential_endpoint(
    provider: ModelProvider,
    body: CredentialUpsert,
    user: AuthenticatedUser = Depends(require_action("credential.pool.manage")),
) -> PlatformCredentialResponse:
    """Set or rotate the platform pool key for one provider.

    The plaintext ``api_key`` is encrypted before any DB write; logging
    runs against a sanitised view so an accidental ``logger.info(body)``
    cannot leak the key. Tenants electing ``pool`` for this provider
    pick up the new key within the resolver's cache TTL.
    """
    logger.info(
        "PUT platform credential",
        provider=provider,
        body=sanitize_for_log(body.model_dump()),
    )

    payload: dict[str, object] = {"api_key": body.api_key}
    if body.extras:
        payload["extras"] = body.extras

    vault = get_credential_vault()
    blob = vault.encrypt_json(payload)
    row = await upsert_platform_credential(provider=provider, credential_data=blob)
    return _to_response(row)


@router.delete("/{provider}", status_code=204)
async def delete_platform_credential_endpoint(
    provider: ModelProvider,
    user: AuthenticatedUser = Depends(require_action("credential.pool.manage")),
) -> Response:
    """Remove the platform pool key for one provider.

    Tenant rows that elected ``pool`` for this provider are left intact;
    their next resolve fails closed until a key is re-added. Returns 404
    if no pool key existed.
    """
    removed = await delete_platform_credential(provider=provider)
    if not removed:
        raise_error_response(
            "NOT_FOUND",
            "Platform credential not found.",
            status_code=404,
            details={"provider": provider},
        )
    return Response(status_code=204)
