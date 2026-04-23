"""LLM-Guard BanSubstrings-backed jailbreak detector.

Distinct from ``llm_guard.prompt_injection`` even though it uses
the same library: prompt_injection fires the transformer classifier,
which catches novel/paraphrased injections. This check runs a fast
exact/fuzzy-substring scan over an operator-curated list of known
jailbreak phrases ("Ignore all previous instructions", "You are
DAN", etc.). Its value is low-latency, high-precision matching
layered on top of the slower classifier.

Why a separate check: operators tune the phrase list per tenant. A
healthcare tenant may ban "pretend to be a doctor" phrases that are
fine for a coding agent. The classifier is one-size-fits-all;
BanSubstrings is per-config.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..types import CostClass, Finding, Stage, Verdict

_DEFAULT_JAILBREAK_PHRASES: tuple[str, ...] = (
    "ignore all previous instructions",
    "ignore previous instructions",
    "disregard the above",
    "you are now",
    "pretend you are",
    "act as dan",
    "DAN mode",
    "jailbreak mode",
    "developer mode",
)


class LLMGuardJailbreakConfig(BaseModel):
    """Rule config schema.

    ``phrases`` defaults to a conservative set — operators add their
    own tenant-specific entries. ``case_sensitive`` defaults to
    False because attack strings tend to vary casing.
    """

    model_config = ConfigDict(extra="forbid")

    phrases: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_JAILBREAK_PHRASES)
    )
    case_sensitive: bool = False
    action_override: str | None = None


class LLMGuardJailbreakCheck:
    id: str = "llm_guard.jailbreak"
    version: str = "1"
    stages: frozenset[Stage] = frozenset({Stage.INPUT_PROMPT})
    cost_class: CostClass = "slow"
    supported_codes: frozenset[str] = frozenset({"JAILBREAK"})
    config_model: type[BaseModel] = LLMGuardJailbreakConfig
    # Substring matching is cheap, but llm-guard scanners are
    # typically thread-pool dispatched to keep the loop clean.
    _sync: bool = True

    def __init__(self) -> None:
        # The BanSubstrings scanner is cheap to construct per call
        # (no model download) — but we still import lazily so the
        # orchestrator registry can gate on ImportError.
        from llm_guard.input_scanners import BanSubstrings  # noqa: F401

    async def check(
        self, ctx: Any, config: dict[str, Any]
    ) -> Verdict:
        # Late import keeps the symbol available without leaking
        # llm-guard into module load. Re-import here so the thread
        # pool worker picks up the same code path as __init__.
        from llm_guard.input_scanners import BanSubstrings
        from llm_guard.input_scanners.ban_substrings import MatchType

        prompt = str(ctx.payload.get("prompt", ""))
        if not prompt.strip():
            return Verdict(action="allow")

        phrases_raw = config.get("phrases")
        phrases = (
            [str(p) for p in phrases_raw if isinstance(p, str) and p]
            if isinstance(phrases_raw, list)
            else list(_DEFAULT_JAILBREAK_PHRASES)
        )
        if not phrases:
            return Verdict(action="allow")
        case_sensitive = bool(config.get("case_sensitive", False))

        scanner = BanSubstrings(
            substrings=phrases,
            match_type=MatchType.STR,
            case_sensitive=case_sensitive,
        )
        _sanitized, is_valid, risk_score = scanner.scan(prompt)
        if is_valid:
            return Verdict(action="allow")
        return Verdict(
            action="block",
            reason="Blocked: matched jailbreak phrase list",
            findings=[
                Finding(
                    code="JAILBREAK",
                    severity="high",
                    message=(
                        f"jailbreak phrase detected "
                        f"(score={float(risk_score):.3f})"
                    ),
                    metadata={"score": float(risk_score)},
                )
            ],
        )


__all__ = ["LLMGuardJailbreakCheck", "LLMGuardJailbreakConfig"]
