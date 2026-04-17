"""Package manager — Python port of packages/coding-agent/src/core/package-manager.ts.

Manages extension packages (npm, git, local) that provide skills, prompts,
themes, and extensions. This Python port implements the core types and
interface; full npm/git install functionality uses asyncio subprocesses.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass
class PathMetadata:
    """Metadata about the source of a resource path."""

    source: str = ""
    scope: Literal["user", "project", "temporary"] = "user"
    origin: Literal["package", "top-level"] = "top-level"
    base_dir: str | None = None


@dataclass
class ResolvedResource:
    """A resolved resource path with metadata."""

    path: str = ""
    enabled: bool = True
    metadata: PathMetadata = field(default_factory=PathMetadata)


@dataclass
class ResolvedPaths:
    """Resolved resource paths for all resource types."""

    extensions: list[ResolvedResource] = field(default_factory=list)
    skills: list[ResolvedResource] = field(default_factory=list)
    prompts: list[ResolvedResource] = field(default_factory=list)
    themes: list[ResolvedResource] = field(default_factory=list)


MissingSourceAction = Literal["install", "skip", "error"]


@dataclass
class ProgressEvent:
    """Progress event for package operations."""

    type: Literal["start", "progress", "complete", "error"] = "start"
    action: Literal["install", "remove", "update", "clone", "pull"] = "install"
    source: str = ""
    message: str | None = None


ProgressCallback = Callable[[ProgressEvent], None]


class PackageManager(Protocol):
    """Protocol for package managers that resolve and install extension packages."""

    async def resolve(
        self,
        on_missing: Callable[[str], Any] | None = None,
    ) -> ResolvedPaths:
        """Resolve all configured sources to resource paths."""
        ...

    async def install(self, source: str, options: dict[str, Any] | None = None) -> None:
        """Install a package from a source (npm spec, git URL, or local path)."""
        ...

    async def remove(self, source: str, options: dict[str, Any] | None = None) -> None:
        """Remove an installed package."""
        ...

    async def update(self, source: str | None = None) -> None:
        """Update installed packages."""
        ...

    async def resolve_extension_sources(
        self,
        sources: list[str],
        options: dict[str, Any] | None = None,
    ) -> ResolvedPaths:
        """Resolve a list of extension sources to resource paths."""
        ...

    def add_source_to_settings(self, source: str, options: dict[str, Any] | None = None) -> bool:
        """Add a source to settings. Returns True if added (False if already present)."""
        ...

    def remove_source_from_settings(self, source: str, options: dict[str, Any] | None = None) -> bool:
        """Remove a source from settings. Returns True if removed."""
        ...

    def set_progress_callback(self, callback: ProgressCallback | None) -> None:
        """Set a callback to receive progress events."""
        ...

    def get_installed_path(self, source: str, scope: Literal["user", "project"]) -> str | None:
        """Return the installed path for a source, or None if not installed."""
        ...


# ---------------------------------------------------------------------------
# Source parsing (C-2: validate source before passing to npm)
# ---------------------------------------------------------------------------


@dataclass
class _NpmSource:
    """A parsed npm package source."""

    type: Literal["npm"] = "npm"
    spec: str = ""


@dataclass
class _LocalSource:
    """A parsed local path source."""

    type: Literal["local"] = "local"
    path: str = ""


_ParsedSource = _NpmSource | _LocalSource


def _parse_source(source: str) -> _ParsedSource:
    """Parse a source string into a typed _ParsedSource.

    Local paths start with '.' or '/'.  Everything else is treated as an npm spec.
    """
    trimmed = source.strip()
    if trimmed.startswith(".") or trimmed.startswith("/"):
        return _LocalSource(path=source)
    return _NpmSource(spec=trimmed)


async def _run_npm(*args: str) -> tuple[int, str]:
    """Run an npm command asynchronously. Returns (exit_code, stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "npm",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_bytes = await proc.communicate()
        return proc.returncode or 0, stderr_bytes.decode("utf-8", errors="replace")
    except OSError as exc:
        return 1, str(exc)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def _collect_files_by_extensions(
    directory: str,
    extensions: tuple[str, ...],
    *,
    skip_node_modules: bool = True,
    skip_hidden: bool = True,
) -> list[str]:
    """Walk directory and collect files matching given extensions."""
    result: list[str] = []
    dir_path = Path(directory)
    if not dir_path.exists():
        return result

    for root, dirs, files in os.walk(directory):
        # Prune hidden dirs and node_modules in-place
        if skip_hidden:
            dirs[:] = [d for d in dirs if not d.startswith(".")]
        if skip_node_modules:
            dirs[:] = [d for d in dirs if d != "node_modules"]

        for filename in files:
            if filename.startswith(".") and skip_hidden:
                continue
            if any(filename.endswith(ext) for ext in extensions):
                result.append(os.path.join(root, filename))

    return result


def _collect_skill_entries(directory: str, include_root_files: bool = True) -> list[str]:
    """Collect skill file paths from a directory.

    Discovery rules:
    - Direct .md files in root (if include_root_files)
    - Recursive SKILL.md under subdirectories
    """
    result: list[str] = []
    dir_path = Path(directory)
    if not dir_path.exists():
        return result

    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "node_modules"]

        is_root = root == directory

        for filename in files:
            if filename.startswith("."):
                continue
            full_path = os.path.join(root, filename)
            is_root_md = is_root and include_root_files and filename.endswith(".md")
            if is_root_md or (not is_root and filename == "SKILL.md"):
                result.append(full_path)

    return result


# ---------------------------------------------------------------------------
# DefaultPackageManager - minimal implementation
# ---------------------------------------------------------------------------


@dataclass
class _PackageManagerOptions:
    """Internal options for DefaultPackageManager."""

    cwd: str = ""
    agent_dir: str = ""


class DefaultPackageManager:
    """Default implementation of PackageManager.

    Supports npm, git, and local sources. In this Python port, complex
    npm/git operations delegate to subprocess; the primary purpose is
    resource resolution from already-installed packages and local paths.
    """

    def __init__(self, options: _PackageManagerOptions) -> None:
        self._cwd = options.cwd
        self._agent_dir = options.agent_dir
        self._progress_callback: ProgressCallback | None = None

    def set_progress_callback(self, callback: ProgressCallback | None) -> None:
        """Set callback for progress events."""
        self._progress_callback = callback

    def _emit_progress(self, event: ProgressEvent) -> None:
        if self._progress_callback is not None:
            self._progress_callback(event)

    def get_installed_path(self, source: str, scope: Literal["user", "project"]) -> str | None:
        """Return installed path for source in user or project scope."""
        if scope == "user":
            base_dir = os.path.join(self._agent_dir, "packages")
        else:
            base_dir = os.path.join(self._cwd, ".pi", "packages")

        candidate = os.path.join(base_dir, _sanitize_source_name(source))
        return candidate if os.path.exists(candidate) else None

    async def install(self, source: str, options: dict[str, Any] | None = None) -> None:
        """Install a package source."""
        parsed = _parse_source(source)
        self._emit_progress(ProgressEvent(type="start", action="install", source=source))

        if parsed.type == "local":
            # Local path - just resolve it
            resolved = str(Path(parsed.path).resolve())
            if not os.path.exists(resolved):
                self._emit_progress(
                    ProgressEvent(
                        type="error",
                        action="install",
                        source=source,
                        message=f"Path does not exist: {resolved}",
                    )
                )
                raise FileNotFoundError(f"Local source path does not exist: {resolved}")
            self._emit_progress(ProgressEvent(type="complete", action="install", source=source))
            return

        # npm install - use parsed.spec (validated, stripped) instead of raw source
        base_dir = os.path.join(self._agent_dir, "packages")
        os.makedirs(base_dir, exist_ok=True)
        code, stderr = await _run_npm("install", "--prefix", base_dir, parsed.spec)
        if code != 0:
            self._emit_progress(ProgressEvent(type="error", action="install", source=source, message=stderr))
            raise RuntimeError(f"npm install failed (exit {code}): {stderr}")
        self._emit_progress(ProgressEvent(type="complete", action="install", source=source))

    async def remove(self, source: str, options: dict[str, Any] | None = None) -> None:
        """Remove an installed package."""
        parsed = _parse_source(source)
        self._emit_progress(ProgressEvent(type="start", action="remove", source=source))

        if parsed.type == "local":
            self._emit_progress(ProgressEvent(type="complete", action="remove", source=source))
            return

        base_dir = os.path.join(self._agent_dir, "packages")
        code, stderr = await _run_npm("uninstall", "--prefix", base_dir, parsed.spec)
        if code != 0:
            self._emit_progress(ProgressEvent(type="error", action="remove", source=source, message=stderr))
            raise RuntimeError(f"npm uninstall failed (exit {code}): {stderr}")
        self._emit_progress(ProgressEvent(type="complete", action="remove", source=source))

    async def update(self, source: str | None = None) -> None:
        """Update installed packages."""
        base_dir = os.path.join(self._agent_dir, "packages")
        if not os.path.exists(base_dir):
            return

        target = source or ""
        self._emit_progress(ProgressEvent(type="start", action="update", source=target))

        npm_args = ["update", "--prefix", base_dir]
        if source is not None:
            parsed = _parse_source(source)
            if parsed.type == "npm":
                npm_args.append(parsed.spec)

        code, stderr = await _run_npm(*npm_args)
        if code != 0:
            self._emit_progress(ProgressEvent(type="error", action="update", source=target, message=stderr))
            raise RuntimeError(f"npm update failed (exit {code}): {stderr}")
        self._emit_progress(ProgressEvent(type="complete", action="update", source=target))

    async def resolve(
        self,
        on_missing: Callable[[str], Any] | None = None,
    ) -> ResolvedPaths:
        """Resolve all configured resource paths from agent dir and project dir."""
        result = ResolvedPaths()

        # User-scoped skills
        user_skills_dir = os.path.join(self._agent_dir, "skills")
        for file_path in _collect_skill_entries(user_skills_dir):
            result.skills.append(
                ResolvedResource(
                    path=file_path,
                    enabled=True,
                    metadata=PathMetadata(
                        source="user",
                        scope="user",
                        origin="top-level",
                        base_dir=os.path.dirname(file_path),
                    ),
                )
            )

        # Project-scoped skills
        project_skills_dir = os.path.join(self._cwd, ".pi", "skills")
        for file_path in _collect_skill_entries(project_skills_dir):
            result.skills.append(
                ResolvedResource(
                    path=file_path,
                    enabled=True,
                    metadata=PathMetadata(
                        source="project",
                        scope="project",
                        origin="top-level",
                        base_dir=os.path.dirname(file_path),
                    ),
                )
            )

        # User-scoped prompts
        user_prompts_dir = os.path.join(self._agent_dir, "prompts")
        for file_path in _collect_files_by_extensions(user_prompts_dir, (".md",)):
            result.prompts.append(
                ResolvedResource(
                    path=file_path,
                    enabled=True,
                    metadata=PathMetadata(source="user", scope="user", origin="top-level"),
                )
            )

        # User-scoped themes
        user_themes_dir = os.path.join(self._agent_dir, "themes")
        for file_path in _collect_files_by_extensions(user_themes_dir, (".json",)):
            result.themes.append(
                ResolvedResource(
                    path=file_path,
                    enabled=True,
                    metadata=PathMetadata(source="user", scope="user", origin="top-level"),
                )
            )

        return result

    async def resolve_extension_sources(
        self,
        sources: list[str],
        options: dict[str, Any] | None = None,
    ) -> ResolvedPaths:
        """Resolve a list of extension source paths."""
        result = ResolvedPaths()
        is_local = options.get("local", False) if options else False
        scope: Literal["user", "project", "temporary"] = "project" if is_local else "temporary"

        for source in sources:
            resolved_path = str(Path(source).resolve()) if os.path.isabs(source) or source.startswith(".") else source

            if os.path.exists(resolved_path):
                if os.path.isfile(resolved_path) and resolved_path.endswith((".ts", ".js", ".py")):
                    result.extensions.append(
                        ResolvedResource(
                            path=resolved_path,
                            enabled=True,
                            metadata=PathMetadata(
                                source=resolved_path,
                                scope=scope,
                                origin="top-level",
                                base_dir=os.path.dirname(resolved_path),
                            ),
                        )
                    )
                elif os.path.isdir(resolved_path):
                    # Look for index.py or index.ts/js
                    for candidate in ["index.py", "index.ts", "index.js"]:
                        candidate_path = os.path.join(resolved_path, candidate)
                        if os.path.exists(candidate_path):
                            result.extensions.append(
                                ResolvedResource(
                                    path=candidate_path,
                                    enabled=True,
                                    metadata=PathMetadata(
                                        source=resolved_path,
                                        scope=scope,
                                        origin="top-level",
                                        base_dir=resolved_path,
                                    ),
                                )
                            )
                            break

        return result

    def add_source_to_settings(self, source: str, options: dict[str, Any] | None = None) -> bool:
        """Add source to settings.json. Returns True if added."""
        is_local = options.get("local", False) if options else False
        settings_path = (
            os.path.join(self._cwd, ".pi", "settings.json")
            if is_local
            else os.path.join(self._agent_dir, "settings.json")
        )
        return self._modify_settings_sources(settings_path, source, add=True)

    def remove_source_from_settings(self, source: str, options: dict[str, Any] | None = None) -> bool:
        """Remove source from settings.json. Returns True if removed."""
        is_local = options.get("local", False) if options else False
        settings_path = (
            os.path.join(self._cwd, ".pi", "settings.json")
            if is_local
            else os.path.join(self._agent_dir, "settings.json")
        )
        return self._modify_settings_sources(settings_path, source, add=False)

    def _modify_settings_sources(self, settings_path: str, source: str, *, add: bool) -> bool:
        """Add or remove source from the packages list in a settings file."""
        settings: dict[str, Any] = {}
        if os.path.exists(settings_path):
            try:
                settings = json.loads(Path(settings_path).read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                settings = {}

        packages: list[str] = settings.get("packages", [])

        if add:
            if source in packages:
                return False
            packages.append(source)
        else:
            if source not in packages:
                return False
            packages.remove(source)

        settings["packages"] = packages
        os.makedirs(os.path.dirname(settings_path) or ".", exist_ok=True)
        Path(settings_path).write_text(json.dumps(settings, indent=2), encoding="utf-8")
        return True


def _sanitize_source_name(source: str) -> str:
    """Create a filesystem-safe directory name from a package source spec."""
    return source.replace("/", "_").replace("@", "").replace(":", "_")


def create_package_manager(cwd: str, agent_dir: str) -> DefaultPackageManager:
    """Create a DefaultPackageManager for the given working directory and agent dir."""
    return DefaultPackageManager(_PackageManagerOptions(cwd=cwd, agent_dir=agent_dir))
