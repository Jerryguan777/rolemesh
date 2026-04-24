"""Environment file (.env) parser.

Reads specific keys from .env OR from ``os.environ`` as a fallback.
Prefers ``.env`` so host-side tooling that stores secrets outside the
process environment wins; falls back to ``os.environ`` so containerized
deployments using ``docker --env-file`` / K8s secrets injection still
work without an in-image ``.env`` file.

Pre-EC-2 this function read ONLY ``.env`` — that silently broke the
egress gateway's reverse-proxy provider registry any time an operator
mounted secrets via env vars instead of a bind-mounted ``.env``.
"""

from __future__ import annotations

import os
from pathlib import Path

from rolemesh.core.logger import get_logger

logger = get_logger()


def read_env_file(keys: list[str], env_path: Path | None = None) -> dict[str, str]:
    """Parse the .env file (if present) and fall back to os.environ.

    Priority: .env > os.environ. This matches operator intent — a
    .env placed next to the process is a deliberate override of
    whatever the ambient environment has.

    Returns only keys with a non-empty value. Keys absent in BOTH
    sources are omitted from the result dict.
    """
    if env_path is None:
        env_path = Path.cwd() / ".env"

    file_values: dict[str, str] = {}
    try:
        content = env_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.debug(".env file not found, will use os.environ", path=str(env_path))
    else:
        wanted = set(keys)
        for line in content.splitlines():
            trimmed = line.strip()
            if not trimmed or trimmed.startswith("#"):
                continue
            eq_idx = trimmed.find("=")
            if eq_idx == -1:
                continue
            key = trimmed[:eq_idx].strip()
            if key not in wanted:
                continue
            value = trimmed[eq_idx + 1 :].strip()
            if len(value) >= 2 and (
                (value.startswith('"') and value.endswith('"'))
                or (value.startswith("'") and value.endswith("'"))
            ):
                value = value[1:-1]
            if value:
                file_values[key] = value

    # Layer os.environ under file_values — file wins where both define
    # the same key. Keys with empty-string values in os.environ are
    # treated the same as missing, matching the "non-empty only" rule
    # we apply to the file path.
    result: dict[str, str] = {}
    for key in keys:
        if key in file_values:
            result[key] = file_values[key]
            continue
        env_value = os.environ.get(key, "")
        if env_value:
            result[key] = env_value
    return result
