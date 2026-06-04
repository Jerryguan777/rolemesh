"""Settings manager for pi.coding_agent.

Port of packages/coding-agent/src/core/settings-manager.ts.

Manages global (~/.pi/agent/settings.json) and project-local (.pi/settings.json) settings.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import filelock

from pi.coding_agent.core.config import CONFIG_DIR_NAME, get_agent_dir

# ============================================================================
# Settings dataclasses
# ============================================================================


@dataclass
class CompactionSettings:
    """Settings controlling context compaction."""

    enabled: bool | None = None
    reserve_tokens: int | None = None
    keep_recent_tokens: int | None = None


@dataclass
class BranchSummarySettings:
    """Settings for branch summarization."""

    reserve_tokens: int | None = None


@dataclass
class RetrySettings:
    """Settings for request retry behavior."""

    enabled: bool | None = None
    max_retries: int | None = None
    base_delay_ms: int | None = None
    max_delay_ms: int | None = None


@dataclass
class TerminalSettings:
    """Terminal display settings."""

    show_images: bool | None = None
    clear_on_shrink: bool | None = None


@dataclass
class ImageSettings:
    """Image handling settings."""

    auto_resize: bool | None = None
    block_images: bool | None = None


@dataclass
class ThinkingBudgetsSettings:
    """Custom token budgets for thinking levels."""

    minimal: int | None = None
    low: int | None = None
    medium: int | None = None
    high: int | None = None


@dataclass
class MarkdownSettings:
    """Markdown rendering settings."""

    code_block_indent: str | None = None


@dataclass
class PackageSourceObject:
    """Structured package source with filtering."""

    source: str = ""
    extensions: list[str] | None = None
    skills: list[str] | None = None
    prompts: list[str] | None = None
    themes: list[str] | None = None


PackageSource = str | PackageSourceObject


@dataclass
class Settings:
    """All configurable settings for pi.coding_agent."""

    last_changelog_version: str | None = None
    default_provider: str | None = None
    default_model: str | None = None
    default_thinking_level: str | None = None
    transport: str | None = None
    steering_mode: str | None = None
    follow_up_mode: str | None = None
    theme: str | None = None
    compaction: CompactionSettings | None = None
    branch_summary: BranchSummarySettings | None = None
    retry: RetrySettings | None = None
    hide_thinking_block: bool | None = None
    shell_path: str | None = None
    quiet_startup: bool | None = None
    shell_command_prefix: str | None = None
    collapse_changelog: bool | None = None
    packages: list[PackageSource] | None = None
    extensions: list[str] | None = None
    skills: list[str] | None = None
    prompts: list[str] | None = None
    themes: list[str] | None = None
    enable_skill_commands: bool | None = None
    terminal: TerminalSettings | None = None
    images: ImageSettings | None = None
    enabled_models: list[str] | None = None
    double_escape_action: str | None = None
    thinking_budgets: ThinkingBudgetsSettings | None = None
    editor_padding_x: int | None = None
    autocomplete_max_visible: int | None = None
    show_hardware_cursor: bool | None = None
    markdown: MarkdownSettings | None = None


SettingsScope = Literal["global", "project"]

# Transport setting type alias (mirrors TS: type TransportSetting = Transport)
TransportSetting = str


@dataclass
class SettingsError:
    """An error that occurred while loading settings."""

    scope: SettingsScope
    error: Exception


# ============================================================================
# Camel/snake case conversion for JSON serialization
# ============================================================================

_CAMEL_TO_SNAKE: dict[str, str] = {
    "lastChangelogVersion": "last_changelog_version",
    "defaultProvider": "default_provider",
    "defaultModel": "default_model",
    "defaultThinkingLevel": "default_thinking_level",
    "transport": "transport",
    "steeringMode": "steering_mode",
    "followUpMode": "follow_up_mode",
    "theme": "theme",
    "compaction": "compaction",
    "branchSummary": "branch_summary",
    "retry": "retry",
    "hideThinkingBlock": "hide_thinking_block",
    "shellPath": "shell_path",
    "quietStartup": "quiet_startup",
    "shellCommandPrefix": "shell_command_prefix",
    "collapseChangelog": "collapse_changelog",
    "packages": "packages",
    "extensions": "extensions",
    "skills": "skills",
    "prompts": "prompts",
    "themes": "themes",
    "enableSkillCommands": "enable_skill_commands",
    "terminal": "terminal",
    "images": "images",
    "enabledModels": "enabled_models",
    "doubleEscapeAction": "double_escape_action",
    "thinkingBudgets": "thinking_budgets",
    "editorPaddingX": "editor_padding_x",
    "autocompleteMaxVisible": "autocomplete_max_visible",
    "showHardwareCursor": "show_hardware_cursor",
    "markdown": "markdown",
}

_SNAKE_TO_CAMEL: dict[str, str] = {v: k for k, v in _CAMEL_TO_SNAKE.items()}

_COMPACTION_CAMEL: dict[str, str] = {
    "enabled": "enabled",
    "reserveTokens": "reserve_tokens",
    "keepRecentTokens": "keep_recent_tokens",
}

_BRANCH_SUMMARY_CAMEL: dict[str, str] = {
    "reserveTokens": "reserve_tokens",
}

_RETRY_CAMEL: dict[str, str] = {
    "enabled": "enabled",
    "maxRetries": "max_retries",
    "baseDelayMs": "base_delay_ms",
    "maxDelayMs": "max_delay_ms",
}

_TERMINAL_CAMEL: dict[str, str] = {
    "showImages": "show_images",
    "clearOnShrink": "clear_on_shrink",
}

_IMAGE_CAMEL: dict[str, str] = {
    "autoResize": "auto_resize",
    "blockImages": "block_images",
}

_THINKING_BUDGETS_CAMEL: dict[str, str] = {
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
}

_MARKDOWN_CAMEL: dict[str, str] = {
    "codeBlockIndent": "code_block_indent",
}


def _parse_settings_from_dict(raw: dict[str, Any]) -> Settings:
    """Parse a camelCase dict into a Settings dataclass."""
    s = Settings()

    for camel_key, snake_key in _CAMEL_TO_SNAKE.items():
        if camel_key not in raw:
            continue
        val = raw[camel_key]

        if snake_key == "compaction" and isinstance(val, dict):
            c = CompactionSettings()
            for ck, sk in _COMPACTION_CAMEL.items():
                if ck in val:
                    setattr(c, sk, val[ck])
            setattr(s, snake_key, c)

        elif snake_key == "branch_summary" and isinstance(val, dict):
            bs = BranchSummarySettings()
            for ck, sk in _BRANCH_SUMMARY_CAMEL.items():
                if ck in val:
                    setattr(bs, sk, val[ck])
            setattr(s, snake_key, bs)

        elif snake_key == "retry" and isinstance(val, dict):
            r = RetrySettings()
            for ck, sk in _RETRY_CAMEL.items():
                if ck in val:
                    setattr(r, sk, val[ck])
            setattr(s, snake_key, r)

        elif snake_key == "terminal" and isinstance(val, dict):
            t = TerminalSettings()
            for ck, sk in _TERMINAL_CAMEL.items():
                if ck in val:
                    setattr(t, sk, val[ck])
            setattr(s, snake_key, t)

        elif snake_key == "images" and isinstance(val, dict):
            img = ImageSettings()
            for ck, sk in _IMAGE_CAMEL.items():
                if ck in val:
                    setattr(img, sk, val[ck])
            setattr(s, snake_key, img)

        elif snake_key == "thinking_budgets" and isinstance(val, dict):
            tb = ThinkingBudgetsSettings()
            for ck, sk in _THINKING_BUDGETS_CAMEL.items():
                if ck in val:
                    setattr(tb, sk, val[ck])
            setattr(s, snake_key, tb)

        elif snake_key == "markdown" and isinstance(val, dict):
            md = MarkdownSettings()
            for ck, sk in _MARKDOWN_CAMEL.items():
                if ck in val:
                    setattr(md, sk, val[ck])
            setattr(s, snake_key, md)

        elif snake_key == "packages" and isinstance(val, list):
            packages: list[PackageSource] = []
            for item in val:
                if isinstance(item, str):
                    packages.append(item)
                elif isinstance(item, dict):
                    pkg = PackageSourceObject(source=item.get("source", ""))
                    pkg.extensions = item.get("extensions")
                    pkg.skills = item.get("skills")
                    pkg.prompts = item.get("prompts")
                    pkg.themes = item.get("themes")
                    packages.append(pkg)
            setattr(s, snake_key, packages)

        else:
            setattr(s, snake_key, val)

    return s


def _settings_to_dict(s: Settings) -> dict[str, Any]:
    """Serialize a Settings dataclass to a camelCase dict (excluding None values)."""
    result: dict[str, Any] = {}

    for snake_key, camel_key in _SNAKE_TO_CAMEL.items():
        val = getattr(s, snake_key, None)
        if val is None:
            continue

        if snake_key == "compaction" and isinstance(val, CompactionSettings):
            nested: dict[str, Any] = {}
            for ck, sk in _COMPACTION_CAMEL.items():
                v2 = getattr(val, sk, None)
                if v2 is not None:
                    nested[ck] = v2
            if nested:
                result[camel_key] = nested

        elif snake_key == "branch_summary" and isinstance(val, BranchSummarySettings):
            nested = {}
            for ck, sk in _BRANCH_SUMMARY_CAMEL.items():
                v2 = getattr(val, sk, None)
                if v2 is not None:
                    nested[ck] = v2
            if nested:
                result[camel_key] = nested

        elif snake_key == "retry" and isinstance(val, RetrySettings):
            nested = {}
            for ck, sk in _RETRY_CAMEL.items():
                v2 = getattr(val, sk, None)
                if v2 is not None:
                    nested[ck] = v2
            if nested:
                result[camel_key] = nested

        elif snake_key == "terminal" and isinstance(val, TerminalSettings):
            nested = {}
            for ck, sk in _TERMINAL_CAMEL.items():
                v2 = getattr(val, sk, None)
                if v2 is not None:
                    nested[ck] = v2
            if nested:
                result[camel_key] = nested

        elif snake_key == "images" and isinstance(val, ImageSettings):
            nested = {}
            for ck, sk in _IMAGE_CAMEL.items():
                v2 = getattr(val, sk, None)
                if v2 is not None:
                    nested[ck] = v2
            if nested:
                result[camel_key] = nested

        elif snake_key == "thinking_budgets" and isinstance(val, ThinkingBudgetsSettings):
            nested = {}
            for ck, sk in _THINKING_BUDGETS_CAMEL.items():
                v2 = getattr(val, sk, None)
                if v2 is not None:
                    nested[ck] = v2
            if nested:
                result[camel_key] = nested

        elif snake_key == "markdown" and isinstance(val, MarkdownSettings):
            nested = {}
            for ck, sk in _MARKDOWN_CAMEL.items():
                v2 = getattr(val, sk, None)
                if v2 is not None:
                    nested[ck] = v2
            if nested:
                result[camel_key] = nested

        elif snake_key == "packages" and isinstance(val, list):
            pkgs: list[Any] = []
            for item in val:
                if isinstance(item, str):
                    pkgs.append(item)
                elif isinstance(item, PackageSourceObject):
                    pkg_dict: dict[str, Any] = {"source": item.source}
                    if item.extensions is not None:
                        pkg_dict["extensions"] = item.extensions
                    if item.skills is not None:
                        pkg_dict["skills"] = item.skills
                    if item.prompts is not None:
                        pkg_dict["prompts"] = item.prompts
                    if item.themes is not None:
                        pkg_dict["themes"] = item.themes
                    pkgs.append(pkg_dict)
            result[camel_key] = pkgs

        else:
            result[camel_key] = val

    return result


def _deep_merge_settings(base: Settings, overrides: Settings) -> Settings:
    """Deep merge settings: overrides take precedence, nested objects merge recursively."""
    result = Settings()

    for snake_key in vars(Settings()):
        base_val = getattr(base, snake_key)
        override_val = getattr(overrides, snake_key)

        if override_val is None:
            setattr(result, snake_key, base_val)
            continue

        # For nested dataclasses, merge recursively (shallow merge of fields)
        if isinstance(
            override_val,
            (
                CompactionSettings,
                BranchSummarySettings,
                RetrySettings,
                TerminalSettings,
                ImageSettings,
                ThinkingBudgetsSettings,
                MarkdownSettings,
            ),
        ):
            if base_val is None:
                setattr(result, snake_key, override_val)
            else:
                merged = type(override_val)()
                for field_name in vars(merged):
                    ov = getattr(override_val, field_name)
                    bv = getattr(base_val, field_name)
                    setattr(merged, field_name, ov if ov is not None else bv)
                setattr(result, snake_key, merged)
        else:
            setattr(result, snake_key, override_val)

    return result


# ============================================================================
# Storage backends
# ============================================================================


class SettingsStorage(ABC):
    """Abstract storage backend for settings."""

    @abstractmethod
    def with_lock(
        self,
        scope: SettingsScope,
        fn: Callable[[str | None], str | None],
    ) -> None:
        """Execute fn with a lock held for the given scope.

        fn receives the current JSON string (or None) and returns the next
        JSON string to write (or None to skip writing).
        """
        ...


class FileSettingsStorage(SettingsStorage):
    """File-based settings storage.

    Uses filelock.FileLock for cross-process serialisation (equivalent to
    proper-lockfile in the TS version).
    """

    _LOCK_TIMEOUT = 30  # seconds

    def __init__(
        self,
        cwd: Path | None = None,
        agent_dir: Path | None = None,
    ) -> None:
        resolved_agent_dir = agent_dir or get_agent_dir()
        resolved_cwd = cwd or Path.cwd()
        self._global_path = resolved_agent_dir / "settings.json"
        self._project_path = resolved_cwd / CONFIG_DIR_NAME / "settings.json"

    def _path_for(self, scope: SettingsScope) -> Path:
        return self._global_path if scope == "global" else self._project_path

    def with_lock(
        self,
        scope: SettingsScope,
        fn: Callable[[str | None], str | None],
    ) -> None:
        path = self._path_for(scope)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = str(path) + ".lock"

        with filelock.FileLock(lock_path, timeout=self._LOCK_TIMEOUT):
            current = path.read_text(encoding="utf-8") if path.exists() else None
            next_content = fn(current)
            if next_content is not None:
                path.write_text(next_content, encoding="utf-8")


class InMemorySettingsStorage(SettingsStorage):
    """In-memory settings storage for testing.

    No locking needed: callers are sequential (matching TS InMemorySettingsStorage).
    """

    def __init__(self) -> None:
        self._global: str | None = None
        self._project: str | None = None

    def with_lock(
        self,
        scope: SettingsScope,
        fn: Callable[[str | None], str | None],
    ) -> None:
        current = self._global if scope == "global" else self._project
        next_content = fn(current)
        if next_content is not None:
            if scope == "global":
                self._global = next_content
            else:
                self._project = next_content


# ============================================================================
# SettingsManager
# ============================================================================


class SettingsManager:
    """Manages global and project settings with deep merge and persistence."""

    def __init__(
        self,
        storage: SettingsStorage,
        global_settings: Settings,
        project_settings: Settings,
        errors: list[SettingsError],
    ) -> None:
        self._storage = storage
        self._global_settings = global_settings
        self._project_settings = project_settings
        self._settings = _deep_merge_settings(global_settings, project_settings)
        self._errors: list[SettingsError] = errors
        self._modified_fields: set[str] = set()
        self._modified_nested: dict[str, set[str]] = {}
        self._modified_project_fields: set[str] = set()
        self._modified_project_nested: dict[str, set[str]] = {}

    @staticmethod
    def create(cwd: Path | None = None, agent_dir: Path | None = None) -> SettingsManager:
        """Create a SettingsManager that loads from files."""
        storage = FileSettingsStorage(cwd=cwd, agent_dir=agent_dir)
        return SettingsManager.from_storage(storage)

    @staticmethod
    def from_storage(storage: SettingsStorage) -> SettingsManager:
        """Create a SettingsManager from an explicit storage backend."""
        errors: list[SettingsError] = []

        global_settings = Settings()
        try:
            global_settings = _load_from_storage(storage, "global")
        except Exception as e:
            errors.append(SettingsError(scope="global", error=e))

        project_settings = Settings()
        try:
            project_settings = _load_from_storage(storage, "project")
        except Exception as e:
            errors.append(SettingsError(scope="project", error=e))

        return SettingsManager(storage, global_settings, project_settings, errors)

    @staticmethod
    def in_memory(settings: dict[str, Any] | None = None) -> SettingsManager:
        """Create an in-memory SettingsManager for testing."""
        storage = InMemorySettingsStorage()
        if settings:
            content = json.dumps(settings, indent=2)
            storage.with_lock("global", lambda _: content)
        return SettingsManager.from_storage(storage)

    def get_errors(self) -> list[SettingsError]:
        """Get all settings load errors."""
        return list(self._errors)

    def reload(self) -> None:
        """Reload settings from storage."""
        new_errors: list[SettingsError] = []

        global_settings = Settings()
        try:
            global_settings = _load_from_storage(self._storage, "global")
        except Exception as e:
            new_errors.append(SettingsError(scope="global", error=e))

        project_settings = Settings()
        try:
            project_settings = _load_from_storage(self._storage, "project")
        except Exception as e:
            new_errors.append(SettingsError(scope="project", error=e))

        self._global_settings = global_settings
        self._project_settings = project_settings
        self._settings = _deep_merge_settings(global_settings, project_settings)
        self._errors = new_errors

    def _mark_modified(self, field_name: str, nested_key: str | None = None) -> None:
        """Track modified global fields."""
        self._modified_fields.add(field_name)
        if nested_key:
            if field_name not in self._modified_nested:
                self._modified_nested[field_name] = set()
            self._modified_nested[field_name].add(nested_key)

    def _mark_project_modified(self, field_name: str, nested_key: str | None = None) -> None:
        """Track modified project fields."""
        self._modified_project_fields.add(field_name)
        if nested_key:
            if field_name not in self._modified_project_nested:
                self._modified_project_nested[field_name] = set()
            self._modified_project_nested[field_name].add(nested_key)

    def _save(self) -> None:
        """Persist global settings changes."""
        global_to_save = self._global_settings

        def _write(current: str | None) -> str | None:
            existing_settings = _try_parse_settings(current)
            current_dict = _settings_to_dict(existing_settings)

            in_memory_dict = _settings_to_dict(global_to_save)
            for field_name in self._modified_fields:
                camel_key = _SNAKE_TO_CAMEL.get(field_name, field_name)
                nested_keys = self._modified_nested.get(field_name)

                if nested_keys and camel_key in current_dict and isinstance(current_dict[camel_key], dict):
                    # Merge nested fields
                    in_memory_nested = in_memory_dict.get(camel_key, {})
                    if not isinstance(in_memory_nested, dict):
                        in_memory_nested = {}
                    merged_nested = dict(current_dict[camel_key])
                    for nk in nested_keys:
                        # nk is a snake_case key - find the camel version
                        nk_camel = _find_nested_camel_key(field_name, nk)
                        if nk_camel in in_memory_nested:
                            merged_nested[nk_camel] = in_memory_nested[nk_camel]
                    current_dict[camel_key] = merged_nested
                else:
                    if camel_key in in_memory_dict:
                        current_dict[camel_key] = in_memory_dict[camel_key]
                    else:
                        current_dict.pop(camel_key, None)

            return json.dumps(current_dict, indent=2)

        self._storage.with_lock("global", _write)
        self._modified_fields.clear()
        self._modified_nested.clear()
        # Recompute merged view so getters reflect the change immediately
        self._settings = _deep_merge_settings(self._global_settings, self._project_settings)

    def _save_project_settings(self, project_settings: Settings) -> None:
        """Persist project settings."""
        project_dict = _settings_to_dict(project_settings)

        def _write(_: str | None) -> str | None:
            return json.dumps(project_dict, indent=2)

        self._storage.with_lock("project", _write)
        self._modified_project_fields.clear()
        self._modified_project_nested.clear()

    # =========================================================================
    # Getters and setters
    # =========================================================================

    def set_last_changelog_version(self, version: str) -> None:
        self._global_settings.last_changelog_version = version
        self._mark_modified("last_changelog_version")
        self._save()

    def get_default_provider(self) -> str | None:
        return self._settings.default_provider

    def get_default_model(self) -> str | None:
        return self._settings.default_model

    def set_default_provider(self, provider: str) -> None:
        self._global_settings.default_provider = provider
        self._mark_modified("default_provider")
        self._save()

    def set_default_model(self, model_id: str) -> None:
        self._global_settings.default_model = model_id
        self._mark_modified("default_model")
        self._save()

    def set_default_model_and_provider(self, provider: str, model_id: str) -> None:
        self._global_settings.default_provider = provider
        self._global_settings.default_model = model_id
        self._mark_modified("default_provider")
        self._mark_modified("default_model")
        self._save()

    def get_steering_mode(self) -> str:
        return self._settings.steering_mode or "one-at-a-time"

    def set_steering_mode(self, mode: str) -> None:
        self._global_settings.steering_mode = mode
        self._mark_modified("steering_mode")
        self._save()

    def get_follow_up_mode(self) -> str:
        return self._settings.follow_up_mode or "one-at-a-time"

    def set_follow_up_mode(self, mode: str) -> None:
        self._global_settings.follow_up_mode = mode
        self._mark_modified("follow_up_mode")
        self._save()

    def get_theme(self) -> str | None:
        return self._settings.theme

    def set_theme(self, theme: str) -> None:
        self._global_settings.theme = theme
        self._mark_modified("theme")
        self._save()

    def get_default_thinking_level(self) -> str | None:
        return self._settings.default_thinking_level

    def set_default_thinking_level(self, level: str) -> None:
        self._global_settings.default_thinking_level = level
        self._mark_modified("default_thinking_level")
        self._save()

    def get_transport(self) -> str:
        return self._settings.transport or "sse"

    def set_transport(self, transport: str) -> None:
        self._global_settings.transport = transport
        self._mark_modified("transport")
        self._save()

    def get_compaction_enabled(self) -> bool:
        c = self._settings.compaction
        return c.enabled if c and c.enabled is not None else True

    def set_compaction_enabled(self, enabled: bool) -> None:
        if self._global_settings.compaction is None:
            self._global_settings.compaction = CompactionSettings()
        self._global_settings.compaction.enabled = enabled
        self._mark_modified("compaction", "enabled")
        self._save()

    def get_compaction_reserve_tokens(self) -> int:
        c = self._settings.compaction
        return c.reserve_tokens if c and c.reserve_tokens is not None else 16384

    def get_compaction_keep_recent_tokens(self) -> int:
        c = self._settings.compaction
        return c.keep_recent_tokens if c and c.keep_recent_tokens is not None else 20000

    def get_compaction_settings(self) -> dict[str, Any]:
        return {
            "enabled": self.get_compaction_enabled(),
            "reserve_tokens": self.get_compaction_reserve_tokens(),
            "keep_recent_tokens": self.get_compaction_keep_recent_tokens(),
        }

    def get_branch_summary_settings(self) -> dict[str, Any]:
        bs = self._settings.branch_summary
        return {
            "reserve_tokens": bs.reserve_tokens if bs and bs.reserve_tokens is not None else 16384,
        }

    def get_retry_enabled(self) -> bool:
        r = self._settings.retry
        return r.enabled if r and r.enabled is not None else True

    def set_retry_enabled(self, enabled: bool) -> None:
        if self._global_settings.retry is None:
            self._global_settings.retry = RetrySettings()
        self._global_settings.retry.enabled = enabled
        self._mark_modified("retry", "enabled")
        self._save()

    def get_retry_settings(self) -> dict[str, Any]:
        r = self._settings.retry
        return {
            "enabled": self.get_retry_enabled(),
            "max_retries": r.max_retries if r and r.max_retries is not None else 3,
            "base_delay_ms": r.base_delay_ms if r and r.base_delay_ms is not None else 2000,
            "max_delay_ms": r.max_delay_ms if r and r.max_delay_ms is not None else 60000,
        }

    def get_hide_thinking_block(self) -> bool:
        return self._settings.hide_thinking_block or False

    def set_hide_thinking_block(self, hide: bool) -> None:
        self._global_settings.hide_thinking_block = hide
        self._mark_modified("hide_thinking_block")
        self._save()

    def get_shell_path(self) -> str | None:
        return self._settings.shell_path

    def set_shell_path(self, path: str | None) -> None:
        self._global_settings.shell_path = path
        self._mark_modified("shell_path")
        self._save()

    def get_quiet_startup(self) -> bool:
        return self._settings.quiet_startup or False

    def set_quiet_startup(self, quiet: bool) -> None:
        self._global_settings.quiet_startup = quiet
        self._mark_modified("quiet_startup")
        self._save()

    def get_shell_command_prefix(self) -> str | None:
        return self._settings.shell_command_prefix

    def set_shell_command_prefix(self, prefix: str | None) -> None:
        self._global_settings.shell_command_prefix = prefix
        self._mark_modified("shell_command_prefix")
        self._save()

    def get_collapse_changelog(self) -> bool:
        return self._settings.collapse_changelog or False

    def set_collapse_changelog(self, collapse: bool) -> None:
        self._global_settings.collapse_changelog = collapse
        self._mark_modified("collapse_changelog")
        self._save()

    def get_packages(self) -> list[PackageSource]:
        return list(self._settings.packages or [])

    def set_packages(self, packages: list[PackageSource]) -> None:
        self._global_settings.packages = packages
        self._mark_modified("packages")
        self._save()

    def set_project_packages(self, packages: list[PackageSource]) -> None:
        project_settings = _clone_settings(self._project_settings)
        project_settings.packages = packages
        self._mark_project_modified("packages")
        self._save_project_settings(project_settings)

    def get_extension_paths(self) -> list[str]:
        return list(self._settings.extensions or [])

    def set_extension_paths(self, paths: list[str]) -> None:
        self._global_settings.extensions = paths
        self._mark_modified("extensions")
        self._save()

    def set_project_extension_paths(self, paths: list[str]) -> None:
        project_settings = _clone_settings(self._project_settings)
        project_settings.extensions = paths
        self._mark_project_modified("extensions")
        self._save_project_settings(project_settings)

    def get_skill_paths(self) -> list[str]:
        return list(self._settings.skills or [])

    def set_skill_paths(self, paths: list[str]) -> None:
        self._global_settings.skills = paths
        self._mark_modified("skills")
        self._save()

    def set_project_skill_paths(self, paths: list[str]) -> None:
        project_settings = _clone_settings(self._project_settings)
        project_settings.skills = paths
        self._mark_project_modified("skills")
        self._save_project_settings(project_settings)

    def get_prompt_template_paths(self) -> list[str]:
        return list(self._settings.prompts or [])

    def set_prompt_template_paths(self, paths: list[str]) -> None:
        self._global_settings.prompts = paths
        self._mark_modified("prompts")
        self._save()

    def set_project_prompt_template_paths(self, paths: list[str]) -> None:
        project_settings = _clone_settings(self._project_settings)
        project_settings.prompts = paths
        self._mark_project_modified("prompts")
        self._save_project_settings(project_settings)

    def get_theme_paths(self) -> list[str]:
        return list(self._settings.themes or [])

    def set_theme_paths(self, paths: list[str]) -> None:
        self._global_settings.themes = paths
        self._mark_modified("themes")
        self._save()

    def set_project_theme_paths(self, paths: list[str]) -> None:
        project_settings = _clone_settings(self._project_settings)
        project_settings.themes = paths
        self._mark_project_modified("themes")
        self._save_project_settings(project_settings)

    def get_enable_skill_commands(self) -> bool:
        v = self._settings.enable_skill_commands
        return v if v is not None else True

    def set_enable_skill_commands(self, enabled: bool) -> None:
        self._global_settings.enable_skill_commands = enabled
        self._mark_modified("enable_skill_commands")
        self._save()

    def get_thinking_budgets(self) -> ThinkingBudgetsSettings | None:
        return self._settings.thinking_budgets

    def get_show_images(self) -> bool:
        t = self._settings.terminal
        return t.show_images if t and t.show_images is not None else True

    def set_show_images(self, show: bool) -> None:
        if self._global_settings.terminal is None:
            self._global_settings.terminal = TerminalSettings()
        self._global_settings.terminal.show_images = show
        self._mark_modified("terminal", "show_images")
        self._save()

    def get_clear_on_shrink(self) -> bool:
        t = self._settings.terminal
        if t and t.clear_on_shrink is not None:
            return t.clear_on_shrink
        return os.environ.get("PI_CLEAR_ON_SHRINK") == "1"

    def set_clear_on_shrink(self, enabled: bool) -> None:
        if self._global_settings.terminal is None:
            self._global_settings.terminal = TerminalSettings()
        self._global_settings.terminal.clear_on_shrink = enabled
        self._mark_modified("terminal", "clear_on_shrink")
        self._save()

    def get_image_auto_resize(self) -> bool:
        img = self._settings.images
        return img.auto_resize if img and img.auto_resize is not None else True

    def set_image_auto_resize(self, enabled: bool) -> None:
        if self._global_settings.images is None:
            self._global_settings.images = ImageSettings()
        self._global_settings.images.auto_resize = enabled
        self._mark_modified("images", "auto_resize")
        self._save()

    def get_block_images(self) -> bool:
        img = self._settings.images
        return img.block_images if img and img.block_images is not None else False

    def set_block_images(self, blocked: bool) -> None:
        if self._global_settings.images is None:
            self._global_settings.images = ImageSettings()
        self._global_settings.images.block_images = blocked
        self._mark_modified("images", "block_images")
        self._save()

    def get_enabled_models(self) -> list[str] | None:
        return self._settings.enabled_models

    def set_enabled_models(self, patterns: list[str] | None) -> None:
        self._global_settings.enabled_models = patterns
        self._mark_modified("enabled_models")
        self._save()

    def get_double_escape_action(self) -> str:
        return self._settings.double_escape_action or "tree"

    def set_double_escape_action(self, action: str) -> None:
        self._global_settings.double_escape_action = action
        self._mark_modified("double_escape_action")
        self._save()

    def get_show_hardware_cursor(self) -> bool:
        v = self._settings.show_hardware_cursor
        if v is not None:
            return v
        return os.environ.get("PI_HARDWARE_CURSOR") == "1"

    def set_show_hardware_cursor(self, enabled: bool) -> None:
        self._global_settings.show_hardware_cursor = enabled
        self._mark_modified("show_hardware_cursor")
        self._save()

    def get_editor_padding_x(self) -> int:
        return self._settings.editor_padding_x or 0

    def set_editor_padding_x(self, padding: int) -> None:
        self._global_settings.editor_padding_x = max(0, min(3, int(padding)))
        self._mark_modified("editor_padding_x")
        self._save()

    def get_autocomplete_max_visible(self) -> int:
        return self._settings.autocomplete_max_visible or 5

    def set_autocomplete_max_visible(self, max_visible: int) -> None:
        self._global_settings.autocomplete_max_visible = max(3, min(20, int(max_visible)))
        self._mark_modified("autocomplete_max_visible")
        self._save()

    def get_code_block_indent(self) -> str:
        md = self._settings.markdown
        return md.code_block_indent if md and md.code_block_indent is not None else "  "


# ============================================================================
# Helpers
# ============================================================================


def _load_from_storage(storage: SettingsStorage, scope: SettingsScope) -> Settings:
    """Load settings from storage for a given scope."""
    result: Settings | None = None
    error_holder: list[Exception] = []

    def _read(current: str | None) -> str | None:
        nonlocal result
        if not current:
            result = Settings()
            return None
        try:
            raw = json.loads(current)
            result = _parse_settings_from_dict(raw) if isinstance(raw, dict) else Settings()
        except (json.JSONDecodeError, TypeError) as e:
            error_holder.append(e)
            result = Settings()
        return None

    storage.with_lock(scope, _read)
    if error_holder:
        raise error_holder[0]
    return result or Settings()


def _try_parse_settings(content: str | None) -> Settings:
    """Parse settings JSON, returning empty settings on failure."""
    if not content:
        return Settings()
    try:
        raw = json.loads(content)
        if isinstance(raw, dict):
            return _parse_settings_from_dict(raw)
    except (json.JSONDecodeError, TypeError):
        pass
    return Settings()


def _clone_settings(s: Settings) -> Settings:
    """Shallow clone a Settings object."""
    clone = Settings()
    for field_name in vars(s):
        setattr(clone, field_name, getattr(s, field_name))
    return clone


def _find_nested_camel_key(field_name: str, snake_nested_key: str) -> str:
    """Find the camelCase version of a nested settings key."""
    nested_maps: dict[str, dict[str, str]] = {
        "compaction": {v: k for k, v in _COMPACTION_CAMEL.items()},
        "branch_summary": {v: k for k, v in _BRANCH_SUMMARY_CAMEL.items()},
        "retry": {v: k for k, v in _RETRY_CAMEL.items()},
        "terminal": {v: k for k, v in _TERMINAL_CAMEL.items()},
        "images": {v: k for k, v in _IMAGE_CAMEL.items()},
        "thinking_budgets": {v: k for k, v in _THINKING_BUDGETS_CAMEL.items()},
        "markdown": {v: k for k, v in _MARKDOWN_CAMEL.items()},
    }
    snake_to_camel = nested_maps.get(field_name, {})
    return snake_to_camel.get(snake_nested_key, snake_nested_key)
