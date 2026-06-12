"""Stateless signed identity tokens for the egress gateway HTTP planes.

This is the replacement for the source-IP -> :class:`Identity` lookup
that ``identity.py`` provides today. Instead of inferring who a request
came from by mapping its bridge IP through a NATS-fed in-memory table,
the orchestrator *mints* a signed token at spawn time and injects it
into the agent container; the gateway *verifies* the token on each
request and reads the identity straight out of it.

Why this exists
---------------

The IP scheme couples identity to L3 topology (breaks under NAT / k8s /
multi-host) and to a distributed-state pipeline (lifecycle events +
snapshot RPC + two in-memory maps) whose every failure mode is a
silent 401. A signed token travels *in-band* with the request, so the
gateway needs no shared state and no event stream — verification is a
pure function of the token plus a shared secret.

Token shape
-----------

    base64url(claims_json) "." base64url(HMAC-SHA256(secret, payload))

Both halves use URL-safe base64 without padding, and the ``.`` joiner
is safe in every place we embed the token:

  * forward proxy — ``HTTP_PROXY=http://job:<token>@gateway:3128``; the
    token rides in the userinfo password position and the client emits
    ``Proxy-Authorization: Basic ...`` automatically.
  * reverse proxy — ``ANTHROPIC_BASE_URL=.../proxy/<token>/anthropic``;
    the token is a path segment the SDK preserves before appending its
    own ``/v1/messages`` suffix.

Trust model
-----------

The secret (``EGRESS_TOKEN_SECRET``) lives only on the orchestrator and
the gateway — never inside an agent container. A compromised agent can
read and replay *its own* token (it is a bearer credential by
construction) but cannot forge a token for another tenant without the
secret. The token is therefore as strong as keeping that secret off the
agent filesystem, plus a TTL bound (see :class:`TokenAuthority`).

Fail-closed: any malformed / mis-signed / expired token yields
``None`` from :meth:`verify`, which callers translate into a 401/407 —
the same posture as an unknown source IP in the old scheme.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass

from rolemesh.core.logger import get_logger

logger = get_logger()


@dataclass(frozen=True)
class Identity:
    """Authoritative view of which agent a request came from.

    Carried inside the signed egress token and reconstructed by the
    gateway on ``verify``. Frozen so a shared record can't be mutated
    in flight through the safety pipeline. All fields are strings
    because the downstream ``safety_decisions`` table keys off strings;
    asking Identity to know about UUID types would force an ``asyncpg``
    import into every gateway code path.

    ``container_name`` is retained for audit/debug continuity even
    though the token scheme no longer needs a container→identity index
    (that was the source-IP resolver's job, now removed).
    """

    tenant_id: str
    coworker_id: str
    user_id: str
    conversation_id: str
    job_id: str
    container_name: str


# Env knobs. The secret has no default — its absence under EC must
# fail the process at startup, not silently mint unsigned-equivalent
# tokens. The TTL has a safe default; see the class docstring for the
# orchestrator-side recycling that keeps long-lived sessions inside it.
SECRET_ENV = "EGRESS_TOKEN_SECRET"
TTL_ENV = "EGRESS_TOKEN_TTL_SECONDS"

# 7 days. A bearer credential should not outlive a week even if the
# orchestrator's container-recycling logic (which re-mints well before
# this) has a bug. Operators can shorten it; lengthening past a week
# should be a deliberate, reviewed change.
_DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60

# Clock-skew grace applied to the expiry check. The orchestrator and
# gateway share a host clock in the single-box deployment, but a few
# seconds of tolerance costs nothing and avoids a spurious 401 at the
# exact tick of expiry.
_EXP_SKEW_SECONDS = 30


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    # Restore the padding base64 needs but the token format strips.
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(payload_b64: str, secret: str) -> str:
    sig = hmac.new(
        secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256
    ).digest()
    return _b64url_encode(sig)


def mint(
    identity: Identity,
    *,
    secret: str,
    ttl_seconds: int,
    now: float | None = None,
) -> str:
    """Produce a signed token carrying *identity*.

    ``now`` is injectable so tests can pin issuance time; production
    passes ``None`` and gets wall-clock seconds.
    """
    issued = int(now if now is not None else time.time())
    claims = {
        "tenant_id": identity.tenant_id,
        "coworker_id": identity.coworker_id,
        "user_id": identity.user_id,
        "conversation_id": identity.conversation_id,
        "job_id": identity.job_id,
        "container_name": identity.container_name,
        "iat": issued,
        "exp": issued + int(ttl_seconds),
    }
    payload_b64 = _b64url_encode(
        json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    return f"{payload_b64}.{_sign(payload_b64, secret)}"


def verify(
    token: str,
    *,
    secret: str,
    now: float | None = None,
) -> Identity | None:
    """Return the :class:`Identity` a valid token carries, else ``None``.

    Every failure path returns ``None`` (fail-closed). The reason is
    logged at INFO without the token contents or the secret — enough
    to tell "expired" from "bad signature" from "malformed" in the
    gateway logs, never enough to leak the credential.
    """
    if not token:
        return None

    parts = token.split(".")
    if len(parts) != 2:
        logger.info("token: malformed (not two segments)", segments=len(parts))
        return None
    payload_b64, sig_b64 = parts

    # Verify the MAC before trusting any byte of the payload.
    expected = _sign(payload_b64, secret)
    if not hmac.compare_digest(expected, sig_b64):
        logger.info("token: bad signature")
        return None

    try:
        claims = json.loads(_b64url_decode(payload_b64))
    except (ValueError, TypeError) as exc:
        logger.info("token: undecodable payload", error=str(exc))
        return None
    if not isinstance(claims, dict):
        logger.info("token: payload not an object")
        return None

    exp = claims.get("exp")
    if not isinstance(exp, int):
        logger.info("token: missing/!int exp")
        return None
    current = now if now is not None else time.time()
    if current > exp + _EXP_SKEW_SECONDS:
        logger.info("token: expired", exp=exp)
        return None

    try:
        return Identity(
            tenant_id=str(claims["tenant_id"]),
            coworker_id=str(claims["coworker_id"]),
            user_id=str(claims.get("user_id", "")),
            conversation_id=str(claims.get("conversation_id", "")),
            job_id=str(claims.get("job_id", "")),
            container_name=str(claims.get("container_name", "")),
        )
    except KeyError as exc:
        logger.info("token: missing required claim", claim=str(exc))
        return None


@dataclass(frozen=True)
class TokenAuthority:
    """Bundles the shared secret + TTL so call sites mint/verify without
    threading two parameters through every signature.

    The orchestrator builds one at startup and mints from it; the
    gateway builds one at startup and verifies with it. Both load the
    same secret from the environment (the gateway bind-mounts the same
    ``.env``), so no key distribution channel is added.
    """

    secret: str
    ttl_seconds: int = _DEFAULT_TTL_SECONDS

    def mint(self, identity: Identity, *, now: float | None = None) -> str:
        return mint(
            identity, secret=self.secret, ttl_seconds=self.ttl_seconds, now=now
        )

    def verify(self, token: str, *, now: float | None = None) -> Identity | None:
        return verify(token, secret=self.secret, now=now)

    @classmethod
    def from_env(cls) -> TokenAuthority:
        """Build from ``EGRESS_TOKEN_SECRET`` / ``EGRESS_TOKEN_TTL_SECONDS``.

        Raises ``ValueError`` when the secret is absent or trivially
        short. Both the orchestrator and the gateway call this at
        startup under EC, so a missing secret fails the boot loudly
        instead of degrading to a path where identity can't be
        established.
        """
        secret = os.environ.get(SECRET_ENV, "").strip()
        if len(secret) < 16:
            raise ValueError(
                f"{SECRET_ENV} must be set to a secret of at least 16 chars "
                "when egress control is enabled (orchestrator mints and the "
                "gateway verifies agent identity tokens with it)"
            )

        raw_ttl = os.environ.get(TTL_ENV, "").strip()
        if raw_ttl:
            try:
                ttl = int(raw_ttl)
            except ValueError as exc:
                raise ValueError(f"{TTL_ENV} must be an integer: {raw_ttl!r}") from exc
            if ttl <= 0:
                raise ValueError(f"{TTL_ENV} must be positive, got {ttl}")
        else:
            ttl = _DEFAULT_TTL_SECONDS

        logger.info("token authority loaded", ttl_seconds=ttl)
        return cls(secret=secret, ttl_seconds=ttl)


__all__ = [
    "SECRET_ENV",
    "TTL_ENV",
    "Identity",
    "TokenAuthority",
    "mint",
    "verify",
]
