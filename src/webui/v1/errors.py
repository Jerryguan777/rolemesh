"""Uniform ``{code, message, details?}`` error envelope for ``/api/v1``.

Design §13 mandates the same JSON shape across every ``/api/v1`` error
response. FastAPI's ``HTTPException(detail=...)`` ships the entire
``detail`` value as the ``detail`` *field* of a top-level object — so
``raise HTTPException(status_code=409, detail={"code": ..., ...})``
serialises to ``{"detail": {"code": ..., ...}}``, not ``{"code": ...,
"message": ...}``. The typed client decoded against the
``ErrorResponse`` schema would then see an extra wrapping layer and
discard the structured fields.

This module ships two pieces that together flatten that envelope:

* ``ErrorResponseException`` — an ``HTTPException`` subclass carrying
  the structured envelope alongside the HTTP status.
* ``install_error_handler`` — registers an exception handler on the
  app that re-serialises ``ErrorResponseException.envelope`` as the
  *root* JSON body, matching ``ErrorResponse`` in
  :mod:`webui.schemas_v1`.

Handlers raise via :func:`raise_error_response`. The helper exists so
the call site doesn't have to remember either the wrapping pitfall or
the field names — and so we have a single grep target for "every
``/api/v1`` 4xx".
"""

from __future__ import annotations

from typing import NoReturn

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

__all__ = [
    "KNOWN_ERROR_CODES",
    "ErrorResponseException",
    "install_error_handler",
    "raise_error_response",
]


# Authoritative catalog of every ``code`` that can appear in an
# ``ErrorResponse`` on the ``/api/v1`` surface. This is the HTTP error
# vocabulary ONLY — safety-check *finding* codes (JAILBREAK, TOXICITY,
# PROMPT_INJECTION, DOMAIN_NOT_ALLOWED, …) are a separate vocabulary that
# lives on the ``SafetyFinding`` model, not on ``ErrorResponse.code``, and
# is intentionally not listed here.
#
# A drift guard (``tests/webui/test_v1_error_codes.py``) keeps this honest:
# every ``code`` literal raised under ``src/webui`` must be in this set, the
# set must have no dead entries, and the OpenAPI ``ErrorResponse`` doc must
# mention each one. When you introduce a new error code: add it here, raise
# it, and list it in ``contracts/openapi.yaml``'s ``ErrorResponse`` schema.
KNOWN_ERROR_CODES: frozenset[str] = frozenset({
    # Generic transport / CRUD.
    "NOT_FOUND",
    "FORBIDDEN",
    "CONFLICT",
    "INVALID_REQUEST",
    "RESOURCE_IN_USE",
    "RESOURCE_NOT_AVAILABLE",
    "ALREADY_TERMINAL",
    "TENANT_SUSPENDED",
    # Coworker / model / credential validation.
    "MODEL_NOT_FOUND",
    "MISSING_CREDENTIAL",
    "BACKEND_INCOMPAT",  # raised via BackendCompatError.code (non-literal)
    # Skills.
    "INVALID_NAME",
    "INVALID_PATH",
    "INVALID_PAYLOAD",
    "INVALID_MANIFEST",
    "SKILL_MANIFEST_REQUIRED",
    "SKILL_MANIFEST_PROTECTED",
    # Safety rules (tenant + platform).
    "INVALID_RULE",
    "SEEDED_RULE_IMMUTABLE",
    # Pagination / channel links / WS ticket.
    "INVALID_CURSOR",
    "ACTOR_NOT_LINKABLE",
    "WS_TICKET_SECRET_UNSET",  # raised via WsTicketError.code (non-literal)
})


class ErrorResponseException(HTTPException):
    """``HTTPException`` that flattens to the design §13 envelope.

    Stores the structured envelope as ``self.envelope`` so the
    application-level handler can serve it verbatim. ``self.detail``
    is set to a plain summary string so the default FastAPI machinery
    (which logs ``detail``) keeps producing readable log lines.
    """

    envelope: dict[str, object]

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=message)
        envelope: dict[str, object] = {"code": code, "message": message}
        if details is not None:
            envelope["details"] = details
        self.envelope = envelope


def raise_error_response(
    code: str,
    message: str,
    *,
    status_code: int,
    details: dict[str, object] | None = None,
) -> NoReturn:
    """Raise an ``ErrorResponseException`` with the design §13 envelope.

    Keyword-only ``status_code`` so callsites read as
    ``raise_error_response("RESOURCE_IN_USE", "...", status_code=409,
    details={...})`` instead of accidentally swapping the HTTP status
    with the human-readable message.
    """
    raise ErrorResponseException(
        status_code=status_code,
        code=code,
        message=message,
        details=details,
    )


def install_error_handler(app: FastAPI) -> None:
    """Wire the flat-envelope handler onto ``app``.

    Idempotent: registering twice on the same app just overwrites the
    same key in FastAPI's handler map. Tests that build a transient
    app per request can call this freely.
    """

    @app.exception_handler(ErrorResponseException)
    async def _handler(_request: Request, exc: ErrorResponseException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.envelope)
