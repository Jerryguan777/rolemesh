"""Extension loader - loads Python extension modules.

Port of packages/coding-agent/src/core/extensions/loader.ts.

Uses importlib instead of jiti. Extensions are Python files (not TS).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

from pi.coding_agent.core.config import get_agent_dir
from pi.coding_agent.core.extensions.types import (
    Extension,
    ExtensionAPI,
    ExtensionFactory,
    ExtensionRuntime,
    LoadExtensionsResult,
)

# ============================================================================
# Helpers
# ============================================================================

_UNICODE_SPACES = "\u00a0\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000"


def _normalize_unicode_spaces(s: str) -> str:
    for char in _UNICODE_SPACES:
        s = s.replace(char, " ")
    return s


def _expand_path(p: str) -> str:
    normalized = _normalize_unicode_spaces(p)
    if normalized.startswith("~/"):
        return str(Path.home() / normalized[2:])
    if normalized.startswith("~"):
        return str(Path.home() / normalized[1:])
    return normalized


def _resolve_path(ext_path: str, cwd: str) -> str:
    expanded = _expand_path(ext_path)
    if os.path.isabs(expanded):
        return expanded
    return str(Path(cwd) / expanded)


def _is_extension_file(name: str) -> bool:
    return name.endswith(".py")


# ============================================================================
# Runtime creation
# ============================================================================


def create_extension_runtime() -> ExtensionRuntime:
    """Create a runtime with throwing stubs for action methods.

    Runner.bind_core() replaces these with real implementations.
    """
    return ExtensionRuntime()


# ============================================================================
# Extension creation
# ============================================================================


def create_extension(extension_path: str, resolved_path: str) -> Extension:
    """Create an Extension object with empty collections."""
    return Extension(
        path=extension_path,
        resolved_path=resolved_path,
        handlers={},
        tools={},
        message_renderers={},
        commands={},
        flags={},
        shortcuts={},
    )


# ============================================================================
# Extension loading from Python modules
# ============================================================================


def _load_python_module(module_path: str) -> Any:
    """Load a Python module from a file path using importlib."""
    spec = importlib.util.spec_from_file_location("_pi_extension", module_path)
    if not spec or not spec.loader:
        return None
    module = importlib.util.module_from_spec(spec)
    # Add to sys.modules temporarily to allow relative imports
    sys.modules["_pi_extension"] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop("_pi_extension", None)
    return module


async def _load_extension_from_file(
    extension_path: str,
    cwd: str,
    runtime: ExtensionRuntime,
) -> tuple[Extension | None, str | None]:
    """Load a single Python extension from a file path."""
    resolved_path = _resolve_path(extension_path, cwd)

    try:
        module = _load_python_module(resolved_path)
        if module is None:
            return None, f"Extension does not have a valid module: {extension_path}"

        # Look for a factory function (default export equivalent)
        factory: ExtensionFactory | None = None

        # Try common factory names
        for attr_name in ("extension", "register", "factory", "main"):
            attr = getattr(module, attr_name, None)
            if callable(attr):
                factory = attr
                break

        # Fall back to looking for any callable that takes one argument
        if factory is None:
            for attr_name in dir(module):
                if attr_name.startswith("_"):
                    continue
                attr = getattr(module, attr_name, None)
                if callable(attr) and not isinstance(attr, type):
                    factory = attr
                    break

        if factory is None:
            return None, f"Extension does not export a valid factory function: {extension_path}"

        extension = create_extension(extension_path, resolved_path)
        api = ExtensionAPI(extension, runtime, cwd)
        await factory(api)
        return extension, None

    except Exception as err:
        return None, f"Failed to load extension: {err}"


# ============================================================================
# Public API
# ============================================================================


async def load_extension_from_factory(
    factory: ExtensionFactory,
    cwd: str,
    runtime: ExtensionRuntime,
    extension_path: str = "<inline>",
) -> Extension:
    """Create an Extension from an inline factory function."""
    extension = create_extension(extension_path, extension_path)
    api = ExtensionAPI(extension, runtime, cwd)
    await factory(api)
    return extension


async def load_extensions(
    paths: list[str],
    cwd: str,
) -> LoadExtensionsResult:
    """Load extensions from paths."""
    extensions: list[Extension] = []
    errors: list[dict[str, str]] = []
    runtime = create_extension_runtime()

    for ext_path in paths:
        extension, error = await _load_extension_from_file(ext_path, cwd, runtime)

        if error:
            errors.append({"path": ext_path, "error": error})
            continue

        if extension:
            extensions.append(extension)

    return LoadExtensionsResult(extensions=extensions, errors=errors, runtime=runtime)


def _discover_extensions_in_dir(dir_path: str) -> list[str]:
    """Discover extension files in a directory.

    Discovery rules:
    1. Direct files: *.py
    2. Subdirectories with __init__.py
    """
    dirp = Path(dir_path)
    if not dirp.exists():
        return []

    discovered: list[str] = []

    try:
        for entry in dirp.iterdir():
            if entry.name.startswith(".") or entry.name == "__pycache__":
                continue

            if entry.is_file() and _is_extension_file(entry.name):
                discovered.append(str(entry))
                continue

            if entry.is_dir():
                # Check for __init__.py
                init_py = entry / "__init__.py"
                if init_py.exists():
                    discovered.append(str(init_py))
    except OSError:
        return []

    return discovered


async def discover_and_load_extensions(
    configured_paths: list[str],
    cwd: str,
    agent_dir: Path | None = None,
) -> LoadExtensionsResult:
    """Discover and load extensions from standard locations.

    Searches:
    1. Global: agent_dir/extensions/
    2. Project-local: cwd/.pi/extensions/
    3. Explicitly configured paths
    """
    resolved_agent_dir = agent_dir or get_agent_dir()
    all_paths: list[str] = []
    seen: set[str] = set()

    def add_paths(paths: list[str]) -> None:
        for p in paths:
            resolved = str(Path(p).resolve())
            if resolved not in seen:
                seen.add(resolved)
                all_paths.append(p)

    # 1. Global extensions
    global_ext_dir = resolved_agent_dir / "extensions"
    add_paths(_discover_extensions_in_dir(str(global_ext_dir)))

    # 2. Project-local extensions
    local_ext_dir = Path(cwd) / ".pi" / "extensions"
    add_paths(_discover_extensions_in_dir(str(local_ext_dir)))

    # 3. Explicitly configured paths
    for p in configured_paths:
        resolved = _resolve_path(p, cwd)
        if Path(resolved).exists() and Path(resolved).is_dir():
            add_paths(_discover_extensions_in_dir(resolved))
            continue
        add_paths([resolved])

    return await load_extensions(all_paths, cwd)
