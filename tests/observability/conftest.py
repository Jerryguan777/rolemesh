"""Shared fixtures for observability adversarial tests.

The framework's ``install_tracer`` wires a real OTLP/HTTP exporter,
which would require a live collector to introspect spans. These
fixtures replace that wiring with an ``InMemorySpanExporter`` so
each test can read what was actually emitted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def in_memory_tracer() -> Iterator[object]:
    """Real TracerProvider + InMemorySpanExporter via SimpleSpanProcessor.

    SimpleSpanProcessor flushes per-span on ``end()`` so tests can
    introspect synchronously without a deadline. We bypass OTel's
    once-only ``set_tracer_provider`` guard via direct private-attr
    assignment because tests need to swap the provider per case.
    """
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from rolemesh.observability import tracer as _tracer_mod

    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    prev_global = otel_trace._TRACER_PROVIDER
    otel_trace._TRACER_PROVIDER = provider
    prev_provider = _tracer_mod._provider
    prev_installed = _tracer_mod._installed
    _tracer_mod._provider = provider
    _tracer_mod._installed = True

    yield exporter

    _tracer_mod._provider = prev_provider
    _tracer_mod._installed = prev_installed
    otel_trace._TRACER_PROVIDER = prev_global
