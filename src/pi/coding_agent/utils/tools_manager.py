"""Tools manager — Python port of packages/coding-agent/src/utils/tools-manager.ts.

Manages external binary tools (fd, rg) — checks availability and downloads
them if missing.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import stat
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

ToolName = Literal["fd", "rg"]

# ---------------------------------------------------------------------------
# Download metadata
# ---------------------------------------------------------------------------

# Canonical download URLs for supported tools.
# The ``{version}`` placeholder is substituted at download time.
_TOOL_VERSIONS: dict[ToolName, str] = {
    "fd": "10.2.0",
    "rg": "14.1.1",
}


def _get_bin_dir() -> Path:
    """Return the pi bin directory, creating it if necessary."""
    try:
        from pi.coding_agent.config import get_bin_dir

        result = Path(get_bin_dir())
    except (ImportError, AttributeError):
        result = Path.home() / ".pi" / "bin"
    result.mkdir(parents=True, exist_ok=True)
    return result


def _current_platform() -> str:
    return sys.platform


def _current_arch() -> str:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    if machine in ("aarch64", "arm64"):
        return "aarch64"
    return machine


def _fd_download_url(version: str) -> str | None:
    plat = _current_platform()
    arch = _current_arch()
    if plat == "linux":
        return (
            f"https://github.com/sharkdp/fd/releases/download/v{version}/fd-v{version}-{arch}-unknown-linux-musl.tar.gz"
        )
    if plat == "darwin":
        return f"https://github.com/sharkdp/fd/releases/download/v{version}/fd-v{version}-{arch}-apple-darwin.tar.gz"
    if plat == "win32":
        return f"https://github.com/sharkdp/fd/releases/download/v{version}/fd-v{version}-{arch}-pc-windows-msvc.zip"
    return None


def _rg_download_url(version: str) -> str | None:
    plat = _current_platform()
    arch = _current_arch()
    if plat == "linux":
        return (
            f"https://github.com/BurntSushi/ripgrep/releases/download/{version}/"
            f"ripgrep-{version}-{arch}-unknown-linux-musl.tar.gz"
        )
    if plat == "darwin":
        return (
            f"https://github.com/BurntSushi/ripgrep/releases/download/{version}/"
            f"ripgrep-{version}-{arch}-apple-darwin.tar.gz"
        )
    if plat == "win32":
        return (
            f"https://github.com/BurntSushi/ripgrep/releases/download/{version}/"
            f"ripgrep-{version}-{arch}-pc-windows-msvc.zip"
        )
    return None


def _download_url(tool: ToolName) -> str | None:
    version = _TOOL_VERSIONS[tool]
    if tool == "fd":
        return _fd_download_url(version)
    if tool == "rg":
        return _rg_download_url(version)
    return None  # pragma: no cover


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_tool_path(tool: ToolName) -> str | None:
    """Return the absolute path to a tool binary, or None if not found.

    Checks the pi bin directory first, then PATH.
    """
    exe_name = tool + (".exe" if sys.platform == "win32" else "")
    # Check pi bin dir
    bin_dir = _get_bin_dir()
    local = bin_dir / exe_name
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    # Fall back to PATH
    found = shutil.which(tool)
    return found


async def ensure_tool(tool: ToolName, silent: bool = False) -> str | None:
    """Ensure a tool is available, downloading it if necessary.

    Args:
        tool: Tool name (``"fd"`` or ``"rg"``).
        silent: If True, suppress informational log messages.

    Returns:
        Absolute path to the tool binary, or None on failure.
    """
    existing = get_tool_path(tool)
    if existing is not None:
        return existing

    url = _download_url(tool)
    if url is None:
        if not silent:
            logger.warning("No download URL for tool %r on platform %s", tool, sys.platform)
        return None

    if not silent:
        logger.info("Downloading %s from %s", tool, url)

    try:
        import httpx
    except ImportError:
        logger.error("httpx is required to download tools. Install it with: pip install httpx")
        return None

    bin_dir = _get_bin_dir()
    exe_name = tool + (".exe" if sys.platform == "win32" else "")
    target = bin_dir / exe_name

    try:
        async with (
            httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client,
            client.stream("GET", url) as response,
        ):
            response.raise_for_status()
            with tempfile.NamedTemporaryFile(delete=False, suffix=_suffix(url)) as tmp:
                tmp_path = Path(tmp.name)
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    tmp.write(chunk)

        _extract_binary(tmp_path, tool, target)
        tmp_path.unlink(missing_ok=True)

        if not silent:
            logger.info("Installed %s at %s", tool, target)
        return str(target)

    except Exception:
        logger.exception("Failed to download %s", tool)
        return None


def _suffix(url: str) -> str:
    if url.endswith(".zip"):
        return ".zip"
    if url.endswith(".tar.gz"):
        return ".tar.gz"
    return ".bin"


def _extract_binary(archive_path: Path, tool: ToolName, target: Path) -> None:
    """Extract the tool binary from an archive and place it at target."""
    exe_name = tool + (".exe" if sys.platform == "win32" else "")
    archive_name = str(archive_path).lower()

    if archive_name.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as zf:
            for zip_member in zf.namelist():
                if zip_member.endswith(exe_name) or zip_member.endswith(f"/{exe_name}"):
                    data = zf.read(zip_member)
                    target.write_bytes(data)
                    _make_executable(target)
                    return
    elif archive_name.endswith(".tar.gz"):
        with tarfile.open(archive_path, "r:gz") as tf:
            for tar_member in tf.getmembers():
                if tar_member.name.endswith(exe_name) or tar_member.name == exe_name:
                    fileobj = tf.extractfile(tar_member)
                    if fileobj is not None:
                        target.write_bytes(fileobj.read())
                        _make_executable(target)
                        return
    else:
        # Assume it's a raw binary
        import shutil as _shutil

        _shutil.copy2(archive_path, target)
        _make_executable(target)


def _make_executable(path: Path) -> None:
    current = path.stat().st_mode
    path.chmod(current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


__all__ = [
    "ToolName",
    "ensure_tool",
    "get_tool_path",
]
