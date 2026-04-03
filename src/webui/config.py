"""Configuration for the WebUI FastAPI service."""

from __future__ import annotations

import os
from pathlib import Path

NATS_URL: str = os.environ.get("NATS_URL", "nats://localhost:4222")
DATABASE_URL: str = os.environ.get(
    "DATABASE_URL",
    "postgresql://rolemesh:rolemesh@localhost:5432/rolemesh",
)
WEB_UI_PORT: int = int(os.environ.get("WEB_UI_PORT", "8080"))
WEB_UI_HOST: str = os.environ.get("WEB_UI_HOST", "0.0.0.0")
WEB_UI_DIST: Path = Path(os.environ.get("WEB_UI_DIST", "web/dist"))
ADMIN_BOOTSTRAP_TOKEN: str = os.environ.get("ADMIN_BOOTSTRAP_TOKEN", "")
