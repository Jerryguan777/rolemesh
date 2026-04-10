"""MIME type detection utilities.

Python port of packages/coding-agent/src/utils/mime.ts.
"""

from __future__ import annotations

from pathlib import Path

_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})

# Number of bytes to read for file type sniffing
_FILE_TYPE_SNIFF_BYTES = 4100

# Magic byte signatures for supported image types
_SIGNATURES: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),  # RIFF....WEBP (check further below)
]


async def detect_supported_image_mime_type_from_file(file_path: str) -> str | None:
    """Detect if a file is a supported image type by reading its magic bytes.

    Returns the MIME type string (e.g. 'image/png') or None if the file is
    not a recognized supported image format.
    """
    path = Path(file_path)
    try:
        data = path.read_bytes()[:_FILE_TYPE_SNIFF_BYTES]
    except OSError:
        return None

    if not data:
        return None

    for sig, mime in _SIGNATURES:
        if data.startswith(sig):
            # Special handling for RIFF container - must also contain WEBP
            if sig == b"RIFF":
                if len(data) >= 12 and data[8:12] == b"WEBP":
                    return "image/webp"
                continue
            return mime

    return None
