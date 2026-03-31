"""Mount validation against external allowlist.

Validates additional mounts against an allowlist stored OUTSIDE the project root.
This prevents container agents from modifying security configuration.

Allowlist location: ~/.config/rolemesh/mount-allowlist.json
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from rolemesh.core.config import MOUNT_ALLOWLIST_PATH
from rolemesh.core.logger import get_logger
from rolemesh.core.types import AdditionalMount, AllowedRoot, MountAllowlist

logger = get_logger()

# Cache the allowlist in memory — only reloads on process restart
_cached_allowlist: MountAllowlist | None = None
_allowlist_load_error: str | None = None

DEFAULT_BLOCKED_PATTERNS: list[str] = [
    ".ssh",
    ".gnupg",
    ".gpg",
    ".aws",
    ".azure",
    ".gcloud",
    ".kube",
    ".docker",
    "credentials",
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "id_rsa",
    "id_ed25519",
    "private_key",
    ".secret",
]


def load_mount_allowlist() -> MountAllowlist | None:
    """Load the mount allowlist from the external config location.

    Returns None if the file doesn't exist or is invalid.
    Result is cached in memory for the lifetime of the process.
    """
    global _cached_allowlist, _allowlist_load_error

    if _cached_allowlist is not None:
        return _cached_allowlist

    if _allowlist_load_error is not None:
        return None

    try:
        if not MOUNT_ALLOWLIST_PATH.exists():
            _allowlist_load_error = f"Mount allowlist not found at {MOUNT_ALLOWLIST_PATH}"
            logger.warning(
                "Mount allowlist not found - additional mounts will be BLOCKED. "
                "Create the file to enable additional mounts.",
                path=str(MOUNT_ALLOWLIST_PATH),
            )
            return None

        content = MOUNT_ALLOWLIST_PATH.read_text(encoding="utf-8")
        raw = json.loads(content)

        if not isinstance(raw.get("allowedRoots"), list):
            raise ValueError("allowedRoots must be an array")
        if not isinstance(raw.get("blockedPatterns"), list):
            raise ValueError("blockedPatterns must be an array")
        if not isinstance(raw.get("nonMainReadOnly"), bool):
            raise ValueError("nonMainReadOnly must be a boolean")

        allowed_roots = [
            AllowedRoot(
                path=r["path"],
                allow_read_write=r.get("allowReadWrite", False),
                description=r.get("description"),
            )
            for r in raw["allowedRoots"]
        ]

        merged_blocked = list(dict.fromkeys(DEFAULT_BLOCKED_PATTERNS + raw["blockedPatterns"]))

        _cached_allowlist = MountAllowlist(
            allowed_roots=allowed_roots,
            blocked_patterns=merged_blocked,
            non_main_read_only=raw["nonMainReadOnly"],
        )

        logger.info(
            "Mount allowlist loaded successfully",
            path=str(MOUNT_ALLOWLIST_PATH),
            allowed_roots=len(allowed_roots),
            blocked_patterns=len(merged_blocked),
        )
        return _cached_allowlist

    except (json.JSONDecodeError, ValueError, KeyError, TypeError, OSError) as exc:
        _allowlist_load_error = str(exc)
        logger.error(
            "Failed to load mount allowlist - additional mounts will be BLOCKED",
            path=str(MOUNT_ALLOWLIST_PATH),
            error=_allowlist_load_error,
        )
        return None


def reset_cache() -> None:
    """Reset the cached allowlist (for testing)."""
    global _cached_allowlist, _allowlist_load_error
    _cached_allowlist = None
    _allowlist_load_error = None


def _expand_path(p: str) -> Path:
    """Expand ~ to home directory and resolve to absolute path."""
    return Path(os.path.expanduser(p)).resolve()


def _get_real_path(p: Path) -> Path | None:
    """Get the real path, resolving symlinks. Returns None if it doesn't exist."""
    try:
        return p.resolve(strict=True)
    except OSError:
        return None


def _matches_blocked_pattern(real_path: Path, blocked_patterns: list[str]) -> str | None:
    """Check if a path matches any blocked pattern."""
    path_str = str(real_path)
    parts = real_path.parts

    for pattern in blocked_patterns:
        for part in parts:
            if part == pattern or pattern in part:
                return pattern
        if pattern in path_str:
            return pattern
    return None


def _find_allowed_root(real_path: Path, allowed_roots: list[AllowedRoot]) -> AllowedRoot | None:
    """Check if a real path is under an allowed root."""
    for root in allowed_roots:
        expanded_root = _expand_path(root.path)
        real_root = _get_real_path(expanded_root)
        if real_root is None:
            continue
        try:
            real_path.relative_to(real_root)
            return root
        except ValueError:
            continue
    return None


def _is_valid_container_path(container_path: str) -> bool:
    """Validate the container path to prevent escaping /workspace/extra/."""
    if ".." in container_path:
        return False
    if container_path.startswith("/"):
        return False
    return bool(container_path and container_path.strip())


@dataclass
class MountValidationResult:
    """Result of validating a mount against the allowlist."""

    allowed: bool
    reason: str
    real_host_path: str | None = None
    resolved_container_path: str | None = None
    effective_readonly: bool | None = None


def validate_mount(mount: AdditionalMount, is_main: bool) -> MountValidationResult:
    """Validate a single additional mount against the allowlist."""
    allowlist = load_mount_allowlist()

    if allowlist is None:
        return MountValidationResult(
            allowed=False,
            reason=f"No mount allowlist configured at {MOUNT_ALLOWLIST_PATH}",
        )

    container_path = mount.container_path or Path(mount.host_path).name

    if not _is_valid_container_path(container_path):
        return MountValidationResult(
            allowed=False,
            reason=f'Invalid container path: "{container_path}" - must be relative, non-empty, and not contain ".."',
        )

    expanded_path = _expand_path(mount.host_path)
    real_path = _get_real_path(expanded_path)

    if real_path is None:
        return MountValidationResult(
            allowed=False,
            reason=f'Host path does not exist: "{mount.host_path}" (expanded: "{expanded_path}")',
        )

    blocked_match = _matches_blocked_pattern(real_path, allowlist.blocked_patterns)
    if blocked_match is not None:
        return MountValidationResult(
            allowed=False,
            reason=f'Path matches blocked pattern "{blocked_match}": "{real_path}"',
        )

    allowed_root = _find_allowed_root(real_path, allowlist.allowed_roots)
    if allowed_root is None:
        roots_str = ", ".join(str(_expand_path(r.path)) for r in allowlist.allowed_roots)
        return MountValidationResult(
            allowed=False,
            reason=f'Path "{real_path}" is not under any allowed root. Allowed roots: {roots_str}',
        )

    requested_read_write = not mount.readonly
    effective_readonly = True

    if requested_read_write:
        if not is_main and allowlist.non_main_read_only:
            logger.info("Mount forced to read-only for non-main group", mount=mount.host_path)
        elif not allowed_root.allow_read_write:
            logger.info(
                "Mount forced to read-only - root does not allow read-write",
                mount=mount.host_path,
                root=allowed_root.path,
            )
        else:
            effective_readonly = False

    desc = f" ({allowed_root.description})" if allowed_root.description else ""
    return MountValidationResult(
        allowed=True,
        reason=f'Allowed under root "{allowed_root.path}"{desc}',
        real_host_path=str(real_path),
        resolved_container_path=container_path,
        effective_readonly=effective_readonly,
    )


def validate_additional_mounts(
    mounts: list[AdditionalMount],
    group_name: str,
    is_main: bool,
) -> list[dict[str, object]]:
    """Validate all additional mounts for a group.

    Returns list of validated mounts (only those that passed).
    """
    validated: list[dict[str, object]] = []

    for mount in mounts:
        result = validate_mount(mount, is_main)

        if result.allowed:
            validated.append(
                {
                    "host_path": result.real_host_path,
                    "container_path": f"/workspace/extra/{result.resolved_container_path}",
                    "readonly": result.effective_readonly,
                }
            )
            logger.debug(
                "Mount validated successfully",
                group=group_name,
                host_path=result.real_host_path,
                container_path=result.resolved_container_path,
                readonly=result.effective_readonly,
                reason=result.reason,
            )
        else:
            logger.warning(
                "Additional mount REJECTED",
                group=group_name,
                requested_path=mount.host_path,
                container_path=mount.container_path,
                reason=result.reason,
            )

    return validated


def generate_allowlist_template() -> str:
    """Generate a template allowlist file for users to customize."""
    template = {
        "allowedRoots": [
            {"path": "~/projects", "allowReadWrite": True, "description": "Development projects"},
            {"path": "~/repos", "allowReadWrite": True, "description": "Git repositories"},
            {"path": "~/Documents/work", "allowReadWrite": False, "description": "Work documents (read-only)"},
        ],
        "blockedPatterns": ["password", "secret", "token"],
        "nonMainReadOnly": True,
    }
    return json.dumps(template, indent=2)
