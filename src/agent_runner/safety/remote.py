"""Container-side ``RemoteCheck`` — proxies a slow check to the orchestrator.

The container safety registry holds cheap checks as real objects and
slow checks as ``RemoteCheck`` instances. Both satisfy the
``SafetyCheck`` protocol — the pipeline does not distinguish. The
proxy's ``check()`` implementation sends a NATS request on
``agent.{job_id}.safety.detect`` and awaits the orchestrator's reply.

Fail-mode policy (per V2 P0.3 design):

  - **Timeout** — transport-level timeout → fail-open with a
    ``SAFETY.RPC_TIMEOUT`` critical finding. A slow check is not
    authoritative; treating its outage as a block would turn a normal
    orchestrator restart into an agent outage. The critical finding
    makes the event visible in audit dashboards so operators can react.
  - **Transport error** (``NoRespondersError``, broken connection,
    etc.) — same fail-open + critical finding posture. Code path
    rolls up under ``OSError`` / ``RuntimeError`` in the nats-py
    client.
  - **Malformed reply** (not JSON, missing ``verdict`` key, etc.) —
    fail-open + critical finding. The orchestrator wouldn't normally
    emit this, but a half-written bytes payload during a crash is
    survivable without taking the agent down.
  - **Orchestrator error field set** — the remote raised an exception
    executing the check. Same fail-open + critical finding.

The class never fails closed because the pipeline's own fail-close /
fail-safe semantics already drive control-vs-observational behavior
from the ``Verdict`` the check returns. A slow check proxy reporting
"remote broken" surfaces as a per-rule critical finding without
changing the pipeline's ultimate decision on OTHER (still-running)
rules at the same stage.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

from rolemesh.safety.rpc_codec import deserialize_verdict, serialize_context
from rolemesh.safety.types import (
    CostClass,
    Finding,
    SafetyObservabilityCode,
    Stage,
    Verdict,
)

if TYPE_CHECKING:
    from rolemesh.safety.types import SafetyContext


# Subject template for slow-check RPC. Orchestrator subscribes with a
# wildcard ``agent.*.safety.detect``.
DETECT_SUBJECT_TEMPLATE = "agent.{job_id}.safety.detect"


class RemoteCheck:
    """Stand-in SafetyCheck that forwards to the orchestrator.

    ``config_model`` is deliberately ``None``: the real pydantic model
    lives alongside the remote check implementation on the orchestrator.
    REST-layer validation happens there (the REST handler is
    co-located with the orch registry), so clients never see the
    remote model in this proxy.

    ``_sync`` / per-check thread-pool execution is an orchestrator-side
    concern; the container just sees an async RPC.
    """

    def __init__(
        self,
        *,
        check_id: str,
        version: str,
        stages: frozenset[Stage],
        cost_class: CostClass,
        supported_codes: frozenset[str],
        nats_client: Any,
        default_timeout_ms: int = 1500,
    ) -> None:
        self.id = check_id
        self.version = version
        self.stages = stages
        self.cost_class = cost_class
        self.supported_codes = supported_codes
        # Remote proxy has no local config model — the real schema
        # lives on the orchestrator beside the concrete check.
        self.config_model: Any = None
        self._nc = nats_client
        self._default_timeout_ms = default_timeout_ms

    @classmethod
    def from_spec(
        cls, spec: dict[str, Any], nats_client: Any
    ) -> RemoteCheck:
        """Build a RemoteCheck from the ``AgentInitData.slow_check_specs``
        dict shape. Invoked at container startup once per spec.
        """
        stages = frozenset(
            Stage(str(s)) for s in spec.get("stages") or []
        )
        return cls(
            check_id=str(spec["check_id"]),
            version=str(spec.get("version", "1")),
            stages=stages,
            cost_class=str(spec.get("cost_class", "slow")),  # type: ignore[arg-type]
            supported_codes=frozenset(
                str(c) for c in spec.get("supported_codes") or []
            ),
            nats_client=nats_client,
            default_timeout_ms=int(
                spec.get("default_timeout_ms", 1500)
            ),
        )

    async def check(
        self, ctx: SafetyContext, config: dict[str, Any]
    ) -> Verdict:
        timeout_ms = int(
            config.get("timeout_ms") or self._default_timeout_ms
        )
        request = {
            "request_id": str(uuid.uuid4()),
            "check_id": self.id,
            "config": {
                k: v for k, v in config.items() if not k.startswith("_")
            },
            "context": serialize_context(ctx),
            "deadline_ms": timeout_ms,
        }
        subject = DETECT_SUBJECT_TEMPLATE.format(
            job_id=ctx.job_id or "unknown"
        )
        try:
            msg = await self._nc.request(
                subject,
                json.dumps(request).encode("utf-8"),
                timeout=timeout_ms / 1000.0,
            )
        except TimeoutError:
            return self._fail_open(
                SafetyObservabilityCode.RPC_TIMEOUT,
                f"{self.id} timeout after {timeout_ms}ms",
            )
        except Exception as exc:  # noqa: BLE001 — transport class is too narrow
            return self._fail_open(
                SafetyObservabilityCode.RPC_ERROR,
                f"{self.id} transport error: {exc}",
            )

        try:
            reply = json.loads(msg.data)
        except (json.JSONDecodeError, ValueError) as exc:
            return self._fail_open(
                SafetyObservabilityCode.RPC_ERROR,
                f"{self.id} malformed reply: {exc}",
            )
        if not isinstance(reply, dict):
            return self._fail_open(
                SafetyObservabilityCode.RPC_ERROR,
                f"{self.id} non-dict reply: {type(reply).__name__}",
            )
        if reply.get("error"):
            return self._fail_open(
                SafetyObservabilityCode.RPC_ERROR,
                f"{self.id} remote error: {reply['error']}",
            )
        verdict_data = reply.get("verdict")
        if not isinstance(verdict_data, dict):
            return self._fail_open(
                SafetyObservabilityCode.RPC_ERROR,
                f"{self.id} reply missing verdict",
            )
        return deserialize_verdict(verdict_data)

    @staticmethod
    def _fail_open(
        code: SafetyObservabilityCode | str, message: str
    ) -> Verdict:
        """Shape: allow verdict carrying a single SAFETY.* critical
        finding. Accepts the enum or a raw string (``str(enum)``
        already resolves to the ``SAFETY.*`` value) so call sites
        can use the enum while tests that assert on strings stay
        readable.
        """
        return Verdict(
            action="allow",
            findings=[
                Finding(
                    code=str(code),
                    severity="critical",
                    message=message,
                )
            ],
        )


__all__ = ["DETECT_SUBJECT_TEMPLATE", "RemoteCheck"]
