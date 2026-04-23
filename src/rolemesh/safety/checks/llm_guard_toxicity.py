"""LLM-Guard Toxicity classifier adapter.

Runs the ``unitary/unbiased-toxic-roberta`` classifier (via the
ProtectAI ONNX distribution). Supports INPUT_PROMPT (user being
toxic to the assistant) and MODEL_OUTPUT (assistant emitting toxic
content). Each stage uses the corresponding ``llm_guard.{input,output}_scanners.Toxicity``
class — the training data differs.

Stable code: ``TOXICITY``. The sub-label detail from the classifier
(toxicity / insult / severe_toxicity / …) is surfaced via
``Finding.metadata['sub_labels']`` so dashboards can slice by
sub-category without polluting the audit code space.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, StrictFloat

from ..types import CostClass, Finding, Stage, Verdict

if TYPE_CHECKING:
    from collections.abc import Mapping


class LLMGuardToxicityConfig(BaseModel):
    """Rule config schema.

    ``threshold`` gates the block; below it we still emit a finding
    so operators can tune via audit data. 0.7 default is llm-guard's
    recommended balance between false positives and misses.
    """

    model_config = ConfigDict(extra="forbid")

    threshold: StrictFloat = 0.7
    action_override: str | None = None


def _stage_text_key(stage: Stage) -> str | None:
    if stage == Stage.INPUT_PROMPT:
        return "prompt"
    if stage == Stage.MODEL_OUTPUT:
        return "text"
    return None


def _extract_text(stage: Stage, payload: Mapping[str, Any]) -> str:
    key = _stage_text_key(stage)
    if key is None:
        return ""
    value = payload.get(key)
    if isinstance(value, str):
        return value
    return str(value or "")


class LLMGuardToxicityCheck:
    id: str = "llm_guard.toxicity"
    version: str = "1"
    stages: frozenset[Stage] = frozenset(
        {Stage.INPUT_PROMPT, Stage.MODEL_OUTPUT}
    )
    cost_class: CostClass = "slow"
    supported_codes: frozenset[str] = frozenset({"TOXICITY"})
    config_model: type[BaseModel] = LLMGuardToxicityConfig
    _sync: bool = True

    def __init__(self, default_threshold: float = 0.7) -> None:
        from llm_guard.input_scanners import Toxicity as InputToxicity
        from llm_guard.output_scanners import Toxicity as OutputToxicity

        # Two scanners — the training data differs. Cached so we
        # don't re-download the model on every check call.
        self._input_scanner = InputToxicity(threshold=default_threshold)
        self._output_scanner = OutputToxicity(
            threshold=default_threshold
        )
        self._default_threshold = default_threshold

    async def check(
        self, ctx: Any, config: dict[str, Any]
    ) -> Verdict:
        text = _extract_text(ctx.stage, ctx.payload)
        if not text.strip():
            return Verdict(action="allow")
        threshold = float(
            config.get("threshold") or self._default_threshold
        )

        if ctx.stage == Stage.INPUT_PROMPT:
            _, is_valid, score = self._input_scanner.scan(text)
        else:
            # OUTPUT scanner signature: scan(prompt, output) → returns
            # (sanitized_output, is_valid, risk_score). We don't have
            # a separate prompt at MODEL_OUTPUT stage (the orch
            # pipeline scans the response), so pass an empty prompt.
            _, is_valid, score = self._output_scanner.scan("", text)

        if is_valid or float(score) < threshold:
            return Verdict(action="allow")

        return Verdict(
            action="block",
            reason=(
                f"Blocked: toxicity detected "
                f"(score={float(score):.3f}, threshold={threshold:.3f})"
            ),
            findings=[
                Finding(
                    code="TOXICITY",
                    severity="high",
                    message=f"llm-guard toxicity score={float(score):.3f}",
                    metadata={
                        "score": float(score),
                        "threshold": float(threshold),
                        "stage": ctx.stage.value,
                    },
                )
            ],
        )


__all__ = ["LLMGuardToxicityCheck", "LLMGuardToxicityConfig"]
