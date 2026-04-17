"""ANSI escape code to HTML converter.

Python port of packages/coding-agent/src/core/export-html/ansi-to-html.ts.

Converts terminal ANSI color/style codes to HTML with inline styles.
Supports:
- Standard foreground colors (30-37) and bright variants (90-97)
- Standard background colors (40-47) and bright variants (100-107)
- 256-color palette (38;5;N and 48;5;N)
- RGB true color (38;2;R;G;B and 48;2;R;G;B)
- Text styles: bold (1), dim (2), italic (3), underline (4)
- Reset (0)
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

# Standard ANSI color palette (0-15)
_ANSI_COLORS = [
    "#000000",  # 0: black
    "#800000",  # 1: red
    "#008000",  # 2: green
    "#808000",  # 3: yellow
    "#000080",  # 4: blue
    "#800080",  # 5: magenta
    "#008080",  # 6: cyan
    "#c0c0c0",  # 7: white
    "#808080",  # 8: bright black
    "#ff0000",  # 9: bright red
    "#00ff00",  # 10: bright green
    "#ffff00",  # 11: bright yellow
    "#0000ff",  # 12: bright blue
    "#ff00ff",  # 13: bright magenta
    "#00ffff",  # 14: bright cyan
    "#ffffff",  # 15: bright white
]

# Match ANSI escape sequences: ESC[ followed by params and ending with 'm'
_ANSI_REGEX = re.compile(r"\x1b\[([\d;]*)m")


def _color_256_to_hex(index: int) -> str:
    """Convert a 256-color index to a hex color string."""
    # Standard colors (0-15)
    if index < 16:
        return _ANSI_COLORS[index]

    # Color cube (16-231): 6x6x6 = 216 colors
    if index < 232:
        cube_index = index - 16
        r = cube_index // 36
        g = (cube_index % 36) // 6
        b = cube_index % 6

        def to_component(n: int) -> int:
            return 0 if n == 0 else 55 + n * 40

        return f"#{to_component(r):02x}{to_component(g):02x}{to_component(b):02x}"

    # Grayscale (232-255): 24 shades
    gray = 8 + (index - 232) * 10
    return f"#{gray:02x}{gray:02x}{gray:02x}"


@dataclass
class _TextStyle:
    """Current text styling state."""

    fg: str | None = None
    bg: str | None = None
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False


def _style_to_inline_css(style: _TextStyle) -> str:
    """Convert a TextStyle to inline CSS."""
    parts: list[str] = []
    if style.fg:
        parts.append(f"color:{style.fg}")
    if style.bg:
        parts.append(f"background-color:{style.bg}")
    if style.bold:
        parts.append("font-weight:bold")
    if style.dim:
        parts.append("opacity:0.6")
    if style.italic:
        parts.append("font-style:italic")
    if style.underline:
        parts.append("text-decoration:underline")
    return ";".join(parts)


def _has_style(style: _TextStyle) -> bool:
    """Check if a style has any active properties."""
    return style.fg is not None or style.bg is not None or style.bold or style.dim or style.italic or style.underline


def _apply_sgr_code(params: list[int], style: _TextStyle) -> None:
    """Parse ANSI SGR (Select Graphic Rendition) codes and update style in-place."""
    i = 0
    while i < len(params):
        code = params[i]

        if code == 0:
            # Reset all
            style.fg = None
            style.bg = None
            style.bold = False
            style.dim = False
            style.italic = False
            style.underline = False
        elif code == 1:
            style.bold = True
        elif code == 2:
            style.dim = True
        elif code == 3:
            style.italic = True
        elif code == 4:
            style.underline = True
        elif code == 22:
            # Reset bold/dim
            style.bold = False
            style.dim = False
        elif code == 23:
            style.italic = False
        elif code == 24:
            style.underline = False
        elif 30 <= code <= 37:
            # Standard foreground colors
            style.fg = _ANSI_COLORS[code - 30]
        elif code == 38:
            # Extended foreground color
            if i + 2 < len(params) and params[i + 1] == 5:
                # 256-color: 38;5;N
                style.fg = _color_256_to_hex(params[i + 2])
                i += 2
            elif i + 4 < len(params) and params[i + 1] == 2:
                # RGB: 38;2;R;G;B
                r, g, b = params[i + 2], params[i + 3], params[i + 4]
                style.fg = f"rgb({r},{g},{b})"
                i += 4
        elif code == 39:
            # Default foreground
            style.fg = None
        elif 40 <= code <= 47:
            # Standard background colors
            style.bg = _ANSI_COLORS[code - 40]
        elif code == 48:
            # Extended background color
            if i + 2 < len(params) and params[i + 1] == 5:
                # 256-color: 48;5;N
                style.bg = _color_256_to_hex(params[i + 2])
                i += 2
            elif i + 4 < len(params) and params[i + 1] == 2:
                # RGB: 48;2;R;G;B
                r, g, b = params[i + 2], params[i + 3], params[i + 4]
                style.bg = f"rgb({r},{g},{b})"
                i += 4
        elif code == 49:
            # Default background
            style.bg = None
        elif 90 <= code <= 97:
            # Bright foreground colors
            style.fg = _ANSI_COLORS[code - 90 + 8]
        elif 100 <= code <= 107:
            # Bright background colors
            style.bg = _ANSI_COLORS[code - 100 + 8]
        # Ignore unrecognized codes

        i += 1


def ansi_to_html(text: str) -> str:
    """Convert ANSI-escaped text to HTML with inline styles."""
    style = _TextStyle()
    result: list[str] = []
    last_index = 0
    in_span = False

    for match in _ANSI_REGEX.finditer(text):
        # Add text before this escape sequence
        before_text = text[last_index : match.start()]
        if before_text:
            result.append(html.escape(before_text))

        # Parse SGR parameters
        param_str = match.group(1)
        params = [int(p) if p else 0 for p in param_str.split(";")] if param_str else [0]

        # Close existing span if we have one
        if in_span:
            result.append("</span>")
            in_span = False

        # Apply the codes
        _apply_sgr_code(params, style)

        # Open new span if we have any styling
        if _has_style(style):
            result.append(f'<span style="{_style_to_inline_css(style)}">')
            in_span = True

        last_index = match.end()

    # Add remaining text
    remaining_text = text[last_index:]
    if remaining_text:
        result.append(html.escape(remaining_text))

    # Close any open span
    if in_span:
        result.append("</span>")

    return "".join(result)


def ansi_lines_to_html(lines: list[str]) -> str:
    """Convert array of ANSI-escaped lines to HTML.

    Each line is wrapped in a div element.
    """
    parts: list[str] = []
    for line in lines:
        converted = ansi_to_html(line)
        content = converted if converted else "&nbsp;"
        parts.append(f'<div class="ansi-line">{content}</div>')
    return "\n".join(parts)
