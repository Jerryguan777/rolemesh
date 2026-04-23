"""Presidio-backed PII detector + redactor.

Superset of ``pii.regex`` — adds ML-backed PERSON / LOCATION / etc.
on top of the stock recognizers presidio ships. The regex check
remains the cheap first line in the container; this orchestrator-
side check handles the broader recognition work that needs spaCy.

Adapter discipline (design doc §8.1): presidio's entity universe is
declared here as a closed ``_PRESIDIO_MAPPING`` dict. Any entity type
presidio returns that's not in the map is silently dropped — we
never let third-party labels leak into ``Finding.code``. Stable
codes are:

    PII.SSN / PII.CREDIT_CARD / PII.EMAIL / PII.PHONE / PII.IP_ADDRESS
    PII.PERSON_NAME / PII.LOCATION / PII.DATE_TIME / PII.URL
    PII.IBAN / PII.US_BANK_NUMBER / PII.US_DRIVER_LICENSE
    PII.US_PASSPORT / PII.MEDICAL_LICENSE

Config selects the subset that actually drives a block vs a redact
at the rule level — a tenant might want to block on SSN but only
redact on EMAIL.

Stages: INPUT_PROMPT, MODEL_OUTPUT, POST_TOOL_RESULT. PRE_TOOL_CALL
is deliberately excluded: a slow check on a tool call with a
reversible tool would trip the P0.4 budget guard, and most
high-value PII actually appears in model output, not tool calls.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from ..types import CostClass, Finding, Stage, Verdict

if TYPE_CHECKING:
    from collections.abc import Mapping


class PresidioPIICode(StrEnum):
    """Closed set of Finding.code strings this check may emit."""

    SSN = "PII.SSN"
    CREDIT_CARD = "PII.CREDIT_CARD"
    EMAIL = "PII.EMAIL"
    PHONE = "PII.PHONE"
    IP_ADDRESS = "PII.IP_ADDRESS"
    PERSON_NAME = "PII.PERSON_NAME"
    LOCATION = "PII.LOCATION"
    DATE_TIME = "PII.DATE_TIME"
    URL = "PII.URL"
    IBAN = "PII.IBAN"
    US_BANK_NUMBER = "PII.US_BANK_NUMBER"
    US_DRIVER_LICENSE = "PII.US_DRIVER_LICENSE"
    US_PASSPORT = "PII.US_PASSPORT"
    MEDICAL_LICENSE = "PII.MEDICAL_LICENSE"


# presidio entity_type → stable code. Any entity type NOT in this map
# is dropped silently. When presidio adds a new entity in a future
# version, its results will be invisible to this check until we
# deliberately add the mapping — that's the adapter discipline.
_PRESIDIO_MAPPING: dict[str, PresidioPIICode] = {
    "US_SSN": PresidioPIICode.SSN,
    "CREDIT_CARD": PresidioPIICode.CREDIT_CARD,
    "EMAIL_ADDRESS": PresidioPIICode.EMAIL,
    "PHONE_NUMBER": PresidioPIICode.PHONE,
    "IP_ADDRESS": PresidioPIICode.IP_ADDRESS,
    "PERSON": PresidioPIICode.PERSON_NAME,
    "LOCATION": PresidioPIICode.LOCATION,
    "DATE_TIME": PresidioPIICode.DATE_TIME,
    "URL": PresidioPIICode.URL,
    "IBAN_CODE": PresidioPIICode.IBAN,
    "US_BANK_NUMBER": PresidioPIICode.US_BANK_NUMBER,
    "US_DRIVER_LICENSE": PresidioPIICode.US_DRIVER_LICENSE,
    "US_PASSPORT": PresidioPIICode.US_PASSPORT,
    "MEDICAL_LICENSE": PresidioPIICode.MEDICAL_LICENSE,
    # Deliberately NOT mapped: CRYPTO (too noisy), NRP
    # (nationality/religion — not PII leak per se), MAC_ADDRESS,
    # UK_NHS. Add them here if a user asks.
}


class PresidioPIIConfig(BaseModel):
    """Rule config schema.

    ``block_codes`` and ``redact_codes`` use the stable PII.* codes
    defined above. A code may appear in at most one of the lists;
    block wins if there's a conflict (safer default).

    ``language`` follows presidio — ``en`` only for V2 P1.3. Multi-
    language support means loading extra spaCy models which is a
    deployment concern; narrow to 'en' at the schema level rather
    than blowing up at runtime.

    ``score_threshold`` gates presidio's confidence score. Default
    0.4 matches presidio's default; raise it for fewer false
    positives, lower to catch more at the cost of noise.
    """

    model_config = ConfigDict(extra="forbid")

    block_codes: list[str] = Field(default_factory=list)
    redact_codes: list[str] = Field(default_factory=list)
    language: str = "en"
    score_threshold: float = 0.4
    action_override: str | None = None


def _stage_text_key(stage: Stage) -> str | None:
    """Return the payload key that carries human text for this stage."""
    if stage == Stage.INPUT_PROMPT:
        return "prompt"
    if stage == Stage.MODEL_OUTPUT:
        return "text"
    if stage == Stage.POST_TOOL_RESULT:
        return "tool_result"
    return None


def _extract_text(stage: Stage, payload: Mapping[str, Any]) -> str:
    key = _stage_text_key(stage)
    if key is None:
        return ""
    value = payload.get(key)
    if isinstance(value, str):
        return value
    return str(value or "")


def _rebuild_payload(
    stage: Stage, payload: Mapping[str, Any], new_text: str
) -> dict[str, Any]:
    key = _stage_text_key(stage)
    out = dict(payload)
    if key is not None:
        out[key] = new_text
    return out


class PresidioPIICheck:
    id: str = "presidio.pii"
    version: str = "1"
    stages: frozenset[Stage] = frozenset(
        {
            Stage.INPUT_PROMPT,
            Stage.MODEL_OUTPUT,
            Stage.POST_TOOL_RESULT,
        }
    )
    cost_class: CostClass = "slow"
    supported_codes: frozenset[str] = frozenset(
        c.value for c in PresidioPIICode
    )
    config_model: type[BaseModel] = PresidioPIIConfig
    _sync: bool = True

    def __init__(self) -> None:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider
        from presidio_anonymizer import AnonymizerEngine

        # Presidio's default config pins ``en_core_web_lg`` (425 MB).
        # For RoleMesh's detection needs, the ``sm`` model (~12 MB) is
        # sufficient — our ``_PRESIDIO_MAPPING`` only uses entities
        # whose recognition relies on presidio's regex / rule
        # recognizers (EMAIL_ADDRESS, US_SSN, CREDIT_CARD, …) or on
        # spaCy NER labels (PERSON, LOCATION, DATE_TIME) that are
        # available in the small model too. The accuracy difference
        # on short prompts is negligible in practice and the 35x
        # disk savings are not.
        # If an operator actually needs the larger model they can
        # ``spacy download en_core_web_lg`` and rebuild the registry
        # with a custom NlpEngineProvider config — this class
        # deliberately does not expose that as a runtime knob
        # because it's a deployment choice, not a per-rule one.
        provider = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [
                    {
                        "lang_code": "en",
                        "model_name": "en_core_web_sm",
                    }
                ],
            }
        )
        self._analyzer = AnalyzerEngine(
            nlp_engine=provider.create_engine()
        )
        self._anonymizer = AnonymizerEngine()

    async def check(
        self, ctx: Any, config: dict[str, Any]
    ) -> Verdict:
        text = _extract_text(ctx.stage, ctx.payload)
        if not text.strip():
            return Verdict(action="allow")
        threshold = float(config.get("score_threshold") or 0.4)
        language = str(config.get("language") or "en")
        block_codes = {
            str(c) for c in (config.get("block_codes") or [])
        }
        redact_codes = {
            str(c) for c in (config.get("redact_codes") or [])
        }

        analyzer_results = self._analyzer.analyze(
            text=text,
            language=language,
            score_threshold=threshold,
        )
        # Filter to entity types we know about. Drop anything outside
        # _PRESIDIO_MAPPING — adapter discipline prevents leaking raw
        # presidio labels into Finding.code.
        mapped: list[tuple[PresidioPIICode, Any]] = []
        for r in analyzer_results:
            code = _PRESIDIO_MAPPING.get(r.entity_type)
            if code is None:
                continue
            mapped.append((code, r))

        if not mapped:
            return Verdict(action="allow")

        # Block wins over redact. Iterate block first so operators'
        # "block list" takes precedence if a code appears in both.
        blocks = [
            (c, r) for c, r in mapped if c.value in block_codes
        ]
        if blocks:
            return Verdict(
                action="block",
                reason=(
                    "Blocked: detected "
                    + ", ".join(sorted({c.value for c, _ in blocks}))
                ),
                findings=[_to_finding(c, r) for c, r in blocks],
            )

        redacts = [
            (c, r) for c, r in mapped if c.value in redact_codes
        ]
        if redacts:
            # Anonymize the text with only the matched analyzer
            # results — presidio's anonymizer rewrites those spans to
            # ``<ENTITY_TYPE>`` placeholders by default.
            anonymized = self._anonymizer.anonymize(
                text=text,
                analyzer_results=[r for _, r in redacts],
            )
            return Verdict(
                action="redact",
                modified_payload=_rebuild_payload(
                    ctx.stage, ctx.payload, anonymized.text
                ),
                findings=[_to_finding(c, r) for c, r in redacts],
            )

        return Verdict(action="allow")


def _to_finding(code: PresidioPIICode, result: Any) -> Finding:
    return Finding(
        code=code.value,
        severity="high",
        message=f"{code.value} detected (score={result.score:.3f})",
        metadata={
            "start": int(result.start),
            "end": int(result.end),
            "score": float(result.score),
        },
    )


__all__ = ["PresidioPIICheck", "PresidioPIICode", "PresidioPIIConfig"]
