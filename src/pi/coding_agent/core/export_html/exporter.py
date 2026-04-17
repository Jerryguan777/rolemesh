"""HTML session exporter — Python port of packages/coding-agent/src/core/export-html/index.ts.

Exports a coding-agent session (list of messages) to a self-contained HTML file.
Because SessionManager does not yet exist in the Python codebase, its interface
is captured by ``SessionManagerProtocol``.
"""

from __future__ import annotations

import html
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pi.agent.types import AgentMessage
from pi.ai.types import (
    AssistantMessage,
    ImageContent,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    deserialize_message,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ToolHtmlRenderer protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ToolHtmlRenderer(Protocol):
    """Protocol for custom tool HTML rendering hooks."""

    def render_call(self, tool_name: str, args: dict[str, Any]) -> str | None:
        """Return HTML for a tool call, or None to use the default renderer."""
        ...

    def render_result(self, tool_name: str, result: Any, details: Any, is_error: bool) -> str | None:
        """Return HTML for a tool result, or None to use the default renderer."""
        ...


# ---------------------------------------------------------------------------
# ExportOptions
# ---------------------------------------------------------------------------


@dataclass
class ExportOptions:
    """Options controlling the HTML export."""

    output_path: str | None = None
    theme_name: str | None = None
    tool_renderer: ToolHtmlRenderer | None = None


# ---------------------------------------------------------------------------
# SessionManagerProtocol
# ---------------------------------------------------------------------------


@runtime_checkable
class SessionManagerProtocol(Protocol):
    """Minimal interface that export_session_to_html requires from a SessionManager."""

    def get_messages(self) -> list[AgentMessage]: ...
    def get_session_id(self) -> str: ...
    def get_session_name(self) -> str | None: ...


# ---------------------------------------------------------------------------
# Default HTML builder helpers
# ---------------------------------------------------------------------------

_CSS = (
    "body{font-family:system-ui,sans-serif;margin:0;padding:0;background:#0d1117;color:#c9d1d9;}"
    ".container{max-width:900px;margin:0 auto;padding:24px;}"
    "h1{color:#58a6ff;font-size:1.4rem;margin-bottom:24px;}"
    ".message{margin-bottom:16px;border-radius:8px;overflow:hidden;}"
    ".message-header{padding:8px 16px;font-size:.75rem;font-weight:600;"
    "text-transform:uppercase;letter-spacing:.05em;}"
    ".message-body{padding:12px 16px;white-space:pre-wrap;"
    "font-family:'Menlo',monospace;font-size:.85rem;line-height:1.6;}"
    ".user .message-header{background:#1f6feb;color:#fff;}"
    ".user .message-body{background:#161b22;}"
    ".assistant .message-header{background:#238636;color:#fff;}"
    ".assistant .message-body{background:#0d1117;border:1px solid #21262d;border-top:none;}"
    ".tool-call{background:#161b22;border:1px solid #30363d;border-radius:6px;margin:8px 0;padding:10px;}"
    ".tool-call-name{color:#f78166;font-weight:600;margin-bottom:4px;}"
    ".tool-result{background:#0d1117;border:1px solid #30363d;border-radius:6px;margin:8px 0;padding:10px;}"
    ".tool-result.error{border-color:#f85149;}"
    ".thinking{background:#161b22;border-left:3px solid #8b949e;"
    "padding:8px 12px;margin:8px 0;color:#8b949e;font-style:italic;}"
    ".image-block{margin:8px 0;}"
    ".image-block img{max-width:100%;border-radius:4px;}"
)

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>{title}</title>
<style>{css}</style>
</head>
<body>
<div class="container">
<h1>{title}</h1>
{messages_html}
</div>
</body>
</html>"""


def _escape(text: str) -> str:
    return html.escape(text)


def _render_content_block(block: Any, tool_renderer: ToolHtmlRenderer | None = None) -> str:
    if isinstance(block, TextContent):
        return f"<div>{_escape(block.text)}</div>"
    if isinstance(block, ThinkingContent):
        return f'<div class="thinking">{_escape(block.thinking)}</div>'
    if isinstance(block, ImageContent):
        safe_mime = _escape(block.mime_type)
        return f'<div class="image-block"><img src="data:{safe_mime};base64,{block.data}" alt="image"/></div>'
    if isinstance(block, ToolCall):
        custom: str | None = None
        if tool_renderer is not None:
            custom = tool_renderer.render_call(block.name, block.arguments)
        if custom is not None:
            return custom
        args_json = _escape(json.dumps(block.arguments, indent=2))
        return (
            f'<div class="tool-call">'
            f'<div class="tool-call-name">{_escape(block.name)}</div>'
            f"<pre>{args_json}</pre>"
            f"</div>"
        )
    return ""


def _render_tool_result(msg: ToolResultMessage, tool_renderer: ToolHtmlRenderer | None = None) -> str:
    custom: str | None = None
    content_texts = [b.text for b in msg.content if isinstance(b, TextContent)]
    result_text = "\n".join(content_texts)
    if tool_renderer is not None:
        custom = tool_renderer.render_result(msg.tool_name, result_text, msg.details, msg.is_error)
    if custom is not None:
        return custom
    error_class = " error" if msg.is_error else ""
    return (
        f'<div class="tool-result{error_class}">'
        f"<strong>Tool result: {_escape(msg.tool_name)}</strong>"
        f"<pre>{_escape(result_text)}</pre>"
        f"</div>"
    )


def _render_message(msg: AgentMessage, tool_renderer: ToolHtmlRenderer | None = None) -> str:
    if isinstance(msg, UserMessage):
        if isinstance(msg.content, str):
            body = _escape(msg.content)
        else:
            body = "".join(_render_content_block(b, tool_renderer) for b in msg.content)
        return (
            '<div class="message user">'
            '<div class="message-header">User</div>'
            f'<div class="message-body">{body}</div>'
            "</div>"
        )
    if isinstance(msg, AssistantMessage):
        body = "".join(_render_content_block(b, tool_renderer) for b in msg.content)
        return (
            '<div class="message assistant">'
            '<div class="message-header">Assistant</div>'
            f'<div class="message-body">{body}</div>'
            "</div>"
        )
    if isinstance(msg, ToolResultMessage):
        return _render_tool_result(msg, tool_renderer)
    return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def export_session_to_html(
    sm: SessionManagerProtocol,
    state: Any = None,
    options: ExportOptions | None = None,
) -> str:
    """Export a session to an HTML file.

    Args:
        sm: A session manager implementing ``SessionManagerProtocol``.
        state: Optional session state (unused currently; reserved for future use).
        options: Export configuration.

    Returns:
        Absolute path of the written HTML file.
    """
    opts = options or ExportOptions()
    messages = sm.get_messages()
    session_name = sm.get_session_name() or sm.get_session_id()
    title = f"Session: {session_name}"

    messages_html = "\n".join(_render_message(msg, opts.tool_renderer) for msg in messages)
    content = _HTML_TEMPLATE.format(
        title=_escape(title),
        css=_CSS,
        messages_html=messages_html,
    )

    if opts.output_path is not None:
        out_path = Path(opts.output_path)
    else:
        import tempfile

        suffix = f"_{session_name}.html".replace(" ", "_")
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, mode="w", encoding="utf-8") as tmp:
            tmp.write(content)
            return tmp.name

    out_path.write_text(content, encoding="utf-8")
    return str(out_path.resolve())


def export_from_file(input_path: str, options: ExportOptions | None = None) -> str:
    """Export a session file (JSON) to HTML.

    The session file must be a JSON file containing a list of messages in the
    standard serialised format (as produced by ``serialize_message``).

    Args:
        input_path: Path to the session JSON file.
        options: Export configuration.

    Returns:
        Absolute path of the written HTML file.
    """
    path = Path(input_path)
    raw_messages: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    messages: list[AgentMessage] = [deserialize_message(m) for m in raw_messages]
    session_id = path.stem

    class _SimpleSessionManager:
        def get_messages(self) -> list[AgentMessage]:
            return messages

        def get_session_id(self) -> str:
            return session_id

        def get_session_name(self) -> str | None:
            return None

    return export_session_to_html(_SimpleSessionManager(), options=options)


__all__ = [
    "ExportOptions",
    "SessionManagerProtocol",
    "ToolHtmlRenderer",
    "export_from_file",
    "export_session_to_html",
]
