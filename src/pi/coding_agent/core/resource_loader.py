"""Resource loader for pi.coding_agent.

Simplified port of packages/coding-agent/src/core/resource-loader.ts.
Loads extensions, skills, prompt templates without package manager support.
"""

from __future__ import annotations

import contextlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pi.coding_agent.core.extensions.loader import discover_and_load_extensions
from pi.coding_agent.core.extensions.types import LoadExtensionsResult
from pi.coding_agent.core.prompt_templates import PromptTemplate, load_prompt_templates
from pi.coding_agent.core.skills import Skill, load_skills

# ============================================================================
# Types
# ============================================================================


@dataclass
class ResourceCollision:
    """A collision between two resources of the same name."""

    resource_type: str  # "extension" | "skill" | "prompt" | "theme"
    name: str
    winner_path: str
    loser_path: str
    winner_source: str | None = None
    loser_source: str | None = None


@dataclass
class ResourceDiagnostic:
    """A diagnostic message from resource loading."""

    type: str  # "warning" | "error" | "collision"
    message: str
    path: str | None = None
    collision: ResourceCollision | None = None


@dataclass
class ResourceExtensionPaths:
    """Paths provided by extensions for additional resources."""

    skill_paths: list[dict[str, Any]] | None = None  # [{path, metadata}]
    prompt_paths: list[dict[str, Any]] | None = None
    theme_paths: list[dict[str, Any]] | None = None


# ============================================================================
# Abstract base
# ============================================================================


class ResourceLoader(ABC):
    """Abstract resource loader interface."""

    @abstractmethod
    def get_extensions(self) -> LoadExtensionsResult:
        """Get loaded extensions."""
        ...

    @abstractmethod
    def get_skills(self) -> dict[str, Skill]:
        """Get loaded skills as a name->Skill dict."""
        ...

    @abstractmethod
    def get_prompts(self) -> dict[str, PromptTemplate]:
        """Get loaded prompt templates as a name->PromptTemplate dict."""
        ...

    @abstractmethod
    def get_system_prompt(self) -> str | None:
        """Get the override system prompt (if set)."""
        ...

    @abstractmethod
    def get_append_system_prompt(self) -> list[str]:
        """Get the list of system prompt appendices."""
        ...

    @abstractmethod
    async def reload(self) -> None:
        """Reload all resources."""
        ...


# ============================================================================
# Default implementation
# ============================================================================


@dataclass
class DefaultResourceLoaderOptions:
    """Options for DefaultResourceLoader."""

    cwd: str | None = None
    agent_dir: str | None = None
    settings_manager: Any | None = None
    additional_extension_paths: list[str] = field(default_factory=list)
    additional_skill_paths: list[str] = field(default_factory=list)
    additional_prompt_template_paths: list[str] = field(default_factory=list)
    no_extensions: bool = False
    no_skills: bool = False
    no_prompt_templates: bool = False
    system_prompt: str | None = None
    append_system_prompt: str | None = None


class DefaultResourceLoader(ResourceLoader):
    """Default resource loader that loads from files and settings."""

    def __init__(self, options: DefaultResourceLoaderOptions) -> None:
        self._options = options
        self._cwd = options.cwd or str(Path.cwd())
        self._agent_dir = Path(options.agent_dir) if options.agent_dir else None
        self._extensions: LoadExtensionsResult = LoadExtensionsResult(
            extensions=[], errors=[], runtime=_create_empty_runtime()
        )
        self._skills: dict[str, Skill] = {}
        self._prompts: dict[str, PromptTemplate] = {}

    def get_extensions(self) -> LoadExtensionsResult:
        return self._extensions

    def get_skills(self) -> dict[str, Skill]:
        return dict(self._skills)

    def get_prompts(self) -> dict[str, PromptTemplate]:
        return dict(self._prompts)

    def get_system_prompt(self) -> str | None:
        return self._options.system_prompt

    def get_append_system_prompt(self) -> list[str]:
        result: list[str] = []
        if self._options.append_system_prompt:
            result.append(self._options.append_system_prompt)
        return result

    async def reload(self) -> None:
        """Reload all resources from disk."""
        await self._load_extensions()
        self._load_skills()
        self._load_prompts()

    async def _load_extensions(self) -> None:
        """Load extensions from configured paths."""
        if self._options.no_extensions:
            return

        extension_paths = list(self._options.additional_extension_paths)

        # Add paths from settings manager if available
        if self._options.settings_manager:
            configured: list[str] = getattr(self._options.settings_manager, "get_extension_paths", lambda: [])()
            extension_paths.extend(configured)

        with contextlib.suppress(Exception):
            self._extensions = await discover_and_load_extensions(
                extension_paths,
                self._cwd,
                self._agent_dir,
            )

    def _load_skills(self) -> None:
        """Load skills from configured paths."""
        if self._options.no_skills:
            return

        skill_paths = list(self._options.additional_skill_paths)

        if self._options.settings_manager:
            configured: list[str] = getattr(self._options.settings_manager, "get_skill_paths", lambda: [])()
            skill_paths.extend(configured)

        result = load_skills(
            {
                "cwd": self._cwd,
                "agent_dir": str(self._agent_dir) if self._agent_dir else None,
                "skill_paths": skill_paths,
            }
        )
        self._skills = {s.name: s for s in result.skills}

    def _load_prompts(self) -> None:
        """Load prompt templates from configured paths."""
        if self._options.no_prompt_templates:
            return

        prompt_paths = list(self._options.additional_prompt_template_paths)

        if self._options.settings_manager:
            configured: list[str] = getattr(self._options.settings_manager, "get_prompt_template_paths", lambda: [])()
            prompt_paths.extend(configured)

        templates = load_prompt_templates(
            {
                "cwd": self._cwd,
                "agent_dir": str(self._agent_dir) if self._agent_dir else None,
                "prompt_paths": prompt_paths,
            }
        )
        self._prompts = {t.name: t for t in templates}


def _create_empty_runtime() -> Any:
    """Create an empty ExtensionRuntime for default initialization."""
    from pi.coding_agent.core.extensions.types import ExtensionRuntime

    return ExtensionRuntime()
