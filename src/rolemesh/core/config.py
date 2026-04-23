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

# Runtime-abstraction backend selector: "docker" | "k8s" (not OCI runtime).
# Pairs with CONTAINER_OCI_RUNTIME below: BACKEND picks "which Python client
# talks to which orchestrator", OCI_RUNTIME picks "which binary actually
# runs the container process".
CONTAINER_BACKEND: str = os.environ.get("CONTAINER_BACKEND", "docker")

# OCI runtime selection (R1). "runc" is the default; "runsc" enables gVisor
# syscall-level sandboxing and requires runsc to be registered in
# /etc/docker/daemon.json on the host. Per-coworker overrides live in
# ContainerConfig.runtime.
#
# Named OCI to disambiguate from CONTAINER_BACKEND (docker vs k8s). A
# shorter name like CONTAINER_RUNTIME would collide with the old meaning
# of that variable (runtime-abstraction selector) and confuse anyone who
# saw both in an env file.
CONTAINER_OCI_RUNTIME: str = os.environ.get("CONTAINER_OCI_RUNTIME", "runc")

# Per-container resource ceilings (R7). Overrides come from ContainerConfig
# on each coworker and are clamped to CONTAINER_MAX_* in runner.build_container_spec.
CONTAINER_MEMORY_LIMIT: str = os.environ.get("CONTAINER_MEMORY_LIMIT", "2g")
CONTAINER_CPU_LIMIT: float = float(os.environ.get("CONTAINER_CPU_LIMIT", "2.0"))
CONTAINER_PIDS_LIMIT: int = int(os.environ.get("CONTAINER_PIDS_LIMIT", "512"))
CONTAINER_MAX_MEMORY: str = os.environ.get("CONTAINER_MAX_MEMORY", "8g")
CONTAINER_MAX_CPU: float = float(os.environ.get("CONTAINER_MAX_CPU", "4.0"))

# Custom bridge network for agent containers (R5). Setting this to the
# empty string falls back to Docker's default bridge (loses ICC isolation
# and metadata-blackhole scope; use only when custom networks are
# unsupported on the host).
CONTAINER_NETWORK_NAME: str = os.environ.get("CONTAINER_NETWORK_NAME", "rolemesh-agent-net")

# Allowlist for env vars that the orchestrator dynamically injects into
# containers (R8). Anything produced by build_container_spec() or passed
# via AgentBackendConfig.extra_env must be in this set; unknown keys are
# dropped with a structured warning.
#
# The following are intentionally NOT in the allowlist:
#   PATH             — Docker does not inherit parent PATH; the container's
#                      PATH is set by the image's ENV layer and must stay
#                      fixed there.
#   LANG / LC_ALL    — Container locale must not track host locale; pinned
#                      to C.UTF-8 in the Dockerfile.
#   PYTHONUNBUFFERED — Image property, not a per-tenant knob; set to 1 in
#                      the Dockerfile.
#   HTTP_PROXY etc.  — Reserved for the future egress proxy task. Until
#                      that lands we deliberately do not forward arbitrary
#                      proxy env; that task will extend the allowlist.
CONTAINER_ENV_ALLOWLIST: frozenset[str] = frozenset({
    "TZ",
    "NATS_URL",
    "JOB_ID",
    "AGENT_BACKEND",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    # Redirects Claude Code CLI's `.claude.json` from $HOME/.claude.json to
    # $CLAUDE_CONFIG_DIR/.claude.json. Required under ReadonlyRootfs=True
    # because /home/agent/ itself is read-only; without this env the CLI
    # tries to write /home/agent/.claude.json, hits EROFS, and agent
    # initialization times out.
    # Value is injected in runner.build_container_spec pointing at the
    # per-coworker bind mount /home/agent/.claude, which also makes the
    # config file naturally persist (scoped per coworker) across container
    # spawns.
    "CLAUDE_CONFIG_DIR",
    "HOME",
    "PI_MODEL_ID",
})

# Agent backend: "claude" or "pi"
AGENT_BACKEND_DEFAULT: str = os.environ.get("ROLEMESH_AGENT_BACKEND", "claude")

# Auth configuration
AUTH_MODE: str = os.environ.get("AUTH_MODE", "external")
ROLEMESH_TOKEN_SECRET: str = os.environ.get("ROLEMESH_TOKEN_SECRET", "")

# Approval module: what to do when the orchestrator cannot read policies
# from the DB at container-start time (network blip, degraded replica, …).
#   "closed" — refuse to start the agent. Safe default: a policy outage
#              must not silently promote every call to unsupervised.
#   "open"   — start without any approval policies loaded. Legacy
#              behaviour; acceptable when the tenant accepts "no
#              approval better than no agent" for availability reasons.
APPROVAL_FAIL_MODE: str = os.environ.get("APPROVAL_FAIL_MODE", "closed")

# SAFETY_FAIL_MODE mirrors APPROVAL_FAIL_MODE for the Safety Framework:
# on DB unreachable at container start, "closed" (default) refuses the
# job; "open" runs the agent without safety rules and logs ERROR. The
# safety hook itself is already fail-closed at runtime (check
# exceptions propagate to a block) — this flag only governs the
# boot-time rule snapshot load.
SAFETY_FAIL_MODE: str = os.environ.get("SAFETY_FAIL_MODE", "closed")

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
