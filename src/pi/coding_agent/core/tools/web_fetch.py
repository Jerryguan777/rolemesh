"""WebFetch tool — fetch a URL and return its content as markdown/text."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from markdownify import markdownify

from pi.agent.types import AgentTool, AgentToolResult, AgentToolUpdateCallback
from pi.ai.types import TextContent

from .truncate import truncate_tail

USER_AGENT = "PPI/1.0 (compatible)"
DEFAULT_MAX_LENGTH = 50000
TIMEOUT = 30


class WebFetchTool(AgentTool):
    """Fetch a URL and return its content."""

    @property
    def name(self) -> str:
        return "web_fetch"

    @property
    def label(self) -> str:
        return "web_fetch"

    @property
    def description(self) -> str:
        return (
            "Fetch the content of a URL and return it as markdown (for HTML) or plain text. "
            "Use this to read web pages, APIs, or other online resources."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch",
                },
                "max_length": {
                    "type": "integer",
                    "description": f"Maximum content length in characters (default {DEFAULT_MAX_LENGTH})",
                },
            },
            "required": ["url"],
        }

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        signal: asyncio.Event | None = None,
        on_update: AgentToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        """Fetch a URL and return its content."""
        url: str = params.get("url", "")
        max_length: int = int(params.get("max_length") or DEFAULT_MAX_LENGTH)

        if not url:
            return AgentToolResult(
                content=[TextContent(type="text", text="Error: url parameter is required")],
                details=None,
            )

        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=TIMEOUT,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
        except httpx.TimeoutException:
            return AgentToolResult(
                content=[TextContent(type="text", text=f"Error: Request timed out after {TIMEOUT}s for {url}")],
                details=None,
            )
        except httpx.HTTPStatusError as e:
            return AgentToolResult(
                content=[TextContent(type="text", text=f"Error: HTTP {e.response.status_code} for {url}")],
                details=None,
            )
        except Exception as e:
            return AgentToolResult(
                content=[TextContent(type="text", text=f"Error fetching {url}: {e}")],
                details=None,
            )

        content_type = response.headers.get("content-type", "")

        if "text/html" in content_type:
            text = markdownify(response.text, strip=["img", "script", "style"])
        elif "text/" in content_type or "application/json" in content_type or "application/xml" in content_type:
            text = response.text
        else:
            # Binary content — return metadata only
            size = len(response.content)
            text = f"Binary content: {content_type} ({size} bytes)"

        # Truncate if needed
        result = truncate_tail(text, max_lines=max_length // 50, max_bytes=max_length)
        output = result.content
        if result.truncated:
            output += f"\n\n[Content truncated: showed {result.output_bytes} of {result.total_bytes} bytes]"

        return AgentToolResult(
            content=[TextContent(type="text", text=output)],
            details={"url": url, "content_type": content_type, "truncated": result.truncated},
        )
