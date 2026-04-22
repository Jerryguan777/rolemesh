"""Regex-based PII detector — the only V1 check.

Pure stdlib (``re``) so it runs identically inside the container (no
network, no model, no heavy dependency). V2's ``presidio.pii`` is an
orchestrator-only superset with ML-backed entity detection; ``pii.regex``
remains the cheap first line of defense that never leaves the container.

Stable codes (``supported_codes``) are the adapter discipline required
by §7.1 of the design doc — downstream audit queries and policy
dashboards key off these strings, so renames require a ``version`` bump.

Config key discipline: ``patterns`` keys use the short form (``SSN``,
``EMAIL``) because short names are the human-facing identifier in the
admin UI. The check emits Finding.code in the prefixed form
(``PII.SSN``) because audit tables key off prefixed, namespaced codes.
``_CONFIG_KEY_TO_CODE`` is the single explicit mapping between the two;
we do not build keys with string concatenation (the previous code did,
which silently accepted whitespace-padded keys).
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from ..types import CostClass, Finding, SafetyContext, Stage, Verdict

if TYPE_CHECKING:
    from collections.abc import Mapping


class PIICode(StrEnum):
    """Stable Finding.code values emitted by PIIRegexCheck."""

    SSN = "PII.SSN"
    CREDIT_CARD = "PII.CREDIT_CARD"
    EMAIL = "PII.EMAIL"
    PHONE_US = "PII.PHONE_US"
    IP_ADDRESS = "PII.IP_ADDRESS"


# Explicit short-key → stable code mapping. Admin UI / REST API accept
# the short form on the left; Finding.code uses the prefixed form on
# the right. Any key not in this map is a typo and SHOULD be rejected
# at the REST layer via PIIRegexConfig.
_CONFIG_KEY_TO_CODE: dict[str, PIICode] = {
    "SSN": PIICode.SSN,
    "CREDIT_CARD": PIICode.CREDIT_CARD,
    "EMAIL": PIICode.EMAIL,
    "PHONE_US": PIICode.PHONE_US,
    "IP_ADDRESS": PIICode.IP_ADDRESS,
}


class PIIRegexConfig(BaseModel):
    """Pydantic model validated by the REST layer.

    ``model_config = ConfigDict(extra="forbid")`` turns unknown keys
    into 422 validation errors, so a typo like ``{"patterns": {"SNN":
    true}}`` is rejected at admin-time instead of accepted-and-
    silently-ignored. ``patterns`` values are coerced to bool — so
    ``"yes"`` stays a string that fails bool validation, rather than
    sneaking through as truthy.
    """

    model_config = ConfigDict(extra="forbid")

    # StrictBool rejects truthy strings like "yes" / "on" / "1" that
    # pydantic's default bool coercion silently converts to True. The
    # admin intent of {"patterns": {"SSN": "yes"}} is ambiguous — we
    # want them to write `true` / `false` explicitly.
    patterns: dict[str, StrictBool] = Field(default_factory=dict)

    def model_post_init(self, _ctx: Any) -> None:
        # Validate each key against the stable mapping at admin time
        # so the operator's feedback loop is tight. Unknown keys ->
        # 422 through pydantic's standard error surface.
        for key in self.patterns:
            if key not in _CONFIG_KEY_TO_CODE:
                valid = sorted(_CONFIG_KEY_TO_CODE.keys())
                raise ValueError(
                    f"Unknown PII pattern {key!r}; valid keys: {valid}"
                )


# Conservative regex set. CREDIT_CARD uses a loose 13-19-digit window
# with optional separators — false positives are acceptable for a block
# action because the user can always disable the pattern or switch to
# redact (V2). Real Luhn validation belongs in V2's presidio.pii, not
# here.
_PATTERNS: dict[PIICode, re.Pattern[str]] = {
    PIICode.SSN: re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    PIICode.CREDIT_CARD: re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    PIICode.EMAIL: re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    PIICode.PHONE_US: re.compile(
        r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"
    ),
    PIICode.IP_ADDRESS: re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
}


def _extract_scannable_text(payload: Mapping[str, Any]) -> str:
    """Flatten every string leaf of ``payload`` into a single scan buffer.

    We join with newlines so patterns anchored with ``\\b`` / ``^`` /
    ``$`` still match at field boundaries. Non-string leaves (bools,
    numbers) are coerced to str so a numeric SSN packed into a dict
    field doesn't escape detection. Unlimited recursion is fine for
    tool_input shapes in practice; the pipeline's stage budget caps
    wall time.
    """
    buf: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, str):
            buf.append(node)
        elif isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, list | tuple):
            for v in node:
                _walk(v)
        elif isinstance(node, int | float | bool):
            buf.append(str(node))

    _walk(payload)
    return "\n".join(buf)


class PIIRegexCheck:
    """Regex PII detector.

    Config schema (validated at REST via ``config_model``)::

        {
          "patterns": {
            "SSN": true,
            "CREDIT_CARD": true,
            "EMAIL": false,
            "PHONE_US": false,
            "IP_ADDRESS": false
          }
        }

    Empty / missing ``patterns`` = no-op (return allow). V1 always emits
    ``block`` on any hit; V2 will add an ``action_override`` config key.

    Run-time parsing is permissive (unknown keys skipped with a log)
    rather than hard-failing, because snapshots loaded into a running
    container predate any later REST-layer schema change. REST is the
    strict boundary.
    """

    id: str = "pii.regex"
    version: str = "1"
    stages: frozenset[Stage] = frozenset(
        {
            Stage.PRE_TOOL_CALL,
            Stage.INPUT_PROMPT,
            Stage.MODEL_OUTPUT,
            Stage.POST_TOOL_RESULT,
        }
    )
    cost_class: CostClass = "cheap"
    supported_codes: frozenset[str] = frozenset(c.value for c in PIICode)
    config_model: type[BaseModel] = PIIRegexConfig

    async def check(
        self, ctx: SafetyContext, config: dict[str, Any]
    ) -> Verdict:
        raw_patterns = config.get("patterns") or {}
        if not isinstance(raw_patterns, dict):
            return Verdict(action="allow")

        enabled: set[PIICode] = set()
        for key, flag in raw_patterns.items():
            if not flag:
                continue
            code = _CONFIG_KEY_TO_CODE.get(str(key))
            if code is None:
                # Unknown key in a persisted snapshot — skip silently
                # at container run-time (REST already blocked this on
                # fresh creates). A log line helps operators notice
                # stale snapshots without failing the agent turn.
                continue
            enabled.add(code)

        if not enabled:
            return Verdict(action="allow")

        text = _extract_scannable_text(ctx.payload)
        findings: list[Finding] = []
        for code, pat in _PATTERNS.items():
            if code not in enabled:
                continue
            if pat.search(text):
                findings.append(
                    Finding(
                        code=code.value,
                        severity="high",
                        message=f"{code.value} detected",
                    )
                )

        if findings:
            joined = ", ".join(f.code for f in findings)
            return Verdict(
                action="block",
                reason=f"Blocked: detected {joined}",
                findings=findings,
            )
        return Verdict(action="allow")


__all__ = ["PIICode", "PIIRegexCheck", "PIIRegexConfig"]
