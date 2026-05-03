"""Lazy OpenTelemetry tracer + W3C trace-context propagation.

Every public helper in this module short-circuits to a noop when
either (a) the ``opentelemetry`` SDK is not installed, or (b) the
``OTEL_EXPORTER_OTLP_ENDPOINT`` env var is not set. Callers don't
need to guard their own code paths — they always get a usable
object back.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from rolemesh.core.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = get_logger()

# Module-level handle to the installed TracerProvider. ``None`` means
# noop mode (either SDK absent or endpoint unset). ``shutdown_tracer``
# resets this so install_tracer() can be called again — useful for
# tests that exercise the install / flush cycle.
_provider: Any = None
_installed: bool = False

try:
    from opentelemetry import trace as _otel_trace  # noqa: F401

    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False


# ---------------------------------------------------------------------------
# Noop fallback (returned when SDK is not installed at all).
#
# When the SDK *is* installed but no provider has been registered yet,
# OTel's own ProxyTracer + NoOpSpan handle the no-op behaviour, so we
# don't need our shim. The shim only matters when the import itself
# would fail.
# ---------------------------------------------------------------------------


class _NoopSpan:
    def set_attribute(self, key: str, value: object) -> None:
        pass

    def set_attributes(self, attributes: dict[str, object]) -> None:
        pass

    def set_status(self, *args: object, **kwargs: object) -> None:
        pass

    def record_exception(self, *args: object, **kwargs: object) -> None:
        pass

    def add_event(self, *args: object, **kwargs: object) -> None:
        pass

    def update_name(self, name: str) -> None:
        pass

    def end(self, *args: object, **kwargs: object) -> None:
        pass

    def is_recording(self) -> bool:
        return False

    def get_span_context(self) -> Any:
        return None

    def __enter__(self) -> _NoopSpan:
        return self

    def __exit__(self, *args: object) -> None:
        pass


class _NoopTracer:
    def start_span(self, name: str, *args: object, **kwargs: object) -> _NoopSpan:
        return _NoopSpan()

    @contextmanager
    def start_as_current_span(
        self, name: str, *args: object, **kwargs: object
    ) -> Iterator[_NoopSpan]:
        yield _NoopSpan()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_installed() -> bool:
    """True iff a real OTel TracerProvider is currently configured.

    Useful for tests that need to verify the gating logic, and for
    skip-if-not-installed branches in callers that emit spans manually
    (e.g. the Claude backend's claude.message span — see FR-6).
    """
    return _installed


def install_tracer(service_name: str, **resource_attrs: str) -> None:
    """Configure the global OTel TracerProvider. Idempotent.

    Returns silently in noop mode (SDK missing or endpoint unset) so
    callers can invoke unconditionally at startup.
    """
    global _provider, _installed
    if _installed:
        return
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return
    if not _OTEL_AVAILABLE:
        logger.warning(
            "OTEL_EXPORTER_OTLP_ENDPOINT is set but opentelemetry SDK is "
            "not installed; tracer disabled. Install with "
            "`uv sync --extra observability`."
        )
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
        logger.warning(
            "opentelemetry SDK partially installed; tracer disabled"
        )
        return
    resource_attributes: dict[str, Any] = {"service.name": service_name}
    resource_attributes.update(resource_attrs)
    resource = Resource.create(resource_attributes)
    provider = TracerProvider(resource=resource)
    # Endpoint + headers are read from env by the exporter itself
    # (OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_EXPORTER_OTLP_HEADERS),
    # which keeps this module ignorant of backend-specific URLs.
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _provider = provider
    _installed = True
    logger.info(
        "OTel tracer installed",
        service=service_name,
        endpoint=endpoint,
    )


def get_tracer(name: str) -> Any:
    """Return a Tracer-like object. Always usable.

    With the SDK installed we return OTel's real (Proxy)Tracer, which
    no-ops cleanly even before ``install_tracer`` runs because OTel's
    default global provider is itself a NoOpTracerProvider. Without
    the SDK we hand back a tiny shim with the same interface.
    """
    if _OTEL_AVAILABLE:
        from opentelemetry import trace

        return trace.get_tracer(name)
    return _NoopTracer()


def shutdown_tracer() -> None:
    """Force-flush and shut down the global TracerProvider.

    Call from an ``atexit`` handler or a ``finally`` block before the
    process exits — BatchSpanProcessor buffers spans for up to a few
    seconds and would otherwise drop the tail. Idempotent and noop-safe.
    """
    global _provider, _installed
    if _provider is not None:
        try:
            _provider.shutdown()
        except Exception:
            logger.exception(
                "OTel TracerProvider shutdown raised; tail spans may be lost"
            )
    _provider = None
    _installed = False


# ---------------------------------------------------------------------------
# W3C trace-context propagation across the orchestrator → container hop.
# Both sides go through the global propagator so the wire format stays
# in lockstep with whatever OTel ships as default (currently W3C
# TraceContext + Baggage).
# ---------------------------------------------------------------------------


def inject_trace_context() -> dict[str, str]:
    """Build a carrier dict from the currently-active span.

    Returns ``{}`` when (a) the SDK isn't installed, or (b) there is no
    active span. The receiver side must treat an empty/None carrier as
    'no parent' and start an independent root span — see
    ``attach_parent_context``.
    """
    if not _OTEL_AVAILABLE:
        return {}
    from opentelemetry import propagate

    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


def extract_trace_context(carrier: dict[str, str] | None) -> Any:
    """Parse a carrier into an OTel ``Context``. ``None`` if empty/absent.

    The returned object is opaque to callers — feed it to
    ``attach_parent_context`` (or to ``opentelemetry.context.attach``
    directly).
    """
    if not _OTEL_AVAILABLE or not carrier:
        return None
    from opentelemetry import propagate

    return propagate.extract(carrier)


def attach_parent_context(carrier: dict[str, str] | None) -> None:
    """Make a remote parent span the parent of subsequent local spans.

    The attach is to Python's contextvars, so it sticks for the
    current async-task subtree (and any Tasks spawned afterwards,
    since ``asyncio.create_task`` snapshots contextvars at creation
    time). We deliberately don't track the detach token: containers
    keep this parent for the rest of their lifetime.

    No-ops on noop tracer or empty/None carrier.
    """
    if not _OTEL_AVAILABLE or not carrier:
        return
    from opentelemetry import context as otel_context

    ctx = extract_trace_context(carrier)
    if ctx is None:
        return
    otel_context.attach(ctx)
