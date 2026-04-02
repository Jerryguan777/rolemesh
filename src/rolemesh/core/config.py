"""Configuration constants and paths."""

from __future__ import annotations

import os
from pathlib import Path

from rolemesh.core.env import read_env_file

_env_config = read_env_file(["ASSISTANT_NAME", "ASSISTANT_HAS_OWN_NUMBER"])

# Legacy: ASSISTANT_NAME is no longer the global trigger source.
# Kept for backward compatibility; new code should use coworker.name.
ASSISTANT_NAME: str = os.environ.get("ASSISTANT_NAME") or _env_config.get("ASSISTANT_NAME", "Andy")
ASSISTANT_HAS_OWN_NUMBER: bool = (
    os.environ.get("ASSISTANT_HAS_OWN_NUMBER") or _env_config.get("ASSISTANT_HAS_OWN_NUMBER", "")
) == "true"

POLL_INTERVAL: float = 2.0  # seconds
SCHEDULER_POLL_INTERVAL: float = 60.0  # seconds

PROJECT_ROOT: Path = Path.cwd()
HOME_DIR: Path = Path.home()

MOUNT_ALLOWLIST_PATH: Path = HOME_DIR / ".config" / "rolemesh" / "mount-allowlist.json"
SENDER_ALLOWLIST_PATH: Path = HOME_DIR / ".config" / "rolemesh" / "sender-allowlist.json"
STORE_DIR: Path = PROJECT_ROOT / "store"
GROUPS_DIR: Path = PROJECT_ROOT / "groups"
DATA_DIR: Path = PROJECT_ROOT / "data"

DATABASE_URL: str = os.environ.get("DATABASE_URL", "postgresql://rolemesh:rolemesh@localhost:5432/rolemesh")
NATS_URL: str = os.environ.get("NATS_URL", "nats://localhost:4222")

CONTAINER_IMAGE: str = os.environ.get("CONTAINER_IMAGE", "rolemesh-agent:latest")
CONTAINER_TIMEOUT: int = int(os.environ.get("CONTAINER_TIMEOUT", "1800000"))
CONTAINER_MAX_OUTPUT_SIZE: int = int(os.environ.get("CONTAINER_MAX_OUTPUT_SIZE", "10485760"))  # 10MB
CREDENTIAL_PROXY_PORT: int = int(os.environ.get("CREDENTIAL_PROXY_PORT", "3001"))
IDLE_TIMEOUT: int = int(os.environ.get("IDLE_TIMEOUT", "1800000"))  # 30 min
MCP_PROXY_PREFIX: str = "mcp-proxy"
MAX_CONCURRENT_CONTAINERS: int = max(1, int(os.environ.get("MAX_CONCURRENT_CONTAINERS", "5")))
GLOBAL_MAX_CONTAINERS: int = max(1, int(os.environ.get("GLOBAL_MAX_CONTAINERS", "20")))
CONTAINER_RUNTIME: str = os.environ.get("CONTAINER_RUNTIME", "docker")

# Timezone for scheduled tasks — needs IANA name (e.g. "America/New_York"), not abbreviation ("EST").
TIMEZONE: str = os.environ.get("TZ", "")

if not TIMEZONE or "/" not in TIMEZONE:
    # Try /etc/timezone (Debian/Ubuntu)
    try:
        _tz = Path("/etc/timezone").read_text().strip()
        if "/" in _tz:
            TIMEZONE = _tz
    except OSError:
        pass

if not TIMEZONE or "/" not in TIMEZONE:
    # Try /etc/localtime symlink (most Linux distros + macOS)
    try:
        _link = os.readlink("/etc/localtime")
        _tz = _link.split("zoneinfo/")[-1]
        if "/" in _tz:
            TIMEZONE = _tz
    except OSError:
        pass

if not TIMEZONE or "/" not in TIMEZONE:
    TIMEZONE = "UTC"
