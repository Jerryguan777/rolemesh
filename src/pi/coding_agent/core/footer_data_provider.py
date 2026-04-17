"""Footer data provider for extensions.

Port of packages/coding-agent/src/core/footer-data-provider.ts.
Provides git branch and extension status data not otherwise accessible.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol


class ReadonlyFooterDataProvider(Protocol):
    """Read-only view for extensions - excludes mutation methods."""

    def get_git_branch(self) -> str | None:
        """Current git branch, None if not in repo, 'detached' if detached HEAD."""
        ...

    def get_extension_statuses(self) -> dict[str, str]:
        """Extension status texts set via ctx.ui.set_status()."""
        ...

    def get_available_provider_count(self) -> int:
        """Number of unique providers with available models."""
        ...

    def on_branch_change(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to git branch changes. Returns unsubscribe function."""
        ...


class FooterDataProvider:
    """Provides git branch and extension statuses.

    Data not otherwise accessible to extensions. Token stats, model info
    are available via ctx.session_manager and ctx.model.
    """

    def __init__(self) -> None:
        self._extension_statuses: dict[str, str] = {}
        self._cached_branch: str | None = None
        self._branch_change_callbacks: set[Callable[[], None]] = set()
        self._available_provider_count: int = 0

    def get_git_branch(self) -> str | None:
        """Current git branch, None if not in repo, 'detached' if detached HEAD."""
        return self._cached_branch

    def get_extension_statuses(self) -> dict[str, str]:
        """Extension status texts set via ctx.ui.set_status()."""
        return dict(self._extension_statuses)

    def on_branch_change(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to git branch changes. Returns unsubscribe function."""
        self._branch_change_callbacks.add(callback)

        def unsubscribe() -> None:
            self._branch_change_callbacks.discard(callback)

        return unsubscribe

    def set_extension_status(self, key: str, text: str | None) -> None:
        """Internal: set extension status."""
        if text is None:
            self._extension_statuses.pop(key, None)
        else:
            self._extension_statuses[key] = text

    def clear_extension_statuses(self) -> None:
        """Internal: clear extension statuses."""
        self._extension_statuses.clear()

    def get_available_provider_count(self) -> int:
        """Number of unique providers with available models (for footer display)."""
        return self._available_provider_count

    def set_available_provider_count(self, count: int) -> None:
        """Internal: update available provider count."""
        self._available_provider_count = count

    def dispose(self) -> None:
        """Internal: cleanup."""
        self._branch_change_callbacks.clear()
