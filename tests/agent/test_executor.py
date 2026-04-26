"""Tests for rolemesh.agent.executor -- data types and backend configs."""

from __future__ import annotations

from rolemesh.agent.executor import (
    BACKEND_CONFIGS,
    CLAUDE_CODE_BACKEND,
    PI_BACKEND,
    AgentBackendConfig,
    AgentInput,
    AgentOutput,
)
from rolemesh.auth.permissions import AgentPermissions


def test_agent_output_is_final_default_true() -> None:
    """AgentOutput.is_final defaults True so legacy backends (no isFinal on
    the wire) continue to signal end-of-turn on their sole success event."""
    out = AgentOutput(status="success", result="done")
    assert out.is_final is True


def test_agent_output_is_final_false_explicit() -> None:
    """is_final=False is how batched-reply backends say 'more replies are
    coming in this run_prompt'."""
    out = AgentOutput(status="success", result="part1", is_final=False)
    assert out.is_final is False


def test_parse_container_output_reads_is_final_false() -> None:
    """The orchestrator parses isFinal=False off the NATS payload and carries
    it into AgentOutput so _on_output can gate notify_idle."""
    from rolemesh.agent.container_executor import _parse_container_output

    raw = {"status": "success", "result": "reply1", "isFinal": False}
    out = _parse_container_output(raw)
    assert out.status == "success"
    assert out.result == "reply1"
    assert out.is_final is False


def test_parse_container_output_missing_is_final_defaults_true() -> None:
    """Legacy wire format without isFinal → is_final=True (end of turn)."""
    from rolemesh.agent.container_executor import _parse_container_output

    raw = {"status": "success", "result": "reply"}
    out = _parse_container_output(raw)
    assert out.is_final is True


def test_parse_container_output_non_bool_is_final_falls_back_true() -> None:
    """Garbage in the isFinal slot must NOT silently turn into a truthy value;
    anything that isn't a real bool is treated as 'legacy payload' → True."""
    from rolemesh.agent.container_executor import _parse_container_output

    raw = {"status": "success", "result": "r", "isFinal": "false"}  # string, not bool
    out = _parse_container_output(raw)
    assert out.is_final is True


def test_agent_input_frozen() -> None:
    perms = AgentPermissions.for_role("super_agent").to_dict()
    inp = AgentInput(prompt="hello", group_folder="g", chat_jid="j", permissions=perms)
    try:
        inp.prompt = "other"  # type: ignore[misc]
        raise AssertionError("Should have raised")
    except AttributeError:
        pass


def test_agent_input_optional_fields() -> None:
    perms = AgentPermissions.for_role("agent").to_dict()
    inp = AgentInput(prompt="p", group_folder="g", chat_jid="j", permissions=perms)
    assert inp.session_id is None
    assert inp.is_scheduled_task is False
    assert inp.assistant_name is None
    assert inp.system_prompt is None
    assert inp.role_config is None
    assert inp.user_id == ""


def test_agent_input_all_fields() -> None:
    perms = AgentPermissions.for_role("super_agent").to_dict()
    inp = AgentInput(
        prompt="hello",
        group_folder="grp",
        chat_jid="jid",
        permissions=perms,
        user_id="user-1",
        session_id="s1",
        is_scheduled_task=True,
        assistant_name="Andy",
        system_prompt="You are helpful",
        role_config={"role": "coder"},
    )
    assert inp.prompt == "hello"
    assert inp.permissions["data_scope"] == "tenant"
    assert inp.user_id == "user-1"
    assert inp.system_prompt == "You are helpful"
    assert inp.role_config == {"role": "coder"}


def test_agent_output_frozen() -> None:
    out = AgentOutput(status="success", result="done")
    try:
        out.status = "error"  # type: ignore[misc]
        raise AssertionError("Should have raised")
    except AttributeError:
        pass


def test_agent_output_optional_fields() -> None:
    out = AgentOutput(status="success", result=None)
    assert out.new_session_id is None
    assert out.error is None
    assert out.metadata is None


def test_agent_output_is_progress_for_transient_statuses() -> None:
    for status in ("queued", "container_starting", "running", "tool_use"):
        out = AgentOutput(status=status, result=None)  # type: ignore[arg-type]
        assert out.is_progress() is True, f"{status} should be progress"


def test_agent_output_is_progress_false_for_terminal() -> None:
    assert AgentOutput(status="success", result="x").is_progress() is False
    assert AgentOutput(status="error", result=None, error="e").is_progress() is False
    assert AgentOutput(status="stopped", result=None).is_progress() is False


def test_agent_output_stopped_terminal_status() -> None:
    out = AgentOutput(status="stopped", result=None, new_session_id="s1")
    assert out.status == "stopped"
    assert out.result is None
    assert out.new_session_id == "s1"
    # stopped is in TERMINAL_STATUSES
    from rolemesh.agent.executor import TERMINAL_STATUSES
    assert "stopped" in TERMINAL_STATUSES


def test_agent_output_metadata_survives_construction() -> None:
    out = AgentOutput(
        status="tool_use",
        result=None,
        metadata={"tool": "Bash", "input": "ls /tmp"},
    )
    assert out.metadata == {"tool": "Bash", "input": "ls /tmp"}
    assert out.is_progress() is True


def test_agent_backend_config_defaults() -> None:
    cfg = AgentBackendConfig(name="test", image="test:latest")
    assert cfg.entrypoint is None
    assert cfg.extra_mounts == []
    assert cfg.extra_env == {}
    assert cfg.skip_claude_session is False


def test_claude_code_backend_preset() -> None:
    assert CLAUDE_CODE_BACKEND.name == "claude"
    assert CLAUDE_CODE_BACKEND.image == "rolemesh-agent:latest"
    assert CLAUDE_CODE_BACKEND.entrypoint is None
    assert CLAUDE_CODE_BACKEND.skip_claude_session is False
    assert CLAUDE_CODE_BACKEND.extra_env == {"AGENT_BACKEND": "claude"}


def test_pi_backend_preset() -> None:
    assert PI_BACKEND.name == "pi"
    assert PI_BACKEND.image == "rolemesh-agent:latest"
    assert PI_BACKEND.entrypoint is None
    assert PI_BACKEND.skip_claude_session is True
    assert PI_BACKEND.extra_env["AGENT_BACKEND"] == "pi"


def test_backend_configs_map() -> None:
    assert "claude" in BACKEND_CONFIGS
    assert "pi" in BACKEND_CONFIGS
    assert "claude-code" in BACKEND_CONFIGS  # legacy alias
    assert BACKEND_CONFIGS["claude"] is CLAUDE_CODE_BACKEND
    assert BACKEND_CONFIGS["pi"] is PI_BACKEND
    # Legacy alias must resolve to the same config object
    assert BACKEND_CONFIGS["claude-code"] is BACKEND_CONFIGS["claude"]


def test_agent_backend_config_frozen() -> None:
    cfg = AgentBackendConfig(name="t", image="i")
    try:
        cfg.name = "other"  # type: ignore[misc]
        raise AssertionError("Should have raised")
    except AttributeError:
        pass


# ---------------------------------------------------------------------------
# _pi_extra_env — Bedrock injection (placeholder + synthesized URL)
# ---------------------------------------------------------------------------


import pytest

from rolemesh.agent.executor import _pi_extra_env


class TestPiExtraEnvBedrock:
    """The Bedrock branch in ``_pi_extra_env`` is the security boundary
    for "the real ABSK token never enters an agent container".
    These cases verify the contract from both sides — what MUST land
    in the container env (placeholders + the proxy URL) and what MUST
    NOT (the literal host token, raw ``localhost`` URL the operator
    might have stashed in ``.env``).
    """

    def test_no_pi_model_id_only_returns_backend_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PI_MODEL_ID", raising=False)
        env = _pi_extra_env()
        assert env == {"AGENT_BACKEND": "pi"}

    def test_non_bedrock_model_id_does_not_inject_aws_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Anthropic-direct or OpenAI Pi model ids must NOT spuriously
        # inject AWS_BEARER_TOKEN_BEDROCK / BEDROCK_BASE_URL — those
        # only make sense on the Bedrock route.
        monkeypatch.setenv("PI_MODEL_ID", "anthropic/claude-3-5-sonnet")
        # Even with a real-looking host token set, a non-bedrock
        # model must not pull it / a placeholder into the container.
        monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "ABSKsecret")
        env = _pi_extra_env()
        assert "AWS_BEARER_TOKEN_BEDROCK" not in env
        assert "BEDROCK_BASE_URL" not in env

    def test_bedrock_model_id_injects_placeholder_not_host_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Critical: the host's real token must NEVER appear in the
        # container env. Set a recognizable host token, then assert
        # the container sees only the placeholder.
        monkeypatch.setenv(
            "PI_MODEL_ID", "amazon-bedrock/us.anthropic.claude-sonnet-4-6"
        )
        monkeypatch.setenv(
            "AWS_BEARER_TOKEN_BEDROCK", "ABSKreal-host-secret-do-not-leak"
        )
        env = _pi_extra_env()
        token_in_env = env.get("AWS_BEARER_TOKEN_BEDROCK", "")
        assert "ABSKreal-host-secret" not in token_in_env, (
            "host's real Bedrock token leaked into agent container env"
        )
        assert token_in_env  # but placeholder MUST be set
        assert "placeholder" in token_in_env.lower()

    def test_bedrock_url_NOT_set_in_pi_extra_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Architectural pin: ``BEDROCK_BASE_URL`` belongs in
        # ``container.runner.build_container_spec`` alongside the
        # other base URLs (Anthropic / OpenAI / Google) because it
        # depends on the per-spawn ``proxy_base`` decision (EC-2 →
        # ``egress-gateway``; rollback → ``host.docker.internal``).
        # Setting it from ``_pi_extra_env`` (module-load time) baked
        # the rollback-path host into a per-spawn value and made the
        # Bedrock path silently broken under EC-2. This test pins the
        # invariant so a future refactor doesn't put it back.
        monkeypatch.setenv("PI_MODEL_ID", "amazon-bedrock/anything")
        monkeypatch.setenv("BEDROCK_BASE_URL", "http://localhost:9999/wrong")
        env = _pi_extra_env()
        assert "BEDROCK_BASE_URL" not in env, (
            "BEDROCK_BASE_URL must be set in build_container_spec, "
            "not _pi_extra_env"
        )

    def test_bedrock_default_region_is_us_east_1(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PI_MODEL_ID", "amazon-bedrock/foo")
        monkeypatch.delenv("AWS_REGION", raising=False)
        env = _pi_extra_env()
        assert env["AWS_REGION"] == "us-east-1"

    def test_bedrock_region_propagates_when_host_sets_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PI_MODEL_ID", "amazon-bedrock/foo")
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        env = _pi_extra_env()
        assert env["AWS_REGION"] == "us-west-2"

    def test_warns_when_host_lacks_bedrock_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # P1 ergonomics: an operator who set
        # ``PI_MODEL_ID=amazon-bedrock/...`` but forgot
        # ``AWS_BEARER_TOKEN_BEDROCK`` would otherwise hit a runtime
        # 404 from the credential proxy with no obvious cause.
        # Validate the warning fires at container-spec-build time
        # so the misconfiguration is visible up front.
        #
        # rolemesh uses structlog (not stdlib logging), so caplog can't
        # observe it; spy on the logger directly instead.
        from rolemesh.agent import executor as executor_mod

        captured: list[tuple[str, dict[str, object]]] = []

        def _spy(msg: str, **kwargs: object) -> None:
            captured.append((msg, kwargs))

        monkeypatch.setattr(executor_mod.logger, "warning", _spy)
        monkeypatch.setenv("PI_MODEL_ID", "amazon-bedrock/foo")
        monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)

        env = executor_mod._pi_extra_env()

        # Container env still gets the placeholder — we degrade
        # gracefully rather than refuse to spawn.
        assert env["AWS_BEARER_TOKEN_BEDROCK"] == "placeholder-proxy-replaces-this"
        # And we logged a warning naming the symptom + fix, with the
        # offending model_id attached for log-search ergonomics.
        assert len(captured) == 1
        msg, kwargs = captured[0]
        assert "AWS_BEARER_TOKEN_BEDROCK" in msg
        assert kwargs.get("model_id") == "amazon-bedrock/foo"

    def test_no_warning_when_host_has_bedrock_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from rolemesh.agent import executor as executor_mod

        captured: list[tuple[str, dict[str, object]]] = []

        def _spy(msg: str, **kwargs: object) -> None:
            captured.append((msg, kwargs))

        monkeypatch.setattr(executor_mod.logger, "warning", _spy)
        monkeypatch.setenv("PI_MODEL_ID", "amazon-bedrock/foo")
        monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "ABSKtoken")

        executor_mod._pi_extra_env()

        # Quiet path — host configured correctly, no nag.
        bedrock_warnings = [
            (m, k) for m, k in captured if "AWS_BEARER_TOKEN_BEDROCK" in m
        ]
        assert bedrock_warnings == []
