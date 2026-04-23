"""LLM-Guard-backed prompt injection detector.

Slow check: runs a transformer classifier on the incoming user prompt
and blocks when the injection score crosses the threshold. Registered
only to the orchestrator-side registry (it's slow + has a heavy
dependency graph that the container image doesn't ship). The
container reaches it via ``RemoteCheck`` over the safety.detect NATS
RPC channel.

Stable code set: ``PROMPT_INJECTION`` — singular because a detected
event is just "classifier said yes". Score is surfaced via
``Finding.metadata`` so dashboards can filter aggressively below the
block threshold if desired.

V2 P1.2 scope: INPUT_PROMPT only. The same classifier could run on
MODEL_OUTPUT to catch reflection-style injections that echo user
text back, but that's a distinct check (different stage, different
false-positive profile) — defer to a follow-up.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, StrictFloat

from ..types import CostClass, Finding, Stage, Verdict

if TYPE_CHECKING:
    from ..types import SafetyContext


class LLMGuardPromptInjectionConfig(BaseModel):
    """Rule config schema.

    ``threshold`` gates how aggressive the block is. The model's score
    is in [0.0, 1.0]; the docs suggest 0.9 as a conservative default
    (low false-positive rate, still catches most actual injections).
    Per-tenant tuning: lower threshold → more blocks.
    """

    model_config = ConfigDict(extra="forbid")

    threshold: StrictFloat = 0.9
    action_override: str | None = None


class LLMGuardPromptInjectionCheck:
    id: str = "llm_guard.prompt_injection"
    version: str = "1"
    stages: frozenset[Stage] = frozenset({Stage.INPUT_PROMPT})
    cost_class: CostClass = "slow"
    supported_codes: frozenset[str] = frozenset({"PROMPT_INJECTION"})
    config_model: type[BaseModel] = LLMGuardPromptInjectionConfig
    # _sync = True → SafetyRpcServer dispatches to the thread pool so
    # the transformer inference doesn't block the asyncio loop.
    _sync: bool = True

    def __init__(self, default_threshold: float = 0.9) -> None:
        # Lazy import here (not at module level) so the orchestrator
        # can decide at registry-build time whether llm-guard is
        # installed — see build_orchestrator_registry's ImportError
        # suppress. Once we're inside __init__ a missing dep is a
        # loud AttributeError rather than a silent degrade, which is
        # correct: the operator asked for this check by name.
        from llm_guard.input_scanners import PromptInjection

        self._scanner = PromptInjection(threshold=default_threshold)
        self._default_threshold = default_threshold

    async def check(
        self, ctx: SafetyContext, config: dict[str, Any]
    ) -> Verdict:
        prompt = str(ctx.payload.get("prompt", ""))
        if not prompt.strip():
            return Verdict(action="allow")
        threshold = float(
            config.get("threshold") or self._default_threshold
        )
        # llm-guard returns (sanitized_text, is_valid, score). For this
        # scanner sanitized_text == input (it doesn't rewrite); we
        # only need the score + boolean.
        _sanitized, is_valid, score = self._scanner.scan(prompt)
        if is_valid and score < threshold:
            return Verdict(action="allow")
        return Verdict(
            action="block",
            reason=(
                f"Prompt injection detected "
                f"(score={score:.3f}, threshold={threshold:.3f})"
            ),
            findings=[
                Finding(
                    code="PROMPT_INJECTION",
                    severity="high",
                    message=(
                        f"llm-guard classifier flagged the prompt "
                        f"(score={score:.3f})"
                    ),
                    metadata={
                        "score": float(score),
                        "threshold": float(threshold),
                    },
                )
            ],
        )


__all__ = [
    "LLMGuardPromptInjectionCheck",
    "LLMGuardPromptInjectionConfig",
]
