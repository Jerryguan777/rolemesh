"""Credential storage for API keys and OAuth tokens.

Port of packages/coding-agent/src/core/auth-storage.ts.

Handles loading, saving, and refreshing credentials from auth.json.
Uses filelock.FileLock for cross-process safety (equivalent to proper-lockfile in TS).
Uses asyncio.Lock for in-process async serialisation.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeVar

import filelock

from pi.ai import get_env_api_key, get_oauth_api_key, get_oauth_provider, get_oauth_providers
from pi.ai.oauth.types import OAuthCredentials, OAuthLoginCallbacks
from pi.coding_agent.core.config import get_agent_dir

T = TypeVar("T")


# ============================================================================
# Credential types
# ============================================================================


@dataclass
class ApiKeyCredential:
    """API key credential stored in auth.json."""

    key: str = ""
    type: Literal["api_key"] = "api_key"


@dataclass
class OAuthCredential:
    """OAuth credential stored in auth.json."""

    access: str = ""
    refresh: str = ""
    expires: int = 0
    extra: dict[str, Any] = field(default_factory=dict)
    type: Literal["oauth"] = "oauth"


AuthCredential = ApiKeyCredential | OAuthCredential
AuthStorageData = dict[str, AuthCredential]


def _serialize_credential(cred: AuthCredential) -> dict[str, Any]:
    """Serialize a credential to a JSON-compatible dict."""
    if isinstance(cred, ApiKeyCredential):
        return {"type": "api_key", "key": cred.key}
    else:
        result: dict[str, Any] = {
            "type": "oauth",
            "access": cred.access,
            "refresh": cred.refresh,
            "expires": cred.expires,
        }
        result.update(cred.extra)
        return result


def _deserialize_credential(data: dict[str, Any]) -> AuthCredential:
    """Deserialize a credential from a JSON dict."""
    cred_type = data.get("type")
    if cred_type == "api_key":
        return ApiKeyCredential(key=data.get("key", ""))
    elif cred_type == "oauth":
        extra = {k: v for k, v in data.items() if k not in ("type", "access", "refresh", "expires")}
        return OAuthCredential(
            access=data.get("access", ""),
            refresh=data.get("refresh", ""),
            expires=data.get("expires", 0),
            extra=extra,
        )
    else:
        raise ValueError(f"Unknown credential type: {cred_type}")


def _oauth_credential_from_oauth_creds(creds: OAuthCredentials) -> OAuthCredential:
    """Convert OAuthCredentials to OAuthCredential."""
    return OAuthCredential(
        access=getattr(creds, "access", ""),
        refresh=getattr(creds, "refresh", ""),
        expires=getattr(creds, "expires", 0),
        extra=dict(getattr(creds, "extra", {})),
    )


def _oauth_creds_from_oauth_credential(cred: OAuthCredential) -> OAuthCredentials:
    """Convert OAuthCredential to OAuthCredentials."""
    from pi.ai.oauth.types import OAuthCredentials

    result = OAuthCredentials(
        access=cred.access,
        refresh=cred.refresh,
        expires=cred.expires,
        extra=dict(cred.extra),
    )
    return result


# ============================================================================
# Storage backends
# ============================================================================


class AuthStorageBackend(ABC):
    """Abstract backend for reading/writing auth storage."""

    @abstractmethod
    def with_lock(self, fn: Callable[[str | None], tuple[Any, str | None]]) -> Any:
        """Execute fn with a lock held. fn receives current JSON string (or None) and
        returns (result, next_json). If next_json is not None, it is written."""
        ...

    @abstractmethod
    async def with_lock_async(self, fn: Callable[[str | None], Awaitable[tuple[Any, str | None]]]) -> Any:
        """Async version of with_lock."""
        ...


class FileAuthStorageBackend(AuthStorageBackend):
    """File-based auth storage backend.

    Uses filelock.FileLock for cross-process serialisation (equivalent to
    proper-lockfile in the TS version) and asyncio.Lock for in-process async
    serialisation to avoid blocking the event loop.
    """

    _LOCK_TIMEOUT = 30  # seconds

    def __init__(self, auth_path: Path | None = None) -> None:
        self._auth_path = auth_path or (get_agent_dir() / "auth.json")
        # asyncio.Lock is created lazily so it always belongs to the running loop.
        self._async_lock: asyncio.Lock | None = None

    @property
    def _lock_path(self) -> str:
        return str(self._auth_path) + ".lock"

    def _get_async_lock(self) -> asyncio.Lock:
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        return self._async_lock

    def _ensure_parent_dir(self) -> None:
        self._auth_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self._auth_path.parent, 0o700)

    def _ensure_file_exists(self) -> None:
        if not self._auth_path.exists():
            self._auth_path.write_text("{}", encoding="utf-8")
            with contextlib.suppress(OSError):
                os.chmod(self._auth_path, 0o600)

    def _read_current(self) -> str | None:
        if self._auth_path.exists():
            return self._auth_path.read_text(encoding="utf-8")
        return None

    def _write_next(self, content: str) -> None:
        self._auth_path.write_text(content, encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(self._auth_path, 0o600)

    def with_lock(self, fn: Callable[[str | None], tuple[Any, str | None]]) -> Any:
        self._ensure_parent_dir()
        self._ensure_file_exists()
        with filelock.FileLock(self._lock_path, timeout=self._LOCK_TIMEOUT):
            current = self._read_current()
            result, next_content = fn(current)
            if next_content is not None:
                self._write_next(next_content)
            return result

    async def with_lock_async(self, fn: Callable[[str | None], Awaitable[tuple[Any, str | None]]]) -> Any:
        self._ensure_parent_dir()
        self._ensure_file_exists()
        # asyncio.Lock prevents concurrent coroutines from interleaving.
        # The file lock is acquired in a thread-pool executor so it does not
        # block the event loop while waiting for other processes.
        async with self._get_async_lock():
            fl = filelock.FileLock(self._lock_path, timeout=self._LOCK_TIMEOUT)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, fl.acquire)
            try:
                current = self._read_current()
                result, next_content = await fn(current)
                if next_content is not None:
                    self._write_next(next_content)
                return result
            finally:
                fl.release()


class InMemoryAuthStorageBackend(AuthStorageBackend):
    """In-memory auth storage backend for testing.

    No locking needed: sync callers are inherently sequential and asyncio is
    single-threaded (matching the TS InMemoryAuthStorageBackend behaviour).
    """

    def __init__(self) -> None:
        self._value: str | None = None

    def with_lock(self, fn: Callable[[str | None], tuple[Any, str | None]]) -> Any:
        result, next_content = fn(self._value)
        if next_content is not None:
            self._value = next_content
        return result

    async def with_lock_async(self, fn: Callable[[str | None], Awaitable[tuple[Any, str | None]]]) -> Any:
        result, next_content = await fn(self._value)
        if next_content is not None:
            self._value = next_content
        return result


# ============================================================================
# AuthStorage
# ============================================================================


class AuthStorage:
    """Credential storage backed by a JSON file."""

    def __init__(self, storage: AuthStorageBackend) -> None:
        self._storage = storage
        self._data: AuthStorageData = {}
        self._runtime_overrides: dict[str, str] = {}
        self._fallback_resolver: Callable[[str], str | None] | None = None
        self._load_error: Exception | None = None
        self._errors: list[Exception] = []
        self.reload()

    @staticmethod
    def create(auth_path: Path | None = None) -> AuthStorage:
        """Create an AuthStorage backed by auth.json."""
        resolved = auth_path or (get_agent_dir() / "auth.json")
        return AuthStorage(FileAuthStorageBackend(resolved))

    @staticmethod
    def from_storage(storage: AuthStorageBackend) -> AuthStorage:
        """Create an AuthStorage from an explicit backend."""
        return AuthStorage(storage)

    @staticmethod
    def in_memory(data: AuthStorageData | None = None) -> AuthStorage:
        """Create an in-memory AuthStorage for testing."""
        storage = InMemoryAuthStorageBackend()
        if data:
            storage.with_lock(lambda _: (None, json.dumps(_serialize_storage(data), indent=2)))
        return AuthStorage.from_storage(storage)

    def set_runtime_api_key(self, provider: str, api_key: str) -> None:
        """Set a runtime API key override (not persisted to disk)."""
        self._runtime_overrides[provider] = api_key

    def remove_runtime_api_key(self, provider: str) -> None:
        """Remove a runtime API key override."""
        self._runtime_overrides.pop(provider, None)

    def set_fallback_resolver(self, resolver: Callable[[str], str | None]) -> None:
        """Set a fallback resolver for API keys not found in auth.json or env vars."""
        self._fallback_resolver = resolver

    def reload(self) -> None:
        """Reload credentials from storage."""

        def _load(current: str | None) -> tuple[None, None]:
            self._data = _parse_storage_data(current)
            self._load_error = None
            return None, None

        try:
            self._storage.with_lock(_load)
        except Exception as e:
            self._load_error = e
            self._data = {}

    def get(self, provider: str) -> AuthCredential | None:
        """Get credential for a provider."""
        return self._data.get(provider)

    def set(self, provider: str, credential: AuthCredential) -> None:
        """Set credential for a provider."""
        self._data[provider] = credential
        self._persist_provider_change(provider, credential)

    def remove(self, provider: str) -> None:
        """Remove credential for a provider."""
        self._data.pop(provider, None)
        self._persist_provider_change(provider, None)

    def list(self) -> list[str]:
        """List all providers with credentials."""
        return list(self._data.keys())

    def has(self, provider: str) -> bool:
        """Check if credentials exist for a provider in auth.json."""
        return provider in self._data

    def has_auth(self, provider: str) -> bool:
        """Check if any form of auth is configured for a provider."""
        if provider in self._runtime_overrides:
            return True
        if provider in self._data:
            return True
        if get_env_api_key(provider):
            return True
        return bool(self._fallback_resolver and self._fallback_resolver(provider))

    def get_all(self) -> AuthStorageData:
        """Get all credentials."""
        return dict(self._data)

    def drain_errors(self) -> list[Exception]:  # type: ignore[valid-type]
        """Drain and return accumulated errors."""
        drained = list(self._errors)
        self._errors = []
        return drained

    async def login(self, provider_id: str, callbacks: OAuthLoginCallbacks) -> None:
        """Login to an OAuth provider."""
        provider = get_oauth_provider(provider_id)
        if not provider:
            raise ValueError(f"Unknown OAuth provider: {provider_id}")

        credentials = await provider.login(callbacks)
        cred = _oauth_credential_from_oauth_creds(credentials)
        self.set(provider_id, cred)

    def logout(self, provider: str) -> None:
        """Logout from a provider."""
        self.remove(provider)

    async def get_api_key(self, provider_id: str) -> str | None:
        """Get API key for a provider.

        Priority:
        1. Runtime override (CLI --api-key)
        2. API key from auth.json
        3. OAuth token from auth.json (auto-refreshed)
        4. Environment variable
        5. Fallback resolver (models.json custom providers)
        """
        import time

        runtime_key = self._runtime_overrides.get(provider_id)
        if runtime_key:
            return runtime_key

        cred = self._data.get(provider_id)

        if isinstance(cred, ApiKeyCredential):
            return cred.key or None

        if isinstance(cred, OAuthCredential):
            provider = get_oauth_provider(provider_id)
            if not provider:
                return None

            needs_refresh = time.time() * 1000 >= cred.expires

            if needs_refresh:
                try:
                    result = await self._refresh_oauth_token_with_lock(provider_id)
                    if result:
                        return result
                except Exception as e:
                    self._errors.append(e)
                    self.reload()
                    updated_cred = self._data.get(provider_id)
                    if isinstance(updated_cred, OAuthCredential) and time.time() * 1000 < updated_cred.expires:
                        oauth_creds = _oauth_creds_from_oauth_credential(updated_cred)
                        return provider.get_api_key(oauth_creds)  # type: ignore[no-any-return]
                    return None
            else:
                oauth_creds = _oauth_creds_from_oauth_credential(cred)
                return provider.get_api_key(oauth_creds)  # type: ignore[no-any-return]

        env_key = get_env_api_key(provider_id)
        if env_key:
            return env_key

        if self._fallback_resolver:
            return self._fallback_resolver(provider_id)

        return None

    def get_oauth_providers(self) -> list[Any]:  # type: ignore[valid-type]
        """Get all registered OAuth providers."""
        return get_oauth_providers()

    def _persist_provider_change(self, provider: str, credential: AuthCredential | None) -> None:
        """Persist a change to a single provider's credential."""

        def _update(current: str | None) -> tuple[None, str]:
            existing = _parse_storage_data(current)
            if credential is None:
                existing.pop(provider, None)
            else:
                existing[provider] = credential
            return None, json.dumps(_serialize_storage(existing), indent=2)

        try:
            self._storage.with_lock(_update)
        except Exception as e:
            self._errors.append(e)

    async def _refresh_oauth_token_with_lock(self, provider_id: str) -> str | None:
        """Refresh OAuth token with locking to prevent race conditions."""
        import time

        provider = get_oauth_provider(provider_id)
        if not provider:
            return None

        async def _refresh(current: str | None) -> tuple[str | None, str | None]:
            current_data = _parse_storage_data(current)
            self._data = current_data

            cred = current_data.get(provider_id)
            if not isinstance(cred, OAuthCredential):
                return None, None

            if time.time() * 1000 < cred.expires:
                oauth_creds = _oauth_creds_from_oauth_credential(cred)
                return provider.get_api_key(oauth_creds), None

            oauth_creds_dict: dict[str, OAuthCredentials] = {}
            for k, v in current_data.items():
                if isinstance(v, OAuthCredential):
                    oauth_creds_dict[k] = _oauth_creds_from_oauth_credential(v)

            refreshed = await get_oauth_api_key(provider_id, oauth_creds_dict)
            if not refreshed:
                return None, None

            new_credentials, api_key = refreshed
            new_oauth_cred = _oauth_credential_from_oauth_creds(new_credentials)
            merged = dict(current_data)
            merged[provider_id] = new_oauth_cred
            self._data = merged

            return api_key, json.dumps(_serialize_storage(merged), indent=2)

        return await self._storage.with_lock_async(_refresh)  # type: ignore[no-any-return]


# ============================================================================
# Helpers
# ============================================================================


def _parse_storage_data(content: str | None) -> AuthStorageData:
    """Parse auth storage JSON into AuthStorageData."""
    if not content:
        return {}
    try:
        raw = json.loads(content)
        result: AuthStorageData = {}
        for k, v in raw.items():
            if isinstance(v, dict):
                with contextlib.suppress(ValueError, KeyError):
                    result[k] = _deserialize_credential(v)
        return result
    except (json.JSONDecodeError, TypeError):
        return {}


def _serialize_storage(data: AuthStorageData) -> dict[str, Any]:
    """Serialize AuthStorageData to a JSON-compatible dict."""
    return {k: _serialize_credential(v) for k, v in data.items()}
