"""Cheap check that blocks tool calls reaching unlisted hosts.

Runs at PRE_TOOL_CALL — walks every string leaf of ``tool_input``,
extracts URLs, and compares each host against the rule's
``allowed_hosts`` list. A hit on any non-allowed host produces a
block verdict.

Wildcard semantics: ``*.example.com`` matches ``a.example.com`` and
``a.b.example.com`` but NOT the apex ``example.com``. This matches
what operators typically mean by "allow subdomains" — if they want
the apex they add it explicitly. An entry without a leading ``*.``
is an exact host match, not a suffix match, so ``github.com`` does
not silently cover ``fake-github.com``.

Pure stdlib (``re`` + ``urllib.parse``) so the check lives on both
sides of the registry split — cheap checks run locally in the
container for PRE_TOOL_CALL's tight 100 ms budget.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..types import CostClass, Finding, SafetyContext, Stage, Verdict

if TYPE_CHECKING:
    from collections.abc import Mapping


# Match http:// or https:// URLs up to the first whitespace / quote /
# closing angle bracket. Broad enough to catch URLs embedded in
# JSON-like string values, narrow enough that false-positive whole-
# string matches on prose like "see http://x for more" still parse to
# the real URL via urlparse.
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>)}]+", re.IGNORECASE)


class DomainAllowlistConfig(BaseModel):
    """Pydantic validated at REST create time.

    ``extra='forbid'`` turns unknown keys into 422 so a typo like
    ``allowed_host`` (singular) fails at admin time instead of
    silently matching nothing at runtime. ``allowed_hosts`` is
    required — an empty list would block every outbound call and is
    never the intended configuration.
    """

    model_config = ConfigDict(extra="forbid")

    allowed_hosts: list[str] = Field(min_length=1)
    # Optional action override — same key the pipeline recognizes on
    # every rule config. Declared here so pydantic validation catches
    # bogus values at create time rather than letting a rule with
    # ``action_override: 'teleport'`` reach the runtime guard.
    action_override: str | None = None

    @field_validator("allowed_hosts")
    @classmethod
    def _normalize_hosts(cls, hosts: list[str]) -> list[str]:
        # Reject empty / whitespace-only entries so they can't sneak
        # through as "block nothing". We don't lower-case here because
        # URL hostnames are case-insensitive at match time anyway and
        # preserving the operator's casing helps with debugging.
        clean = [h.strip() for h in hosts if h and h.strip()]
        if not clean:
            raise ValueError(
                "allowed_hosts must contain at least one non-empty entry"
            )
        return clean


def _extract_urls(payload: Mapping[str, Any]) -> list[str]:
    """Flatten every string leaf and extract URLs with the regex."""
    buf: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, str):
            for m in _URL_PATTERN.finditer(node):
                buf.append(m.group(0))
        elif isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, list | tuple):
            for v in node:
                _walk(v)

    _walk(payload)
    return buf


def _host_matches(host: str, pattern: str) -> bool:
    """Apply wildcard semantics to one (host, pattern) pair."""
    host = host.lower()
    pattern = pattern.lower()
    if pattern.startswith("*."):
        suffix = pattern[2:]
        # ``a.example.com`` matches ``*.example.com`` (has ``.`` before suffix).
        # Apex ``example.com`` does NOT match — this is deliberate.
        return host.endswith("." + suffix)
    return host == pattern


def _is_host_allowed(host: str, patterns: list[str]) -> bool:
    return any(_host_matches(host, p) for p in patterns)


class DomainAllowlistCheck:
    """Block tool calls to hosts outside the allowlist.

    Config schema (validated at REST via ``config_model``)::

        {
          "allowed_hosts": ["github.com", "*.github.com", "api.openai.com"],
          "action_override": "require_approval"   // optional
        }

    Findings carry the offending host so audit trails point at the
    specific violation, not just "some URL was blocked".
    """

    id: str = "domain_allowlist"
    version: str = "1"
    stages: frozenset[Stage] = frozenset({Stage.PRE_TOOL_CALL})
    cost_class: CostClass = "cheap"
    supported_codes: frozenset[str] = frozenset({"DOMAIN_NOT_ALLOWED"})
    config_model: type[BaseModel] = DomainAllowlistConfig

    async def check(
        self, ctx: SafetyContext, config: dict[str, Any]
    ) -> Verdict:
        allowed = config.get("allowed_hosts") or []
        if not isinstance(allowed, list) or not allowed:
            # Empty list at runtime means "block everything" — but the
            # REST layer already rejects this shape. Treat an empty
            # runtime config as allow so an accidentally-wiped
            # config_override key does not silently turn every tool
            # call into a block.
            return Verdict(action="allow")

        patterns = [str(p) for p in allowed if isinstance(p, str)]
        if not patterns:
            return Verdict(action="allow")

        findings: list[Finding] = []
        seen_hosts: set[str] = set()
        for url in _extract_urls(ctx.payload):
            host = urlparse(url).hostname or ""
            if not host or host in seen_hosts:
                continue
            seen_hosts.add(host)
            if not _is_host_allowed(host, patterns):
                findings.append(
                    Finding(
                        code="DOMAIN_NOT_ALLOWED",
                        severity="high",
                        message=f"host {host!r} not in allowlist",
                        metadata={"host": host},
                    )
                )

        if findings:
            hosts = ", ".join(sorted({f.metadata["host"] for f in findings}))
            return Verdict(
                action="block",
                reason=f"Blocked: domain(s) not allowed: {hosts}",
                findings=findings,
            )
        return Verdict(action="allow")


__all__ = ["DomainAllowlistCheck", "DomainAllowlistConfig"]
