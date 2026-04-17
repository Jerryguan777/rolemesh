"""Configuration constants and path helpers for the coding agent.

Python port of packages/coding-agent/src/config.ts.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

# ===========================================================================
# App Config (equivalent to package.json piConfig)
# ===========================================================================

APP_NAME: str = "pi"
CONFIG_DIR_NAME: str = ".pi"
try:
    from pi._version import version as VERSION
except ImportError:
    VERSION: str = "0.0.0+unknown"

# Environment variable name for overriding the agent directory
# e.g. PI_CODING_AGENT_DIR
ENV_AGENT_DIR: str = f"{APP_NAME.upper()}_CODING_AGENT_DIR"

DEFAULT_SHARE_VIEWER_URL: str = "https://pi.dev/session/"

# Python is never running as a Bun binary or Bun runtime
IS_BUN_BINARY: bool = False
IS_BUN_RUNTIME: bool = False

# snake_case aliases for parity with TS camelCase exports
is_bun_binary = IS_BUN_BINARY
is_bun_runtime = IS_BUN_RUNTIME

# ===========================================================================
# Install Method Detection
# ===========================================================================

InstallMethod = Literal["pip", "unknown"]


def _resolve_env_path(env_val: str) -> Path:
    """Resolve an environment-variable path string to a Path, expanding tilde."""
    if env_val == "~":
        return Path.home()
    if env_val.startswith("~/"):
        return Path.home() / env_val[2:]
    return Path(env_val)


def detect_install_method() -> str:
    """Detect how the package was installed.

    Python doesn't have bun/npm; always returns "pip".
    """
    return "pip"


def get_update_instruction(package_name: str) -> str:
    """Return the command a user should run to update the package."""
    return f"Run: pip install --upgrade {package_name}"


# ===========================================================================
# Package Asset Paths (shipped with the Python package)
# ===========================================================================


def get_package_dir() -> Path:
    """Return the base directory for package assets.

    Respects the PI_PACKAGE_DIR environment variable override.
    Falls back to the pi.coding_agent package directory.
    """
    env_dir = os.environ.get("PI_PACKAGE_DIR")
    if env_dir:
        return _resolve_env_path(env_dir)
    # The pi.coding_agent package lives at __file__'s parent directory.
    return Path(__file__).parent


def get_themes_dir() -> Path:
    """Return path to built-in themes directory (data/ inside the package)."""
    return get_package_dir() / "data"


def get_export_template_dir() -> Path:
    """Return path to HTML export template directory."""
    return get_package_dir() / "data" / "export-html"


def get_package_json_path() -> Path:
    """Return path to package.json (or pyproject.toml in Python)."""
    return get_package_dir() / "pyproject.toml"


def get_readme_path() -> Path:
    """Return path to README.md."""
    return (get_package_dir() / "README.md").resolve()


def get_docs_path() -> Path:
    """Return path to docs directory."""
    return (get_package_dir() / "docs").resolve()


def get_examples_path() -> Path:
    """Return path to examples directory."""
    return (get_package_dir() / "examples").resolve()


def get_changelog_path() -> Path:
    """Return path to CHANGELOG.md."""
    return (get_package_dir() / "CHANGELOG.md").resolve()


# ===========================================================================
# Share URL
# ===========================================================================


def get_share_viewer_url(gist_id: str) -> str:
    """Return the share viewer URL for a given gist ID."""
    base_url = os.environ.get("PI_SHARE_VIEWER_URL", DEFAULT_SHARE_VIEWER_URL)
    return f"{base_url}#{gist_id}"


# ===========================================================================
# User Config Paths (~/.pi/agent/*)
# ===========================================================================


def get_agent_dir() -> Path:
    """Return the agent config directory (e.g. ~/.pi/agent/).

    Respects the PI_CODING_AGENT_DIR environment variable.
    """
    env_dir = os.environ.get(ENV_AGENT_DIR)
    if env_dir:
        return _resolve_env_path(env_dir)
    return Path.home() / CONFIG_DIR_NAME / "agent"


def get_custom_themes_dir() -> Path:
    """Return path to the user's custom themes directory."""
    return get_agent_dir() / "themes"


def get_models_path() -> Path:
    """Return path to models.json."""
    return get_agent_dir() / "models.json"


def get_auth_path() -> Path:
    """Return path to auth.json."""
    return get_agent_dir() / "auth.json"


def get_settings_path() -> Path:
    """Return path to settings.json."""
    return get_agent_dir() / "settings.json"


def get_tools_dir() -> Path:
    """Return path to the tools directory."""
    return get_agent_dir() / "tools"


def get_bin_dir() -> Path:
    """Return path to managed binaries directory (fd, rg)."""
    return get_agent_dir() / "bin"


def get_prompts_dir() -> Path:
    """Return path to prompt templates directory."""
    return get_agent_dir() / "prompts"


def get_sessions_dir() -> Path:
    """Return path to sessions directory."""
    return get_agent_dir() / "sessions"


def get_debug_log_path() -> Path:
    """Return path to the debug log file."""
    return get_agent_dir() / f"{APP_NAME}-debug.log"
