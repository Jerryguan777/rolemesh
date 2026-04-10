"""Clipboard utilities — Python port of packages/coding-agent/src/utils/clipboard.ts
and packages/coding-agent/src/utils/clipboard-image.ts.

Provides:
- copy_to_clipboard(text): write text to clipboard via OSC 52 and/or native tools
- read_clipboard_image(env, platform): read an image from the system clipboard
- is_wayland_session(env): detect Wayland display server
- extension_for_image_mime_type(mime_type): map MIME type to file extension
"""

from __future__ import annotations

import base64
import contextlib
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Protocol


class ClipboardModule(Protocol):
    """Protocol for native clipboard image access.

    Port of ClipboardModule from packages/coding-agent/src/utils/clipboard-native.ts.
    """

    def has_image(self) -> bool:
        """Return True if the clipboard contains an image."""
        ...

    async def get_image_binary(self) -> list[int]:
        """Return the clipboard image as a list of byte values."""
        ...


@dataclass
class ClipboardImage:
    """Raw clipboard image data with MIME type."""

    bytes: bytes
    mime_type: str


# ---------------------------------------------------------------------------
# MIME type helpers
# ---------------------------------------------------------------------------

_MIME_TO_EXT: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "image/tiff": "tiff",
    "image/svg+xml": "svg",
}


def extension_for_image_mime_type(mime_type: str) -> str | None:
    """Return a file extension (without leading dot) for the given MIME type.

    Returns ``None`` if the MIME type is not recognised.
    """
    return _MIME_TO_EXT.get(mime_type.lower())


# ---------------------------------------------------------------------------
# Wayland detection
# ---------------------------------------------------------------------------


def is_wayland_session(env: dict[str, str] | None = None) -> bool:
    """Return True if the current session is running under Wayland."""
    environment = env if env is not None else dict(os.environ)
    wayland_display = environment.get("WAYLAND_DISPLAY", "")
    xdg_session_type = environment.get("XDG_SESSION_TYPE", "")
    return bool(wayland_display) or xdg_session_type.lower() == "wayland"


# ---------------------------------------------------------------------------
# Copy to clipboard
# ---------------------------------------------------------------------------


def _write_osc52(text: str) -> None:
    """Write text to clipboard using the OSC 52 terminal escape sequence."""
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    # OSC 52 sequence: ESC ] 52 ; c ; <base64> BEL
    sequence = f"\033]52;c;{encoded}\007"
    sys.stdout.write(sequence)
    sys.stdout.flush()


def copy_to_clipboard(text: str) -> None:
    """Copy text to the system clipboard.

    Attempts, in order:
    1. OSC 52 terminal escape sequence (works in most modern terminals).
    2. ``wl-copy`` (Wayland).
    3. ``xclip`` (X11).
    4. ``xsel`` (X11 fallback).
    5. ``pbcopy`` (macOS).
    """
    # Always emit OSC 52 first (works transparently in many terminal emulators)
    with contextlib.suppress(Exception):
        _write_osc52(text)

    # Try native tools as well so the clipboard works outside the terminal
    encoded_bytes = text.encode("utf-8")
    native_commands: list[list[str]] = []

    if is_wayland_session():
        native_commands.append(["wl-copy"])
    if sys.platform == "darwin":
        native_commands.append(["pbcopy"])
    else:
        native_commands.extend(
            [
                ["xclip", "-selection", "clipboard"],
                ["xsel", "--clipboard", "--input"],
            ]
        )

    for cmd in native_commands:
        try:
            result = subprocess.run(cmd, input=encoded_bytes, timeout=5, capture_output=True)
            if result.returncode == 0:
                return
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue


# ---------------------------------------------------------------------------
# Read clipboard image
# ---------------------------------------------------------------------------


def _detect_mime_from_bytes(data: bytes) -> str:
    """Detect image MIME type from the first few bytes (magic bytes)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"  # default fallback


def read_clipboard_image(
    env: dict[str, str] | None = None,
    platform: str | None = None,
) -> ClipboardImage | None:
    """Read an image from the system clipboard.

    Supports:
    - Wayland via ``wl-paste --type image/png``
    - X11 via ``xclip -selection clipboard -t image/png -o``
    - macOS via ``osascript`` (reads PNG from clipboard)

    Returns ``None`` if no image is available or the platform is unsupported.
    """
    current_platform = platform if platform is not None else sys.platform
    environment = env if env is not None else dict(os.environ)

    if current_platform == "darwin":
        return _read_clipboard_image_macos()
    # Linux
    if is_wayland_session(environment):
        return _read_clipboard_image_wayland()
    return _read_clipboard_image_x11()


def _read_clipboard_image_wayland() -> ClipboardImage | None:
    # Try PNG first, then JPEG
    for mime_type in ("image/png", "image/jpeg"):
        try:
            result = subprocess.run(
                ["wl-paste", "--type", mime_type],
                capture_output=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout:
                return ClipboardImage(bytes=result.stdout, mime_type=mime_type)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            break
    return None


def _read_clipboard_image_x11() -> ClipboardImage | None:
    for mime_type in ("image/png", "image/jpeg"):
        for tool in (
            ["xclip", "-selection", "clipboard", "-t", mime_type, "-o"],
            ["xsel", "--clipboard", "--output"],
        ):
            try:
                result = subprocess.run(tool, capture_output=True, timeout=10)
                if result.returncode == 0 and result.stdout:
                    detected = _detect_mime_from_bytes(result.stdout)
                    return ClipboardImage(bytes=result.stdout, mime_type=detected)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
    return None


def _read_clipboard_image_macos() -> ClipboardImage | None:
    # Use osascript to write clipboard PNG to a temp file then read it
    script = (
        'set theFile to (POSIX path of (path to temporary items folder)) & "clipboard_img.png"\n'
        "try\n"
        "    set theData to the clipboard as «class PNGf»\n"
        "    set theFileRef to open for access (POSIX file theFile) with write permission\n"
        "    set eof of theFileRef to 0\n"
        "    write theData to theFileRef\n"
        "    close access theFileRef\n"
        "    return theFile\n"
        "on error\n"
        '    return ""\n'
        "end try"
    )
    try:
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
        path_str = result.stdout.strip()
        if result.returncode == 0 and path_str:
            import pathlib

            path = pathlib.Path(path_str)
            if path.exists():
                data = path.read_bytes()
                path.unlink(missing_ok=True)
                if data:
                    return ClipboardImage(bytes=data, mime_type="image/png")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


__all__ = [
    "ClipboardImage",
    "copy_to_clipboard",
    "extension_for_image_mime_type",
    "is_wayland_session",
    "read_clipboard_image",
]
