"""Spike-tier tests: confirm the observability module is a true no-op
when (a) the optional ``[observability]`` extra is not installed, or
(b) ``OTEL_EXPORTER_OTLP_ENDPOINT`` is not set.

These tests are the "default deployment stays bit-identical" guarantee.
They run on every CI shard — if they fail, observability has stopped
being optional and we've broken default installs.

We don't try to test the *positive* path here (real OTLP export) — the
spike walkthrough in ``docs/observability/spike.md`` covers that
manually against a live Langfuse, and a unit test of OTel SDK
internals would mostly re-test OTel itself.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest


def test_get_tracer_returns_a_usable_object_without_otel(monkeypatch: pytest.MonkeyPatch) -> None:
    """``get_tracer`` must always return something with the standard
    OTel tracer surface — at minimum ``start_as_current_span`` and
    ``start_span`` — so call sites can use it unconditionally.
    """
    # Force the "OTel SDK absent" branch by hiding the module from
    # the import system. Importlib reload picks up the change.
    monkeypatch.setitem(__import__("sys").modules, "opentelemetry", None)

    import rolemesh.observability.tracer as tr_mod

    importlib.reload(tr_mod)
    tracer = tr_mod.get_tracer("test")
    # Both methods exist and return a context-manager-shaped span.
    span = tracer.start_as_current_span("noop")
    assert hasattr(span, "__enter__")
    assert hasattr(span, "__exit__")
    with span as s:
        # set_attribute / set_status / record_exception / end must all
        # be callable without raising.
        s.set_attribute("k", "v")
        s.set_status("OK")
        s.record_exception(RuntimeError("ignored"))
        s.end()


def test_install_tracer_is_noop_without_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``OTEL_EXPORTER_OTLP_ENDPOINT`` is unset, ``install_tracer``
    short-circuits and ``is_enabled`` stays False even if the SDK is
    installed.
    """
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    import rolemesh.observability.tracer as tr_mod

    importlib.reload(tr_mod)
    tr_mod.install_tracer("test-service")
    assert tr_mod.is_enabled() is False


def test_inject_extract_round_trip_without_otel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without OTel installed, ``inject_trace_context`` returns ``{}``
    and ``extract_trace_context`` returns ``None`` — the wire format
    of "no upstream span" that downstream code branches on.
    """
    monkeypatch.setitem(__import__("sys").modules, "opentelemetry", None)
    monkeypatch.setitem(__import__("sys").modules, "opentelemetry.propagate", None)
    import rolemesh.observability.tracer as tr_mod

    importlib.reload(tr_mod)
    assert tr_mod.inject_trace_context() == {}
    assert tr_mod.extract_trace_context({}) is None
    assert tr_mod.extract_trace_context({"traceparent": "abc"}) is None


def test_attach_detach_no_otel_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Attach/detach degrade to ``None`` token + no-op detach when
    OTel is absent. Caller code can still write the standard
    try/finally without conditionals.
    """
    monkeypatch.setitem(__import__("sys").modules, "opentelemetry", None)
    import rolemesh.observability.tracer as tr_mod

    importlib.reload(tr_mod)
    token = tr_mod.attach_parent_context({"traceparent": "ignored"})
    assert token is None
    tr_mod.detach_parent_context(token)  # must not raise
    tr_mod.detach_parent_context(None)
