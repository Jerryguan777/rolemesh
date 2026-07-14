"""Container-side error classification (retryable vs non-retryable).

The agent runner is the one place that can classify a failure at the
source: ``pi.ai.types.NonRetryableConfigError`` marks deterministic
configuration errors raised at local validation points; everything else
stays retryable (fail-open — a transient fault misclassified as
permanent silently drops a recoverable message, which is worse than a
permanent fault being retried).

Bug-bait focus:

* Whitelist-by-type, not by message: a plain ValueError with a scary
  message must remain retryable.
* Wire shape stays legacy-compatible: ``retryable`` only appears in the
  JSON when False, mirroring the ``isFinal`` convention, so older
  orchestrators keep seeing the exact same payloads for every
  retryable outcome.
"""

from __future__ import annotations

import pytest

from agent_runner.main import ContainerOutput, is_retryable_error
from pi.ai.types import NonRetryableConfigError

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_config_error_is_non_retryable() -> None:
    assert is_retryable_error(NonRetryableConfigError("bad tool name")) is False


def test_plain_value_error_stays_retryable() -> None:
    # Same base class, not whitelisted — message content must not matter.
    assert is_retryable_error(ValueError("tool name invalid")) is True


def test_runtime_and_os_errors_stay_retryable() -> None:
    assert is_retryable_error(RuntimeError("NATS gone")) is True
    assert is_retryable_error(OSError("connection reset")) is True


def test_subclass_of_config_error_is_non_retryable() -> None:
    class ProviderSchemaError(NonRetryableConfigError):
        pass

    assert is_retryable_error(ProviderSchemaError("x")) is False


# ---------------------------------------------------------------------------
# Wire shape
# ---------------------------------------------------------------------------


def test_retryable_true_is_omitted_from_wire() -> None:
    """Default (retryable) errors serialize exactly as before this change."""
    d = ContainerOutput(status="error", result=None, error="x").to_dict()
    assert "retryable" not in d


def test_retryable_false_is_emitted() -> None:
    d = ContainerOutput(
        status="error", result=None, error="x", retryable=False
    ).to_dict()
    assert d["retryable"] is False


# ---------------------------------------------------------------------------
# Source raise sites
# ---------------------------------------------------------------------------


def test_bedrock_tool_name_guard_raises_config_error() -> None:
    """The provider's local tool-name validation is the canonical
    non-retryable source — the exact failure that used to burn the whole
    retry ladder."""
    boto3 = pytest.importorskip("boto3")  # noqa: F841 — provider needs it at import
    from pi.ai.providers.amazon_bedrock import _convert_tool_config
    from pi.ai.types import Tool

    bad = Tool(name="x" * 70, description="d", parameters={"type": "object"})
    with pytest.raises(NonRetryableConfigError):
        _convert_tool_config([bad], "auto")
