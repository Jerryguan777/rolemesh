"""Backend × provider × family compatibility matrix.

Design ref: §2.3. The matrix is a code constant (not a DB table)
because it represents *what code is implemented*, not configurable
data: e.g. Claude Agent SDK only knows how to talk to Anthropic-
family models, so picking ``backend=claude`` + ``family=gpt`` cannot
work no matter what credentials are in the DB.

Why a 2-D matrix and not a single enum: Bedrock alone is not enough
information — Bedrock can serve Claude *and* Llama. We need
``(provider, family)`` together to pick a code path. The single
exception is Pi, which accepts any family from its supported
providers; ``supported_model_families = None`` encodes "unrestricted".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class BackendCapability:
    """One row of the matrix: a backend and what it accepts."""

    name: str
    supported_providers: frozenset[str]
    # ``None`` means: any family that the provider itself offers.
    supported_model_families: frozenset[str] | None
    description: str


CLAUDE_BACKEND: Final[BackendCapability] = BackendCapability(
    name="claude",
    supported_providers=frozenset({"anthropic", "bedrock"}),
    supported_model_families=frozenset({"claude"}),
    description="Claude Agent SDK — Anthropic-family models only.",
)

PI_BACKEND: Final[BackendCapability] = BackendCapability(
    name="pi",
    supported_providers=frozenset({"anthropic", "openai", "google", "bedrock"}),
    supported_model_families=None,
    description="Pi runtime — multi-provider, multi-family.",
)

ALL_BACKENDS: Final[dict[str, BackendCapability]] = {
    b.name: b for b in (CLAUDE_BACKEND, PI_BACKEND)
}


class BackendCompatError(ValueError):
    """Raised when a (backend, provider, family) triple is unsupported.

    REST handler maps this to HTTP 400 with ``code="BACKEND_INCOMPAT"``.
    The choice of 400 over 422 is recorded in 00a-inv-foundations.md
    open questions — "combination not implemented" is closer to a
    bad request than a schema validation failure.
    """

    code: Final[str] = "BACKEND_INCOMPAT"
    status: Final[int] = 400


def validate_combo(backend_name: str, provider: str, family: str) -> None:
    """Raise ``BackendCompatError`` if the triple is unsupported.

    Unknown backend / provider / family strings all collapse into
    ``BackendCompatError`` rather than ``KeyError`` — the caller is
    a REST handler that should not need to disambiguate.
    """
    b = ALL_BACKENDS.get(backend_name)
    if b is None:
        raise BackendCompatError(
            f"unknown backend {backend_name!r}; "
            f"known: {sorted(ALL_BACKENDS)}"
        )
    if provider not in b.supported_providers:
        raise BackendCompatError(
            f"backend {backend_name!r} does not support provider "
            f"{provider!r}; supported: {sorted(b.supported_providers)}"
        )
    if (
        b.supported_model_families is not None
        and family not in b.supported_model_families
    ):
        raise BackendCompatError(
            f"backend {backend_name!r} does not support model family "
            f"{family!r}; supported: "
            f"{sorted(b.supported_model_families)}"
        )


def backends_as_json() -> list[dict[str, object]]:
    """Public-facing JSON projection of ``ALL_BACKENDS``.

    ``frozenset`` is not JSON-serialisable on its own; ``None`` for
    ``supported_model_families`` is preserved verbatim so the
    frontend can render "all families allowed" without having to
    enumerate them.
    """
    out: list[dict[str, object]] = []
    for b in ALL_BACKENDS.values():
        out.append(
            {
                "name": b.name,
                "description": b.description,
                "supported_providers": sorted(b.supported_providers),
                "supported_model_families": (
                    sorted(b.supported_model_families)
                    if b.supported_model_families is not None
                    else None
                ),
            }
        )
    return out
