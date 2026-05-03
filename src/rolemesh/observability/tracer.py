"""Tracer setup + W3C trace-context propagation helpers.

All public functions are safe to call when ``opentelemetry`` is not
installed: imports are lazy and guarded. When OTel is missing, the
returned tracer is OTel's NoOp implementation (also lazy-imported on
demand from a tiny vendored shim if even the SDK isn't on the
import path), and ``install_tracer`` is a no-op.

Activation criteria (all required):
  1. The ``observability`` optional extra is installed
     (``opentelemetry-sdk`` + ``opentelemetry-exporter-otlp-proto-http``).
  2. ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set (e.g. Langfuse's OTLP
     intake at ``https://<host>/api/public/otel``).
  3. ``install_tracer(service_name)`` is called once per process.

When (1) or (2) fails, ``install_tracer`` logs and returns; subsequent
``get_tracer`` calls return a noop tracer so call sites keep working.

This module is intentionally small for the spike. Once the spike is
validated, follow-ups belong here:
  - resource auto-detect (container id, k8s pod, host)
  - sampling policy (TraceIdRatioBased) for prod volume
  - structlog<>OTel context bridge so log lines auto-pick trace_id
  - W3C ``baggage`` propagation for ``tenant.id`` / ``coworker.id``
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from rolemesh.core.logger import get_logger

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer

logger = get_logger()

_installed: bool = False


def is_enabled() -> bool:
    """Whether the SDK was successfully installed in this process.

    Call sites can branch on this when they want to skip work that's
    only meaningful with collection turned on (e.g. building large
    span attributes). Plain ``with tracer.start_as_current_span(...)``
    blocks do not need to gate — the noop tracer is cheap.
    """
    return _installed


def install_tracer(
    service_name: str,
    **resource_attrs: Any,
) -> None:
    """Install the global OTel tracer provider + OTLP HTTP exporter.

    Idempotent: a second call is a no-op so module-level imports that
    install on first use don't double-register exporters.

    ``service_name`` lands as ``service.name`` on every span and is
    how Langfuse / SigNoz / Jaeger group traces in their UI. Pass
    ``"rolemesh-orchestrator"`` from the orchestrator process and
    ``"rolemesh-agent"`` from inside the container.

    Extra ``resource_attrs`` (e.g. ``tenant_id="..."``,
    ``coworker_id="..."``, ``job_id="..."``) become ``Resource``
    attributes — span-independent metadata visible on every span this
    process emits. Useful for filtering in the UI.
    """
    global _installed
    if _installed:
        return

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        # Stay quiet — this is the default when observability is off.
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        # Extra not installed; fall back to noop tracer transparently.
        # We log once so operators who set OTEL_EXPORTER_OTLP_ENDPOINT
        # but forgot the extra get a clear hint.
        logger.warning(
            "OTEL_EXPORTER_OTLP_ENDPOINT is set but opentelemetry-sdk "
            "is not installed. Install the [observability] extra to "
            "enable tracing.",
            endpoint=endpoint,
        )
        return

    resource = Resource.create({"service.name": service_name, **resource_attrs})
    provider = TracerProvider(resource=resource)
    # OTLP HTTP — Langfuse accepts ``/api/public/otel`` for free OSS.
    # Many other backends (SigNoz, Jaeger, Tempo, Honeycomb) accept
    # the same endpoint shape.
    exporter = OTLPSpanExporter(endpoint=endpoint)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _installed = True
    logger.info(
        "OTel tracer installed",
        service_name=service_name,
        endpoint=endpoint,
    )


def shutdown_tracer() -> None:
    """Force-flush the tracer provider so buffered spans reach the
    backend before the process exits.

    Critical for short-lived agent containers: the default
    ``BatchSpanProcessor`` schedule_delay is 5 seconds, so a turn that
    finishes in under 5s exits without flushing and every span it
    emitted is dropped silently. Call this once on the way out of
    ``run_query_loop``. Safe to call when ``install_tracer`` was
    skipped — degrades to a no-op.
    """
    if not _installed:
        return
    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        # ``shutdown`` exists on the SDK's TracerProvider but not on
        # the ProxyTracerProvider that's installed when the SDK is
        # missing. ``getattr`` keeps this branch safe for both.
        shutdown = getattr(provider, "shutdown", None)
        if shutdown is not None:
            shutdown()
    except ImportError:
        pass


def get_tracer(name: str) -> Tracer:
    """Return a tracer; works whether ``install_tracer`` ran or not.

    When OTel is missing, returns a noop tracer obtained from a tiny
    inline shim (so importing this module has no hard dep). When OTel
    is present but ``install_tracer`` was never called, returns a
    tracer backed by the default ProxyTracerProvider, which is also
    noop until a real provider is set.
    """
    try:
        from opentelemetry import trace as _trace

        return _trace.get_tracer(name)
    except ImportError:
        # Mypy sees this as a type mismatch only when opentelemetry
        # IS installed (in which case ``_NoopTracer`` is structurally
        # compatible with ``Tracer`` for the methods we use). The
        # ignore is therefore conditionally redundant — silence the
        # ``warn_unused_ignores`` lint with ``unused-ignore``.
        return _NoopTracer()  # type: ignore[return-value,unused-ignore]


def inject_trace_context() -> dict[str, str]:
    """Serialize the current span context into a W3C carrier dict.

    Returns ``{}`` when there's no active span or OTel isn't
    installed. The result is JSON-safe and goes through the existing
    ``AgentInitData`` serialisation untouched.

    Pair with ``extract_trace_context`` on the receiving side. We use
    OTel's stock TraceContextTextMapPropagator so this works
    interchangeably with any OTel-aware system.
    """
    try:
        from opentelemetry.propagate import inject

        carrier: dict[str, str] = {}
        inject(carrier)
        return carrier
    except ImportError:
        return {}


def extract_trace_context(carrier: dict[str, str] | None) -> Any:
    """Hydrate a parent ``Context`` from a carrier dict.

    Returns ``None`` when the carrier is empty or OTel isn't
    installed. Pass the returned object as ``context=`` on the next
    ``tracer.start_as_current_span(name, context=ctx)`` call so the
    new span attaches as a child of the upstream span.
    """
    if not carrier:
        return None
    try:
        from opentelemetry.propagate import extract

        return extract(carrier)
    except ImportError:
        return None


def attach_parent_context(carrier: dict[str, str] | None) -> Any:
    """Make ``carrier``'s span context the active OTel context.

    Convenience for processes (the agent container) that want every
    subsequently-opened span — explicit ``start_as_current_span`` and
    auto-instrumentation alike — to inherit the upstream parent
    without threading a ``context=`` kwarg through every call site.

    Returns an opaque token; pass it to ``detach_parent_context`` on
    cleanup. Returns ``None`` when the carrier is empty or OTel is
    missing, in which case ``detach_parent_context`` is a no-op.

    Note: OTel context lives in ``contextvars``, which are async-task
    local. Attaching from the main task before spawning subtasks
    propagates correctly because asyncio copies the context on task
    creation.
    """
    parent = extract_trace_context(carrier)
    if parent is None:
        return None
    try:
        from opentelemetry import context as ctx_api

        return ctx_api.attach(parent)
    except ImportError:
        return None


def detach_parent_context(token: Any) -> None:
    """Reverse ``attach_parent_context``. Safe to call with ``None``."""
    if token is None:
        return
    try:
        from opentelemetry import context as ctx_api

        ctx_api.detach(token)
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# Noop tracer fallback used only when ``opentelemetry`` is not importable
# at all. The SDK ships its own noop, but we don't want to require the SDK
# import just to get a tracer object — keeps the dependency strictly opt-in.
# ---------------------------------------------------------------------------


class _NoopSpan:
    def __enter__(self) -> _NoopSpan:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def set_attribute(self, key: str, value: object) -> None:
        return None

    def set_status(self, *args: object, **kwargs: object) -> None:
        return None

    def record_exception(self, *args: object, **kwargs: object) -> None:
        return None

    def end(self) -> None:
        return None


class _NoopTracer:
    def start_as_current_span(self, name: str, **kwargs: object) -> _NoopSpan:
        return _NoopSpan()

    def start_span(self, name: str, **kwargs: object) -> _NoopSpan:
        return _NoopSpan()
