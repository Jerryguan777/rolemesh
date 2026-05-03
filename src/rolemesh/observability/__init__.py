"""OpenTelemetry-based observability for RoleMesh (spike).

A purposely thin wrapper over the OpenTelemetry SDK so call sites can
do ``with tracer.start_as_current_span(...)`` without conditionally
importing OTel. When the optional ``[observability]`` extra is not
installed, or ``OTEL_EXPORTER_OTLP_ENDPOINT`` is unset, every
operation degrades to the SDK's built-in NoOp implementation —
behaviour stays bit-identical to a build without observability.

Public surface:
  - install_tracer(service_name, **resource_attrs): idempotent setup
    of the global tracer provider + OTLP exporter.
  - get_tracer(name): returns a real or noop ``Tracer``; safe to call
    before install_tracer.
  - inject_trace_context(): produce a ``dict[str, str]`` carrier with
    the current span's W3C ``traceparent`` / ``tracestate`` headers.
  - extract_trace_context(carrier): hydrate a parent context from a
    carrier produced by ``inject_trace_context``; pass the returned
    object as ``context=`` on the next ``start_as_current_span``.

Why a wrapper rather than direct OTel imports at call sites: the
agent runner and orchestrator are imported by the test suite, which
is not allowed to require the ``observability`` extra. The wrapper
keeps imports of ``opentelemetry`` lazy + try/except so a stock
install of the project still imports cleanly.
"""

from rolemesh.observability.tracer import (
    attach_parent_context,
    detach_parent_context,
    extract_trace_context,
    get_tracer,
    inject_trace_context,
    install_tracer,
    is_enabled,
)

__all__ = [
    "attach_parent_context",
    "detach_parent_context",
    "extract_trace_context",
    "get_tracer",
    "inject_trace_context",
    "install_tracer",
    "is_enabled",
]
