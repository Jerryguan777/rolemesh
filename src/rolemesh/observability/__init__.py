"""OpenTelemetry tracing facade for RoleMesh.

Two-layer opt-in: the ``[observability]`` extra must be installed
*and* ``OTEL_EXPORTER_OTLP_ENDPOINT`` (or ``_AGENT`` for the
container side) must be set. Either gate failing leaves every
helper here in noop mode so a stock rolemesh run is byte-for-byte
unchanged.

See ``docs/observability/setup.md`` for the end-to-end walkthrough.
"""

from rolemesh.observability.tracer import (
    attach_parent_context,
    extract_trace_context,
    get_tracer,
    inject_trace_context,
    install_tracer,
    is_installed,
    shutdown_tracer,
)

__all__ = [
    "attach_parent_context",
    "extract_trace_context",
    "get_tracer",
    "inject_trace_context",
    "install_tracer",
    "is_installed",
    "shutdown_tracer",
]
