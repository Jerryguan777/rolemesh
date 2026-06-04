"""WebSearch tool — search the web using Tavily API."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx

from pi.agent.types import AgentTool, AgentToolResult, AgentToolUpdateCallback
from pi.ai.types import TextContent

TAVILY_API_URL = "https://api.tavily.com/search"
DEFAULT_NUM_RESULTS = 5
TIMEOUT = 30


class WebSearchTool(AgentTool):
    """Search the web using Tavily API."""

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def label(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return (
            "Search the web for information using a search query. "
            "Returns relevant results with titles, URLs, and snippets."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query",
                },
                "num_results": {
                    "type": "integer",
                    "description": f"Number of results to return (default {DEFAULT_NUM_RESULTS})",
                },
            },
            "required": ["query"],
        }

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        signal: asyncio.Event | None = None,
        on_update: AgentToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        """Search the web using Tavily API."""
        query: str = params.get("query", "")
        num_results: int = int(params.get("num_results") or DEFAULT_NUM_RESULTS)

        if not query:
            return AgentToolResult(
                content=[TextContent(type="text", text="Error: query parameter is required")],
                details=None,
            )

        api_key = os.environ.get("TAVILY_API_KEY", "")
        if not api_key:
            return AgentToolResult(
                content=[
                    TextContent(
                        type="text",
                        text="Error: TAVILY_API_KEY environment variable is not set. "
                        "Get an API key from https://tavily.com and set it as TAVILY_API_KEY.",
                    )
                ],
                details=None,
            )

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                response = await client.post(
                    TAVILY_API_URL,
                    json={
                        "api_key": api_key,
                        "query": query,
                        "max_results": num_results,
                        "include_answer": True,
                    },
                )
                response.raise_for_status()
                data: dict[str, Any] = response.json()
        except httpx.TimeoutException:
            return AgentToolResult(
                content=[TextContent(type="text", text=f"Error: Search request timed out after {TIMEOUT}s")],
                details=None,
            )
        except httpx.HTTPStatusError as e:
            return AgentToolResult(
                content=[TextContent(type="text", text=f"Error: Tavily API returned HTTP {e.response.status_code}")],
                details=None,
            )
        except Exception as e:
            return AgentToolResult(
                content=[TextContent(type="text", text=f"Error searching: {e}")],
                details=None,
            )

        # Format results
        lines: list[str] = []

        answer = data.get("answer")
        if answer:
            lines.append(f"Answer: {answer}")
            lines.append("")

        results: list[dict[str, Any]] = data.get("results", [])
        for i, result in enumerate(results, 1):
            title = result.get("title", "No title")
            url = result.get("url", "")
            snippet = result.get("content", "")
            lines.append(f"{i}. [{title}]({url})")
            if snippet:
                lines.append(f"   {snippet}")
            lines.append("")

        if not results and not answer:
            lines.append("No results found.")

        output = "\n".join(lines).rstrip()

        return AgentToolResult(
            content=[TextContent(type="text", text=output)],
            details={"query": query, "num_results": len(results)},
        )
