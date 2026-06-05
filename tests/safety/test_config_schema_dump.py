"""P0-1: every check's ``config_schema`` dump is non-empty and honest.

The v1 ``/safety/checks`` endpoint exposes each check's
``config_model.model_json_schema()`` so the SPA renders the config form
from the authoritative source instead of hardcoding field names (the
schema-drift incident the P0-1 spec calls out). These tests pin two
things per check model: the dump is a non-trivial object with
``additionalProperties: false`` (the ``extra='forbid'`` contract the
frontend validator relies on to reject stray keys), and — for the
enum-bearing checks — that the closed value set is expressed *in the
schema* (``propertyNames`` / ``items.enum``) rather than hidden in
Python-only validation.

Config models import without the checks' heavy optional deps
(presidio / llm_guard / detect_secrets), so this stays a pure unit test.
"""

from __future__ import annotations

import pytest

from rolemesh.safety.checks.domain_allowlist import DomainAllowlistConfig
from rolemesh.safety.checks.egress_domain_rule import EgressDomainRuleConfig
from rolemesh.safety.checks.llm_guard_jailbreak import (
    LLMGuardJailbreakConfig,
)
from rolemesh.safety.checks.llm_guard_prompt_injection import (
    LLMGuardPromptInjectionConfig,
)
from rolemesh.safety.checks.llm_guard_toxicity import LLMGuardToxicityConfig
from rolemesh.safety.checks.openai_moderation import OpenAIModerationConfig
from rolemesh.safety.checks.pii_regex import PIIRegexConfig
from rolemesh.safety.checks.presidio_pii import PresidioPIIConfig
from rolemesh.safety.checks.secret_scanner import SecretScannerConfig

# All nine V1/V2 check config models, keyed by check id.
ALL_CONFIG_MODELS = {
    "pii.regex": PIIRegexConfig,
    "presidio.pii": PresidioPIIConfig,
    "secret_scanner": SecretScannerConfig,
    "domain_allowlist": DomainAllowlistConfig,
    "egress.domain_rule": EgressDomainRuleConfig,
    "llm_guard.prompt_injection": LLMGuardPromptInjectionConfig,
    "llm_guard.jailbreak": LLMGuardJailbreakConfig,
    "llm_guard.toxicity": LLMGuardToxicityConfig,
    "openai_moderation": OpenAIModerationConfig,
}


@pytest.mark.parametrize("check_id", sorted(ALL_CONFIG_MODELS))
def test_config_schema_dump_is_non_empty_object(check_id: str) -> None:
    schema = ALL_CONFIG_MODELS[check_id].model_json_schema()
    assert isinstance(schema, dict) and schema, check_id
    assert schema.get("type") == "object", check_id
    assert "properties" in schema, check_id


@pytest.mark.parametrize("check_id", sorted(ALL_CONFIG_MODELS))
def test_config_schema_forbids_extra_keys(check_id: str) -> None:
    # extra='forbid' → additionalProperties:false. The frontend
    # validator leans on this to reject arbitrary stray keys.
    schema = ALL_CONFIG_MODELS[check_id].model_json_schema()
    assert schema.get("additionalProperties") is False, check_id


def _resolve(schema: dict, node: dict) -> dict:
    """Follow a single ``$ref`` into ``$defs`` (Pydantic factors enums
    out into ``$defs``; Ajv resolves the ref the same way)."""
    ref = node.get("$ref")
    if not ref:
        return node
    name = ref.rsplit("/", 1)[-1]
    return schema["$defs"][name]


def test_pii_regex_pattern_keys_are_constrained() -> None:
    schema = PIIRegexConfig.model_json_schema()
    patterns = schema["properties"]["patterns"]
    # propertyNames is JSON Schema's "the dict's keys must be one of".
    key_schema = _resolve(schema, patterns["propertyNames"])
    assert set(key_schema["enum"]) == {
        "SSN",
        "CREDIT_CARD",
        "EMAIL",
        "PHONE_US",
        "IP_ADDRESS",
    }
    # values stay booleans
    assert patterns["additionalProperties"] == {"type": "boolean"}


def test_presidio_codes_are_enum_constrained() -> None:
    schema = PresidioPIIConfig.model_json_schema()
    for field in ("block_codes", "redact_codes"):
        items = _resolve(schema, schema["properties"][field]["items"])
        assert "PII.SSN" in items["enum"], field
        assert "PII.MEDICAL_LICENSE" in items["enum"], field


def test_openai_categories_are_enum_constrained() -> None:
    schema = OpenAIModerationConfig.model_json_schema()
    for field in ("block_categories", "warn_categories"):
        items = _resolve(schema, schema["properties"][field]["items"])
        assert "MODERATION.HATE" in items["enum"], field
        assert "MODERATION.VIOLENCE" in items["enum"], field
