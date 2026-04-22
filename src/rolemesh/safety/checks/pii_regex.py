"""Regex-based PII detector — the only V1 check.

Pure stdlib (``re``) so it runs identically inside the container (no
network, no model, no heavy dependency). V2's ``presidio.pii`` is an
orchestrator-only superset with ML-backed entity detection; ``pii.regex``
remains the cheap first line of defense that never leaves the container.

Stable codes (``supported_codes``) are the adapter discipline required
by §7.1 of the design doc — downstream audit queries and policy
dashboards key off these strings, so renames require a ``version`` bump.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import TYPE_CHECKING, Any

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

    We join with newlines so patterns anchored with ``\b`` / ``^`` /
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

    Config schema::

        {
          "patterns": {
            "SSN": true,
            "CREDIT_CARD": true,
            "EMAIL": false,
            "PHONE_US": false,
            "IP_ADDRESS": false
          }
        }

    Missing / empty ``patterns`` keys are treated as disabled; a
    completely empty ``patterns`` dict means the check is a no-op and
    returns allow. V1 always emits ``block`` on any hit; V2 will add
    an ``action_override`` config key.
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
            try:
                enabled.add(PIICode(f"PII.{key}") if not str(key).startswith("PII.") else PIICode(key))
            except ValueError:
                # Unknown pattern name — silently ignored rather than
                # failing the rule, so a newer check.version can add
                # patterns without breaking older configs.
                continue

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


__all__ = ["PIICode", "PIIRegexCheck"]
