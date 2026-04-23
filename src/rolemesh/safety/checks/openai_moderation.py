"""OpenAI Moderation API adapter.

Async check — calls the OpenAI moderations endpoint which returns a
closed set of category flags. Unlike the ML checks this one does NOT
run inference locally; it's an HTTP call to OpenAI's hosted
classifier. ``_sync = False`` so the orchestrator dispatches
``check()`` directly on the event loop (no thread pool needed — the
async ``httpx`` call yields fine).

Adapter discipline: OpenAI's category taxonomy is closed
(documented at platform.openai.com/docs/guides/moderation). If they
add a new category in a future model version, it becomes invisible
here until we map it — a Finding.code drift is worse than missed
detection for a novel category.

Cost / rate-limit note: OpenAI's moderation endpoint is free but
rate-limited. A busy tenant can trip limits; a 429 is treated as a
transport error → fail-open with a critical finding (same posture
as RemoteCheck in agent_runner/safety/remote.py).
"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ..types import CostClass, Finding, Stage, Verdict

if TYPE_CHECKING:
    from collections.abc import Mapping


class ModerationCode(StrEnum):
    HARASSMENT = "MODERATION.HARASSMENT"
    HATE = "MODERATION.HATE"
    VIOLENCE = "MODERATION.VIOLENCE"
    SEXUAL = "MODERATION.SEXUAL"
    SELF_HARM = "MODERATION.SELF_HARM"
    ILLICIT = "MODERATION.ILLICIT"


_OPENAI_CATEGORY_MAPPING: dict[str, ModerationCode] = {
    # Stable categories — unchanged across moderation models the last
    # two years. OpenAI uses ``/`` in some names (e.g. ``harassment/threatening``)
    # which are specialisations we fold under the top-level code.
    "harassment": ModerationCode.HARASSMENT,
    "harassment/threatening": ModerationCode.HARASSMENT,
    "hate": ModerationCode.HATE,
    "hate/threatening": ModerationCode.HATE,
    "violence": ModerationCode.VIOLENCE,
    "violence/graphic": ModerationCode.VIOLENCE,
    "sexual": ModerationCode.SEXUAL,
    "sexual/minors": ModerationCode.SEXUAL,
    "self-harm": ModerationCode.SELF_HARM,
    "self-harm/intent": ModerationCode.SELF_HARM,
    "self-harm/instructions": ModerationCode.SELF_HARM,
    "illicit": ModerationCode.ILLICIT,
    "illicit/violent": ModerationCode.ILLICIT,
}


class OpenAIModerationConfig(BaseModel):
    """Rule config schema.

    ``block_categories`` and ``warn_categories`` use the stable
    MODERATION.* codes. Empty block_categories effectively disables
    the block path (still emits findings via warn if configured).

    ``api_key_env`` names the env var holding the OpenAI API key so
    operators can rotate keys without touching the rule. Default is
    ``OPENAI_API_KEY`` per OpenAI convention; an empty env var at
    runtime is treated as a transport error (fail-open + critical
    finding).

    ``timeout_ms`` caps the HTTP call. 2000ms default matches the
    stage budget for slow checks at INPUT_PROMPT / MODEL_OUTPUT.
    """

    model_config = ConfigDict(extra="forbid")

    api_key_env: str = "OPENAI_API_KEY"
    block_categories: list[str] = Field(default_factory=list)
    warn_categories: list[str] = Field(default_factory=list)
    timeout_ms: int = 2000
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


_OPENAI_MODERATIONS_URL = "https://api.openai.com/v1/moderations"


class OpenAIModerationCheck:
    id: str = "openai_moderation"
    version: str = "1"
    stages: frozenset[Stage] = frozenset(
        {Stage.INPUT_PROMPT, Stage.MODEL_OUTPUT}
    )
    cost_class: CostClass = "slow"
    supported_codes: frozenset[str] = frozenset(
        c.value for c in ModerationCode
    )
    config_model: type[BaseModel] = OpenAIModerationConfig
    # Async call (httpx.AsyncClient) — stays on the main event loop.
    _sync: bool = False

    async def check(self, ctx: Any, config: dict[str, Any]) -> Verdict:
        text = _extract_text(ctx.stage, ctx.payload)
        if not text.strip():
            return Verdict(action="allow")
        api_key_env = str(config.get("api_key_env") or "OPENAI_API_KEY")
        api_key = os.environ.get(api_key_env, "")
        if not api_key:
            return _fail_open(
                "MODERATION_CONFIG_ERROR",
                f"{api_key_env} not set — moderation check skipped",
            )
        timeout_s = float(int(config.get("timeout_ms") or 2000)) / 1000.0
        block_codes = {
            str(c) for c in (config.get("block_categories") or [])
        }
        warn_codes = {
            str(c) for c in (config.get("warn_categories") or [])
        }

        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                response = await client.post(
                    _OPENAI_MODERATIONS_URL,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"input": text},
                )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return _fail_open(
                "MODERATION_TRANSPORT_ERROR",
                f"OpenAI moderation call failed: {exc}",
            )
        if response.status_code >= 400:
            return _fail_open(
                "MODERATION_HTTP_ERROR",
                f"OpenAI moderation returned {response.status_code}",
            )
        try:
            data = response.json()
        except Exception as exc:  # noqa: BLE001 — ill-formed JSON is fail-open
            return _fail_open(
                "MODERATION_PARSE_ERROR",
                f"OpenAI moderation returned non-JSON: {exc}",
            )

        results = data.get("results") or []
        if not results:
            return Verdict(action="allow")
        result = results[0]
        categories = result.get("categories") or {}
        scores = result.get("category_scores") or {}

        hits: dict[ModerationCode, float] = {}
        for raw_category, flagged in categories.items():
            if not flagged:
                continue
            code = _OPENAI_CATEGORY_MAPPING.get(raw_category)
            if code is None:
                continue
            # Keep the highest score across subcategories that fold
            # into the same top-level code (e.g. ``harassment`` vs
            # ``harassment/threatening`` → both map to HARASSMENT).
            score = float(scores.get(raw_category) or 0.0)
            hits[code] = max(hits.get(code, 0.0), score)

        if not hits:
            return Verdict(action="allow")

        # Block wins over warn.
        block_hits = [
            (c, s) for c, s in hits.items() if c.value in block_codes
        ]
        if block_hits:
            return Verdict(
                action="block",
                reason=(
                    "Blocked: moderation flagged "
                    + ", ".join(sorted({c.value for c, _ in block_hits}))
                ),
                findings=[
                    Finding(
                        code=c.value,
                        severity="high",
                        message=f"{c.value} score={s:.3f}",
                        metadata={"score": s},
                    )
                    for c, s in block_hits
                ],
            )
        warn_hits = [
            (c, s) for c, s in hits.items() if c.value in warn_codes
        ]
        if warn_hits:
            return Verdict(
                action="warn",
                appended_context=(
                    "[Content advisory — moderation flagged: "
                    + ", ".join(sorted({c.value for c, _ in warn_hits}))
                    + "]"
                ),
                findings=[
                    Finding(
                        code=c.value,
                        severity="medium",
                        message=f"{c.value} score={s:.3f}",
                        metadata={"score": s},
                    )
                    for c, s in warn_hits
                ],
            )
        # Detected but not configured to act — still record as audit
        # so operators see what the classifier saw.
        return Verdict(
            action="allow",
            findings=[
                Finding(
                    code=c.value,
                    severity="info",
                    message=f"{c.value} score={s:.3f} (unconfigured)",
                    metadata={"score": s},
                )
                for c, s in hits.items()
            ],
        )


def _fail_open(code: str, message: str) -> Verdict:
    return Verdict(
        action="allow",
        findings=[
            Finding(code=code, severity="critical", message=message)
        ],
    )


__all__ = [
    "ModerationCode",
    "OpenAIModerationCheck",
    "OpenAIModerationConfig",
]
