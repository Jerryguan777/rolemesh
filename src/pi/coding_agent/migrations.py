"""One-time migrations that run on startup.

Python port of packages/coding-agent/src/migrations.ts.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from pi.coding_agent.config import CONFIG_DIR_NAME, get_agent_dir, get_bin_dir

EXTENSIONS_DOC_URL = "https://github.com/Jerryguan777/ppi/blob/main/docs/coding-agent/docs/extensions.md"


@dataclass
class MigrationResult:
    """Result from running all migrations."""

    migrated_auth_providers: list[str] = field(default_factory=list)
    deprecation_warnings: list[str] = field(default_factory=list)


def migrate_auth_to_auth_json() -> list[str]:
    """Migrate legacy oauth.json and settings.json apiKeys to auth.json.

    Returns a list of provider names that were migrated.
    """
    agent_dir = get_agent_dir()
    auth_path = agent_dir / "auth.json"
    oauth_path = agent_dir / "oauth.json"
    settings_path = agent_dir / "settings.json"

    # Skip if auth.json already exists
    if auth_path.exists():
        return []

    migrated: dict[str, object] = {}
    providers: list[str] = []

    # Migrate oauth.json
    if oauth_path.exists():
        try:
            oauth: dict[str, object] = json.loads(oauth_path.read_text(encoding="utf-8"))
            for provider, cred in oauth.items():
                if isinstance(cred, dict):
                    migrated[provider] = {"type": "oauth", **cred}
                else:
                    migrated[provider] = {"type": "oauth"}
                providers.append(provider)
            oauth_path.rename(Path(str(oauth_path) + ".migrated"))
        except Exception:
            pass  # Skip on error

    # Migrate settings.json apiKeys
    if settings_path.exists():
        try:
            content = settings_path.read_text(encoding="utf-8")
            settings: dict[str, object] = json.loads(content)
            api_keys = settings.get("apiKeys")
            if isinstance(api_keys, dict):
                for provider, key in api_keys.items():
                    if provider not in migrated and isinstance(key, str):
                        migrated[provider] = {"type": "api_key", "key": key}
                        providers.append(provider)
                del settings["apiKeys"]
                settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        except Exception:
            pass  # Skip on error

    if migrated:
        auth_path.parent.mkdir(parents=True, exist_ok=True)
        auth_path.write_text(json.dumps(migrated, indent=2), encoding="utf-8")
        auth_path.chmod(0o600)

    return providers


def migrate_sessions_from_agent_root() -> None:
    """Migrate sessions from ~/.pi/agent/*.jsonl to proper session directories.

    Bug in v0.30.0: Sessions were saved to ~/.pi/agent/ instead of
    ~/.pi/agent/sessions/<encoded-cwd>/. This migration moves them
    to the correct location based on the cwd in their session header.
    """
    agent_dir = get_agent_dir()

    # Find all .jsonl files directly in agentDir (not in subdirectories)
    try:
        files = [f for f in agent_dir.iterdir() if f.is_file() and f.suffix == ".jsonl"]
    except OSError:
        return

    if not files:
        return

    for file in files:
        try:
            content = file.read_text(encoding="utf-8")
            first_line = content.split("\n")[0]
            if not first_line.strip():
                continue

            header: dict[str, object] = json.loads(first_line)
            if header.get("type") != "session" or not header.get("cwd"):
                continue

            cwd = str(header["cwd"])

            # Compute the correct session directory (same encoding as session-manager.ts)
            safe_path = "--" + cwd.lstrip("/\\").replace("/", "-").replace("\\", "-").replace(":", "-") + "--"
            correct_dir = agent_dir / "sessions" / safe_path

            if not correct_dir.exists():
                correct_dir.mkdir(parents=True, exist_ok=True)

            new_path = correct_dir / file.name
            if new_path.exists():
                continue  # Skip if target exists

            file.rename(new_path)
        except Exception:
            pass  # Skip files that can't be migrated


def _migrate_commands_to_prompts(base_dir: Path, label: str) -> bool:
    """Migrate commands/ to prompts/ directory if needed."""
    commands_dir = base_dir / "commands"
    prompts_dir = base_dir / "prompts"

    if commands_dir.exists() and not prompts_dir.exists():
        try:
            commands_dir.rename(prompts_dir)
            print(f"Migrated {label} commands/ -> prompts/")
            return True
        except OSError as err:
            print(
                f"Warning: Could not migrate {label} commands/ to prompts/: {err}",
                file=sys.stderr,
            )
    return False


def _migrate_tools_to_bin() -> None:
    """Move fd/rg binaries from tools/ to bin/ if they exist."""
    agent_dir = get_agent_dir()
    tools_dir = agent_dir / "tools"
    bin_dir = get_bin_dir()

    if not tools_dir.exists():
        return

    binaries = ["fd", "rg", "fd.exe", "rg.exe"]
    moved_any = False

    for bin_name in binaries:
        old_path = tools_dir / bin_name
        new_path = bin_dir / bin_name

        if old_path.exists():
            if not bin_dir.exists():
                bin_dir.mkdir(parents=True, exist_ok=True)
            if not new_path.exists():
                try:
                    old_path.rename(new_path)
                    moved_any = True
                except OSError:
                    pass  # Ignore errors
            else:
                # Target exists, just delete the old one
                with contextlib.suppress(OSError):
                    old_path.unlink()

    if moved_any:
        print("Migrated managed binaries tools/ -> bin/")


def _check_deprecated_extension_dirs(base_dir: Path, label: str) -> list[str]:
    """Check for deprecated hooks/ and tools/ directories."""
    hooks_dir = base_dir / "hooks"
    tools_dir = base_dir / "tools"
    warnings: list[str] = []

    if hooks_dir.exists():
        warnings.append(f"{label} hooks/ directory found. Hooks have been renamed to extensions.")

    if tools_dir.exists():
        # Check if tools/ contains anything other than fd/rg
        try:
            entries = list(tools_dir.iterdir())
            managed = {"fd", "rg", "fd.exe", "rg.exe"}
            custom_tools = [e for e in entries if e.name.lower() not in managed and not e.name.startswith(".")]
            if custom_tools:
                warnings.append(
                    f"{label} tools/ directory contains custom tools. Custom tools have been merged into extensions."
                )
        except OSError:
            pass  # Ignore read errors

    return warnings


def _migrate_extension_system(cwd: str) -> list[str]:
    """Run extension system migrations and collect deprecation warnings."""
    agent_dir = get_agent_dir()
    project_dir = Path(cwd) / CONFIG_DIR_NAME

    # Migrate commands/ to prompts/
    _migrate_commands_to_prompts(agent_dir, "Global")
    _migrate_commands_to_prompts(project_dir, "Project")

    # Check for deprecated directories
    warnings = [
        *_check_deprecated_extension_dirs(agent_dir, "Global"),
        *_check_deprecated_extension_dirs(project_dir, "Project"),
    ]

    return warnings


async def show_deprecation_warnings(warnings: list[str]) -> None:
    """Print deprecation warnings and wait for a keypress.

    On POSIX systems, switches stdin to raw mode to capture a single keypress.
    On Windows or non-TTY environments, falls back to a regular Enter prompt.
    """
    if not warnings:
        return

    for warning in warnings:
        print(f"Warning: {warning}")
    print("\nMove your extensions to the extensions/ directory.")
    print(f"Documentation: {EXTENSIONS_DOC_URL}")
    print("\nPress any key to continue...")

    try:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    except (ImportError, OSError):
        # Windows or non-TTY: fall back to a regular Enter prompt
        with contextlib.suppress(EOFError, KeyboardInterrupt):
            input()

    print()


def run_migrations(cwd: str = "") -> MigrationResult:
    """Run all migrations. Called once on startup.

    Returns a MigrationResult with migration results and deprecation warnings.
    """
    if not cwd:
        cwd = os.getcwd()

    migrated_auth_providers = migrate_auth_to_auth_json()
    migrate_sessions_from_agent_root()
    _migrate_tools_to_bin()
    deprecation_warnings = _migrate_extension_system(cwd)
    return MigrationResult(
        migrated_auth_providers=migrated_auth_providers,
        deprecation_warnings=deprecation_warnings,
    )
