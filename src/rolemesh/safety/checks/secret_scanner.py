"""detect-secrets-backed scanner for credentials in text.

Runs on MODEL_OUTPUT and POST_TOOL_RESULT — the places where tokens
accidentally get echoed back to the user or reflected into the agent
context. Blocks by default; a rule can downgrade to
``action_override='warn'`` when operators want detection without
interruption.

Adapter discipline: detect-secrets names ~25 distinct plugin types
today. The stable Finding.code set below is a closed subset of
meaningful categories. Anything detect-secrets flags outside
``_DETECT_SECRETS_MAPPING`` is dropped — we don't let third-party
labels leak into Finding.code.

The scanner runs in a thread pool (``_sync = True``) because the
file-based scanner internals are sync + CPU-bound. We use a tempfile
rather than ``scan_line`` because the latter exercises a subset of
plugins — the full ``SecretsCollection.scan_file`` is what catches
high-entropy strings too.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from ..types import CostClass, Finding, Stage, Verdict

if TYPE_CHECKING:
    from collections.abc import Mapping


class SecretCode(StrEnum):
    AWS_KEY = "SECRET.AWS_KEY"
    GITHUB_TOKEN = "SECRET.GITHUB_TOKEN"
    GITLAB_TOKEN = "SECRET.GITLAB_TOKEN"
    PRIVATE_KEY = "SECRET.PRIVATE_KEY"
    SLACK_TOKEN = "SECRET.SLACK_TOKEN"
    JWT = "SECRET.JWT"
    OPENAI_KEY = "SECRET.OPENAI_KEY"
    STRIPE_KEY = "SECRET.STRIPE_KEY"
    AZURE_STORAGE = "SECRET.AZURE_STORAGE"
    TELEGRAM_BOT_TOKEN = "SECRET.TELEGRAM_BOT_TOKEN"
    DISCORD_BOT_TOKEN = "SECRET.DISCORD_BOT_TOKEN"
    BASIC_AUTH = "SECRET.BASIC_AUTH"
    GENERIC_HIGH_ENTROPY = "SECRET.GENERIC_HIGH_ENTROPY"
    GENERIC_SECRET_KEYWORD = "SECRET.GENERIC_SECRET_KEYWORD"
    NPM_TOKEN = "SECRET.NPM_TOKEN"


_DETECT_SECRETS_MAPPING: dict[str, SecretCode] = {
    "AWS Access Key": SecretCode.AWS_KEY,
    "GitHub Token": SecretCode.GITHUB_TOKEN,
    "GitLab Token": SecretCode.GITLAB_TOKEN,
    "Private Key": SecretCode.PRIVATE_KEY,
    "Slack Token": SecretCode.SLACK_TOKEN,
    "JSON Web Token": SecretCode.JWT,
    "OpenAI Token": SecretCode.OPENAI_KEY,
    "Stripe Access Key": SecretCode.STRIPE_KEY,
    "Azure Storage Account access key": SecretCode.AZURE_STORAGE,
    "Telegram Bot Token": SecretCode.TELEGRAM_BOT_TOKEN,
    "Discord Bot Token": SecretCode.DISCORD_BOT_TOKEN,
    "Basic Auth Credentials": SecretCode.BASIC_AUTH,
    "Base64 High Entropy String": SecretCode.GENERIC_HIGH_ENTROPY,
    "Hex High Entropy String": SecretCode.GENERIC_HIGH_ENTROPY,
    "Secret Keyword": SecretCode.GENERIC_SECRET_KEYWORD,
    "NPM tokens": SecretCode.NPM_TOKEN,
    # Deliberately NOT mapped:
    #   Public IP (ipv4) — not a credential, covered by presidio
    #   Artifactory / Cloudant / SoftLayer / Square / SendGrid /
    #     Mailchimp / Twilio / IBM Cloud / PyPI — low-frequency in
    #     our threat model, add as real users surface them.
}


class SecretScannerConfig(BaseModel):
    """Rule config schema.

    ``action_override`` lets a tenant downgrade a global block to
    warn (monitoring-only mode during a rollout). Unlike presidio.pii
    we don't expose redact here: replacing a secret mid-output risks
    leaving partial text that still identifies the credential, so
    block is the correct default. If an operator really wants redact
    they can stack a second rule.
    """

    model_config = ConfigDict(extra="forbid")

    action_override: str | None = None


def _stage_text_key(stage: Stage) -> str | None:
    if stage == Stage.MODEL_OUTPUT:
        return "text"
    if stage == Stage.POST_TOOL_RESULT:
        return "tool_result"
    if stage == Stage.INPUT_PROMPT:
        return "prompt"
    return None


def _extract_text(stage: Stage, payload: Mapping[str, Any]) -> str:
    key = _stage_text_key(stage)
    if key is None:
        return ""
    value = payload.get(key)
    if isinstance(value, str):
        return value
    return str(value or "")


class SecretScannerCheck:
    id: str = "secret_scanner"
    version: str = "1"
    stages: frozenset[Stage] = frozenset(
        {Stage.MODEL_OUTPUT, Stage.POST_TOOL_RESULT, Stage.INPUT_PROMPT}
    )
    cost_class: CostClass = "slow"
    supported_codes: frozenset[str] = frozenset(
        c.value for c in SecretCode
    )
    config_model: type[BaseModel] = SecretScannerConfig
    _sync: bool = True

    def __init__(self) -> None:
        # Import and settings validation happens here so a missing
        # detect-secrets installation fails at registry-build time,
        # not per-request. The settings context manager is applied
        # per-scan inside ``check`` because detect-secrets stores
        # its plugin list in a context-local that must be populated
        # for every scanning thread.
        from detect_secrets import SecretsCollection

        self._collection_cls = SecretsCollection

    async def check(
        self, ctx: Any, config: dict[str, Any]
    ) -> Verdict:
        del config  # secret_scanner currently has no runtime knobs
        text = _extract_text(ctx.stage, ctx.payload)
        if not text.strip():
            return Verdict(action="allow")

        # detect-secrets scans files, not strings, so we materialise
        # to a tempfile. Using a tempfile rather than piping through
        # a named-pipe keeps the plugin code unchanged and avoids
        # cross-platform quirks. The file is unlinked in finally.
        from detect_secrets.settings import default_settings

        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False
            ) as fh:
                fh.write(text)
                tmp_path = fh.name
            with default_settings():
                sc = self._collection_cls()
                sc.scan_file(tmp_path)
                hits = list(sc)
        finally:
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)

        findings: list[Finding] = []
        seen_codes: set[SecretCode] = set()
        for _filename, secret in hits:
            code = _DETECT_SECRETS_MAPPING.get(secret.type)
            if code is None:
                continue
            if code in seen_codes:
                # One finding per type per scan — audit noise goes
                # down without losing information (operators can
                # inspect the raw text if needed via the audit UI).
                continue
            seen_codes.add(code)
            findings.append(
                Finding(
                    code=code.value,
                    severity="critical",
                    message=f"{code.value} pattern detected",
                    metadata={
                        "line": int(secret.line_number),
                        "detector": secret.type,
                    },
                )
            )

        if not findings:
            return Verdict(action="allow")

        return Verdict(
            action="block",
            reason=(
                "Blocked: possible credential leak ("
                + ", ".join(sorted({f.code for f in findings}))
                + ")"
            ),
            findings=findings,
        )


__all__ = ["SecretCode", "SecretScannerCheck", "SecretScannerConfig"]
