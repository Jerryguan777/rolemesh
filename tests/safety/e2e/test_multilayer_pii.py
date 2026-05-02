"""V2 acceptance scenario A — multi-layer PII defense.

Three rules cooperate on a single agent turn:

  1. ``pii.regex`` @ INPUT_PROMPT — block on SSN (cheap, container-side)
  2. ``presidio.pii`` @ MODEL_OUTPUT — redact EMAIL_ADDRESS (slow,
     orchestrator-side, ML-backed)
  3. ``secret_scanner`` @ MODEL_OUTPUT — block on credentials (slow,
     orchestrator-side, detect-secrets-backed)

Without this E2E the multi-check interactions (redact chain + block
short-circuit + orch-side MODEL_OUTPUT path) are only covered by
pipeline unit tests with stub checks. That leaves two real bugs
invisible:
  A. ``presidio.pii`` and ``secret_scanner`` both fire on MODEL_OUTPUT.
     In a redact→block order, the redacted payload must reach the
     block check (the second rule sees the post-redact text). A
     regression where redact doesn't propagate would allow a secret
     in text that presidio already rewrote to leak through.
  B. The container-side pipeline (INPUT_PROMPT) and the orch-side
     pipeline (MODEL_OUTPUT) share pipeline_core; a signature drift
     between them would only surface under this combined run.

The ML checks run ``pytest.importorskip`` so the test still executes
cheap-only paths when ``[safety-ml]`` isn't installed — skipping
cleanly rather than failing the suite.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from agent_runner.hooks.events import UserPromptEvent
from agent_runner.safety.hook_handler import SafetyHookHandler
from agent_runner.safety.registry import build_container_registry
from rolemesh.db import (
    create_coworker,
    create_safety_rule,
    create_tenant,
    list_safety_decisions,
    list_safety_rules_for_coworker,
)
from rolemesh.ipc.protocol import AgentInitData
from rolemesh.safety.engine import SafetyEngine
from rolemesh.safety.subscriber import (
    SafetyEventsSubscriber,
    TrustedCoworker,
)
from rolemesh.safety.types import SafetyContext, Stage

pytestmark = pytest.mark.usefixtures("test_db")


@dataclass
class _FakeToolCtx:
    tenant_id: str
    coworker_id: str
    job_id: str = "job-multi"
    conversation_id: str = "conv-multi"
    user_id: str = "user-multi"
    group_folder: str = ""
    permissions: dict[str, Any] = field(default_factory=dict)
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def publish(self, subject: str, data: dict[str, Any]) -> None:
        self.events.append((subject, dict(data)))

    def get_tool_reversibility(self, _tool_name: str) -> bool:
        return False


@dataclass(frozen=True)
class _TrustedRec:
    tenant_id: str
    id: str


class TestMultiLayerPII:
    """Exercises the three checks against a single conversation turn."""

    @pytest.mark.asyncio
    async def test_ssn_prompt_blocked_at_input_stage(self) -> None:
        """Prompt with SSN → container-side pii.regex blocks before the
        agent even sees it. This is the INPUT_PROMPT layer of the
        defense-in-depth chain — no MODEL_OUTPUT rules run because the
        turn is aborted here. Also confirms the audit event reaches
        safety_decisions via the real subscriber trust boundary.
        """
        tenant = await create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        cw = await create_coworker(
            tenant_id=tenant.id, name="cw",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
        )
        rule_input = await create_safety_rule(
            tenant_id=tenant.id,
            stage="input_prompt",
            check_id="pii.regex",
            config={"patterns": {"SSN": True}},
            description="block SSN in user prompts",
        )

        # Simulate container boot: load snapshots + round-trip through
        # AgentInitData so any JSON-serialization regression surfaces.
        rules = await list_safety_rules_for_coworker(tenant.id, cw.id)
        snapshot_dicts = [r.to_snapshot_dict() for r in rules]
        init = AgentInitData(
            prompt="",
            group_folder=cw.folder,
            chat_jid="chat",
            tenant_id=tenant.id,
            coworker_id=cw.id,
            safety_rules=snapshot_dicts,
        )
        decoded = AgentInitData.deserialize(init.serialize())
        assert decoded.safety_rules is not None

        tool_ctx = _FakeToolCtx(
            tenant_id=tenant.id, coworker_id=cw.id
        )
        handler = SafetyHookHandler(
            rules=decoded.safety_rules,
            registry=build_container_registry(),
            tool_ctx=tool_ctx,  # type: ignore[arg-type]
        )
        verdict = await handler.on_user_prompt_submit(
            UserPromptEvent(prompt="my SSN is 123-45-6789 please help")
        )
        assert verdict is not None
        assert verdict.block is True
        assert verdict.reason and "PII.SSN" in verdict.reason

        # Audit round-trip through real subscriber (same pattern as
        # V1 e2e — feed bytes to exercise JSON decode + trust check).
        assert tool_ctx.events, "block must publish exactly one audit event"
        _, event_payload = tool_ctx.events[0]

        def _lookup(cid: str) -> TrustedCoworker | None:
            if cid == cw.id:
                return _TrustedRec(tenant_id=tenant.id, id=cw.id)
            return None

        engine = SafetyEngine()
        subscriber = SafetyEventsSubscriber(
            engine=engine, coworker_lookup=_lookup
        )
        await subscriber.on_message_bytes(
            json.dumps(event_payload).encode()
        )

        decisions = await list_safety_decisions(tenant.id)
        assert len(decisions) == 1
        d = decisions[0]
        assert d["verdict_action"] == "block"
        assert d["stage"] == "input_prompt"
        assert d["triggered_rule_ids"] == [rule_input.id]

    @pytest.mark.asyncio
    async def test_secret_in_model_output_blocked_even_after_email_redact(
        self,
    ) -> None:
        """MODEL_OUTPUT with both an EMAIL and a credential. The redact
        rule (presidio.pii) must run first (higher priority) and
        rewrite the text; secret_scanner then sees the redacted text
        and blocks on the still-present credential. Result is a BLOCK
        (secret_scanner short-circuits), not a redact — the block-
        after-redact ordering matters.

        Skips cleanly when safety-ml extras are missing.
        """
        pytest.importorskip("presidio_analyzer")
        pytest.importorskip("detect_secrets")

        tenant = await create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        cw = await create_coworker(
            tenant_id=tenant.id, name="cw",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
        )
        # presidio.pii with higher priority so it runs FIRST (designs
        # the redact-before-block ordering). secret_scanner runs at
        # lower priority on the redacted payload.
        rule_presidio = await create_safety_rule(
            tenant_id=tenant.id,
            stage="model_output",
            check_id="presidio.pii",
            config={"redact_codes": ["PII.EMAIL"]},
            priority=99,
            description="redact emails in model output",
        )
        rule_secret = await create_safety_rule(
            tenant_id=tenant.id,
            stage="model_output",
            check_id="secret_scanner",
            config={},
            priority=50,
            description="block credentials in model output",
        )

        # Load snapshot and run orchestrator-side pipeline.
        engine = SafetyEngine()
        rules = await engine.load_rules_for_coworker(
            tenant.id, cw.id
        )
        # The snapshot should contain both rules in DESC priority order
        # as the pipeline will sort them (presidio first, then secret).
        rule_ids = {r["id"] for r in rules}
        assert rule_presidio.id in rule_ids
        assert rule_secret.id in rule_ids

        ctx = SafetyContext(
            stage=Stage.MODEL_OUTPUT,
            tenant_id=tenant.id,
            coworker_id=cw.id,
            user_id="u",
            job_id="",
            conversation_id="conv-multi",
            payload={
                "text": (
                    "contact me at bob@example.com "
                    "aws_key = AKIAIOSFODNN7EXAMPLE"
                ),
            },
        )
        verdict = await engine.run_orchestrator_pipeline(ctx, rules)

        # secret_scanner blocks → pipeline returns block. The redact
        # already happened on the in-flight payload; if block lost
        # the findings accumulator this test would report 1 finding
        # instead of 2.
        assert verdict.action == "block"
        codes = {f.code for f in verdict.findings}
        assert "SECRET.AWS_KEY" in codes
        # Regression guard: the redact finding from the earlier rule
        # MUST still be in the combined findings array even though the
        # final action is block (pipeline accumulates across rules).
        assert "PII.EMAIL" in codes

        # Audit rows: both rules produced events (redact per-rule
        # publish + block short-circuit publish). Two rows total,
        # newest-first means block row is first.
        decisions = await list_safety_decisions(tenant.id)
        assert len(decisions) == 2
        actions = [d["verdict_action"] for d in decisions]
        assert "block" in actions
        assert "redact" in actions

    @pytest.mark.asyncio
    async def test_clean_output_allows_but_rules_still_recorded(
        self,
    ) -> None:
        """A clean MODEL_OUTPUT produces allow verdicts — the per-rule
        audit writes still happen (operators can see "rule X ran on N
        turns this week" for coverage). Regression guard: silencing
        per-rule allow events would break coverage dashboards.
        """
        pytest.importorskip("presidio_analyzer")
        pytest.importorskip("detect_secrets")

        tenant = await create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        cw = await create_coworker(
            tenant_id=tenant.id, name="cw",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
        )
        await create_safety_rule(
            tenant_id=tenant.id,
            stage="model_output",
            check_id="presidio.pii",
            config={"redact_codes": ["PII.EMAIL"]},
            priority=99,
        )
        await create_safety_rule(
            tenant_id=tenant.id,
            stage="model_output",
            check_id="secret_scanner",
            config={},
            priority=50,
        )

        engine = SafetyEngine()
        rules = await engine.load_rules_for_coworker(
            tenant.id, cw.id
        )
        ctx = SafetyContext(
            stage=Stage.MODEL_OUTPUT,
            tenant_id=tenant.id,
            coworker_id=cw.id,
            user_id="u",
            job_id="",
            conversation_id="conv-multi",
            payload={
                "text": "Here's the information you asked for. Let me know if you need anything else.",
            },
        )
        verdict = await engine.run_orchestrator_pipeline(ctx, rules)
        assert verdict.action == "allow"

        # Both rules ran and each emitted one allow audit row. The
        # tail allow (pipeline's final no-match return) does NOT emit
        # a row — this test pins both invariants.
        decisions = await list_safety_decisions(tenant.id)
        assert len(decisions) == 2
        assert all(d["verdict_action"] == "allow" for d in decisions)

    @pytest.mark.asyncio
    async def test_email_only_produces_redact_not_block(self) -> None:
        """MODEL_OUTPUT contains an email but no credential → only
        presidio fires (redact); secret_scanner returns allow. Final
        verdict is redact with the anonymized text in modified_payload.

        Negative test for the test_secret_in_model_output_blocked_...
        assertion: without the credential the chain must NOT produce
        a block. Catches a regression where secret_scanner false-
        positives on presidio-anonymized placeholders like
        ``<EMAIL_ADDRESS>``.
        """
        pytest.importorskip("presidio_analyzer")
        pytest.importorskip("detect_secrets")

        tenant = await create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        cw = await create_coworker(
            tenant_id=tenant.id, name="cw",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
        )
        await create_safety_rule(
            tenant_id=tenant.id,
            stage="model_output",
            check_id="presidio.pii",
            config={"redact_codes": ["PII.EMAIL"]},
            priority=99,
        )
        await create_safety_rule(
            tenant_id=tenant.id,
            stage="model_output",
            check_id="secret_scanner",
            config={},
            priority=50,
        )

        engine = SafetyEngine()
        rules = await engine.load_rules_for_coworker(
            tenant.id, cw.id
        )
        ctx = SafetyContext(
            stage=Stage.MODEL_OUTPUT,
            tenant_id=tenant.id,
            coworker_id=cw.id,
            user_id="u",
            job_id="",
            conversation_id="conv-multi",
            payload={
                "text": "contact me at bob@example.com for details",
            },
        )
        verdict = await engine.run_orchestrator_pipeline(ctx, rules)
        assert verdict.action == "redact"
        # The anonymized text must be present and must NOT contain
        # the original email.
        assert isinstance(verdict.modified_payload, dict)
        anonymized = verdict.modified_payload.get("text", "")
        assert "bob@example.com" not in anonymized
        # Findings include the redact marker, but NO SECRET.* code
        # — that would indicate secret_scanner false-positived.
        codes = {f.code for f in verdict.findings}
        assert "PII.EMAIL" in codes
        assert not any(c.startswith("SECRET.") for c in codes), (
            f"secret_scanner must not false-positive on anonymized "
            f"placeholders, got {codes}"
        )
