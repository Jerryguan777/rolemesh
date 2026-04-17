"""Image utilities — Python port of packages/coding-agent/src/utils/image-convert.ts
and packages/coding-agent/src/utils/image-resize.ts.

Requires: Pillow (PIL)
"""

from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass

from pi.ai.types import ImageContent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# convert_to_png
# ---------------------------------------------------------------------------


def convert_to_png(base64_data: str, mime_type: str) -> dict[str, str] | None:
    """Convert a base64-encoded image to PNG format.

    Args:
        base64_data: Base64-encoded image data (no data URL prefix).
        mime_type: MIME type of the source image (e.g. ``"image/jpeg"``).

    Returns:
        A dict with keys ``data`` (base64 PNG) and ``mimeType`` (``"image/png"``),
        or ``None`` if conversion fails.
    """
    try:
        from PIL import Image
    except ImportError:
        logger.error("Pillow is required for image conversion. Install it with: pip install Pillow")
        return None

    try:
        raw_bytes = base64.b64decode(base64_data)
        img = Image.open(io.BytesIO(raw_bytes))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return {"data": png_b64, "mimeType": "image/png"}
    except Exception:
        logger.exception("Failed to convert image to PNG")
        return None


# ---------------------------------------------------------------------------
# ImageResizeOptions and ResizedImage
# ---------------------------------------------------------------------------


@dataclass
class ImageResizeOptions:
    """Options controlling how an image is resized."""

    max_width: int | None = None
    max_height: int | None = None
    max_bytes: int | None = None
    jpeg_quality: int | None = None


@dataclass
class ResizedImage:
    """Result of a resize operation, including dimension metadata."""

    data: str  # base64-encoded
    mime_type: str
    original_width: int
    original_height: int
    width: int
    height: int
    was_resized: bool


# ---------------------------------------------------------------------------
# resize_image
# ---------------------------------------------------------------------------


def resize_image(img: ImageContent, options: ImageResizeOptions | None = None) -> ResizedImage:
    """Resize an image according to the given options.

    Uses Pillow for all image operations.  If no resize is needed the original
    data is returned unchanged (``was_resized=False``).

    Args:
        img: The source image as an ``ImageContent`` dataclass.
        options: Resize constraints.  All fields are optional.

    Returns:
        A ``ResizedImage`` describing the result.

    Raises:
        RuntimeError: If Pillow is not installed or the image cannot be decoded.
    """
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow is required for image resizing. Install it with: pip install Pillow") from exc

    raw_bytes = base64.b64decode(img.data)
    pil_img: Image.Image = Image.open(io.BytesIO(raw_bytes))
    original_width, original_height = pil_img.size
    opts = options or ImageResizeOptions()

    target_width = original_width
    target_height = original_height

    # Compute scale factor from dimension constraints
    scale = 1.0
    if opts.max_width is not None and target_width > opts.max_width:
        scale = min(scale, opts.max_width / target_width)
    if opts.max_height is not None and target_height > opts.max_height:
        scale = min(scale, opts.max_height / target_height)

    if scale < 1.0:
        target_width = max(1, int(original_width * scale))
        target_height = max(1, int(original_height * scale))
        pil_img = pil_img.resize(
            (target_width, target_height),
            Image.Resampling.LANCZOS,
        )

    # Determine output format
    output_format = "PNG"
    output_mime = "image/png"
    jpeg_quality = opts.jpeg_quality if opts.jpeg_quality is not None else 85

    if img.mime_type in ("image/jpeg", "image/jpg"):
        output_format = "JPEG"
        output_mime = "image/jpeg"
        # Ensure no alpha channel for JPEG
        if pil_img.mode in ("RGBA", "LA", "P"):
            pil_img = pil_img.convert("RGB")

    def _encode(pil_image: Image.Image) -> bytes:
        buf = io.BytesIO()
        if output_format == "JPEG":
            pil_image.save(buf, format="JPEG", quality=jpeg_quality)
        else:
            pil_image.save(buf, format="PNG")
        return buf.getvalue()

    encoded_bytes = _encode(pil_img)

    # If max_bytes constraint is specified, reduce quality until it fits
    if opts.max_bytes is not None and len(encoded_bytes) > opts.max_bytes:
        if output_format == "JPEG":
            quality = jpeg_quality
            while quality > 10 and len(encoded_bytes) > opts.max_bytes:
                quality -= 10
                buf2 = io.BytesIO()
                pil_img.save(buf2, format="JPEG", quality=quality)
                encoded_bytes = buf2.getvalue()
        else:
            # For PNG, resize down further
            reduction = 0.9
            temp_img = pil_img
            while len(encoded_bytes) > opts.max_bytes and reduction > 0.1:
                new_w = max(1, int(target_width * reduction))
                new_h = max(1, int(target_height * reduction))
                temp_img = pil_img.resize(
                    (new_w, new_h),
                    Image.Resampling.LANCZOS,
                )
                encoded_bytes = _encode(temp_img)
                reduction -= 0.1
            target_width, target_height = temp_img.size

    was_resized = (target_width != original_width) or (target_height != original_height)
    b64_result = base64.b64encode(encoded_bytes).decode("ascii")

    return ResizedImage(
        data=b64_result,
        mime_type=output_mime,
        original_width=original_width,
        original_height=original_height,
        width=target_width,
        height=target_height,
        was_resized=was_resized,
    )


# ---------------------------------------------------------------------------
# format_dimension_note
# ---------------------------------------------------------------------------


def format_dimension_note(result: ResizedImage) -> str | None:
    """Return a human-readable note about resizing, or None if not resized."""
    if not result.was_resized:
        return None
    return f"Image resized from {result.original_width}x{result.original_height} to {result.width}x{result.height}"


__all__ = [
    "ImageResizeOptions",
    "ResizedImage",
    "convert_to_png",
    "format_dimension_note",
    "resize_image",
]
