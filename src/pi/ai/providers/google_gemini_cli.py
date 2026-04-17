"""Google Gemini CLI / Antigravity provider.

Shared implementation for both google-gemini-cli and google-antigravity providers.
Uses the Cloud Code Assist API endpoint to access Gemini and Claude models.

Ported from packages/ai/src/providers/google-gemini-cli.ts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from pi.ai.event_stream import AssistantMessageEventStream
from pi.ai.models import calculate_cost
from pi.ai.providers.google_shared import (
    convert_messages,
    convert_tools,
    generate_tool_call_id,
    is_thinking_part,
    map_stop_reason_string,
    map_tool_choice,
    retain_thought_signature,
)
from pi.ai.providers.simple_options import build_base_options, clamp_reasoning
from pi.ai.types import (
    AssistantMessage,
    Context,
    DoneEvent,
    ErrorEvent,
    Model,
    SimpleStreamOptions,
    StartEvent,
    StreamOptions,
    TextContent,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ThinkingBudgets,
    ThinkingContent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingLevel,
    ThinkingStartEvent,
    ToolCall,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    Usage,
    UsageCost,
)
from pi.ai.utils.sanitize_unicode import sanitize_surrogates

# ---------------------------------------------------------------------------
# Public type aliases
# ---------------------------------------------------------------------------

GoogleThinkingLevel = Literal[
    "THINKING_LEVEL_UNSPECIFIED",
    "MINIMAL",
    "LOW",
    "MEDIUM",
    "HIGH",
]


@dataclass
class _ThinkingConfig:
    """Thinking/reasoning configuration for Gemini CLI requests."""

    enabled: bool = False
    budget_tokens: int | None = None
    level: GoogleThinkingLevel | None = None


@dataclass
class GoogleGeminiCliOptions(StreamOptions):
    """Options for the Google Gemini CLI / Antigravity stream functions."""

    tool_choice: Literal["auto", "none", "any"] | None = None
    thinking: _ThinkingConfig = field(default_factory=_ThinkingConfig)
    project_id: str | None = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_ENDPOINT = "https://cloudcode-pa.googleapis.com"
ANTIGRAVITY_DAILY_ENDPOINT = "https://daily-cloudcode-pa.sandbox.googleapis.com"
ANTIGRAVITY_ENDPOINT_FALLBACKS: tuple[str, ...] = (ANTIGRAVITY_DAILY_ENDPOINT, DEFAULT_ENDPOINT)

GEMINI_CLI_HEADERS: dict[str, str] = {
    "User-Agent": "google-cloud-sdk vscode_cloudshelleditor/0.1",
    "X-Goog-Api-Client": "gl-node/22.17.0",
    "Client-Metadata": json.dumps(
        {
            "ideType": "IDE_UNSPECIFIED",
            "platform": "PLATFORM_UNSPECIFIED",
            "pluginType": "GEMINI",
        }
    ),
}

DEFAULT_ANTIGRAVITY_VERSION = "1.15.8"

ANTIGRAVITY_SYSTEM_INSTRUCTION = (
    "You are Antigravity, a powerful agentic AI coding assistant designed by the Google Deepmind team "
    "working on Advanced Agentic Coding."
    "You are pair programming with a USER to solve their coding task. The task may require creating a new "
    "codebase, modifying or debugging an existing codebase, or simply answering a question."
    "**Absolute paths only**"
    "**Proactiveness**"
)

MAX_RETRIES = 3
BASE_DELAY_MS = 1000
MAX_EMPTY_STREAM_RETRIES = 2
EMPTY_STREAM_BASE_DELAY_MS = 500
CLAUDE_THINKING_BETA_HEADER = "interleaved-thinking-2025-05-14"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_antigravity_headers() -> dict[str, str]:
    version = os.environ.get("PI_AI_ANTIGRAVITY_VERSION", DEFAULT_ANTIGRAVITY_VERSION)
    return {
        "User-Agent": f"antigravity/{version} darwin/arm64",
        "X-Goog-Api-Client": "google-cloud-sdk vscode_cloudshelleditor/0.1",
        "Client-Metadata": json.dumps(
            {
                "ideType": "IDE_UNSPECIFIED",
                "platform": "PLATFORM_UNSPECIFIED",
                "pluginType": "GEMINI",
            }
        ),
    }


def _is_claude_thinking_model(model_id: str) -> bool:
    normalized = model_id.lower()
    return "claude" in normalized and "thinking" in normalized


def _is_retryable_error(status: int, error_text: str) -> bool:
    """Check if an error is retryable (rate limit, server error, network error, etc.)."""
    if status in (429, 500, 502, 503, 504):
        return True
    pattern = r"resource.?exhausted|rate.?limit|overloaded|service.?unavailable|other.?side.?closed"
    return bool(re.search(pattern, error_text, re.IGNORECASE))


def _extract_error_message(error_text: str) -> str:
    """Extract a clean, user-friendly error message from Google API error response."""
    try:
        parsed = json.loads(error_text)
        if isinstance(parsed, dict):
            error_obj = parsed.get("error")
            if isinstance(error_obj, dict):
                message = error_obj.get("message")
                if isinstance(message, str):
                    return message
    except (json.JSONDecodeError, TypeError):
        pass
    return error_text


async def _sleep(ms: float, signal: asyncio.Event | None = None) -> None:
    """Sleep for *ms* milliseconds, raising if *signal* is already set."""
    if signal is not None and signal.is_set():
        raise RuntimeError("Request was aborted")
    seconds = ms / 1000.0

    if signal is None:
        await asyncio.sleep(seconds)
        return

    # Race between the sleep and the abort signal
    sleep_task = asyncio.ensure_future(asyncio.sleep(seconds))
    abort_task = asyncio.ensure_future(signal.wait())
    done, pending = await asyncio.wait({sleep_task, abort_task}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    if abort_task in done:
        raise RuntimeError("Request was aborted")


def _get_gemini_cli_thinking_level(effort: ThinkingLevel, model_id: str) -> GoogleThinkingLevel:
    """Map a clamped ThinkingLevel to the Google-specific GoogleThinkingLevel enum string."""
    if "3-pro" in model_id:
        if effort in ("minimal", "low"):
            return "LOW"
        return "HIGH"
    mapping: dict[str, GoogleThinkingLevel] = {
        "minimal": "MINIMAL",
        "low": "LOW",
        "medium": "MEDIUM",
        "high": "HIGH",
    }
    return mapping.get(effort, "HIGH")


# ---------------------------------------------------------------------------
# extract_retry_delay (public)
# ---------------------------------------------------------------------------


def extract_retry_delay(
    error_text: str,
    headers: dict[str, str] | httpx.Headers | None = None,
) -> int | None:
    """Extract retry delay from Gemini error response (in milliseconds).

    Checks headers first (Retry-After, x-ratelimit-reset, x-ratelimit-reset-after),
    then parses body patterns like:
    - "Your quota will reset after 39s"
    - "Your quota will reset after 18h31m10s"
    - "Please retry in Xs" or "Please retry in Xms"
    - "retryDelay": "34.074824224s" (JSON field)

    Returns the delay in milliseconds with a 1000ms buffer, or None if no delay found.
    """

    def _normalize_delay(ms: float) -> int | None:
        if ms > 0:
            return math.ceil(ms + 1000)
        return None

    if headers is not None:
        # Normalize header access (httpx.Headers is case-insensitive, dicts are not)
        def _get_header(name: str) -> str | None:
            if isinstance(headers, httpx.Headers):
                val: str | None = headers.get(name)
                return val
            assert isinstance(headers, dict)
            # Try exact, then lowercase
            val = headers.get(name)
            if val is None:
                val = headers.get(name.lower())
            return val

        # retry-after header (seconds or HTTP-date)
        retry_after = _get_header("retry-after")
        if retry_after is not None:
            try:
                retry_after_seconds = float(retry_after)
                if math.isfinite(retry_after_seconds):
                    delay = _normalize_delay(retry_after_seconds * 1000)
                    if delay is not None:
                        return delay
            except ValueError:
                pass
            # Try parsing as HTTP-date (RFC 7231)
            try:
                from email.utils import parsedate_to_datetime

                dt = parsedate_to_datetime(retry_after)
                retry_after_ms = dt.timestamp() * 1000
                delay = _normalize_delay(retry_after_ms - time.time() * 1000)
                if delay is not None:
                    return delay
            except (ValueError, TypeError):
                pass

        # x-ratelimit-reset (unix timestamp in seconds)
        rate_limit_reset = _get_header("x-ratelimit-reset")
        if rate_limit_reset is not None:
            try:
                reset_seconds = int(rate_limit_reset)
                delay = _normalize_delay(reset_seconds * 1000 - time.time() * 1000)
                if delay is not None:
                    return delay
            except ValueError:
                pass

        # x-ratelimit-reset-after (seconds)
        rate_limit_reset_after = _get_header("x-ratelimit-reset-after")
        if rate_limit_reset_after is not None:
            try:
                reset_after_seconds = float(rate_limit_reset_after)
                if math.isfinite(reset_after_seconds):
                    delay = _normalize_delay(reset_after_seconds * 1000)
                    if delay is not None:
                        return delay
            except ValueError:
                pass

    # Pattern 1: "Your quota will reset after ..." (formats: "18h31m10s", "10m15s", "6s", "39s")
    duration_match = re.search(r"reset after (?:(\d+)h)?(?:(\d+)m)?(\d+(?:\.\d+)?)s", error_text, re.IGNORECASE)
    if duration_match:
        hours = int(duration_match.group(1)) if duration_match.group(1) else 0
        minutes = int(duration_match.group(2)) if duration_match.group(2) else 0
        try:
            seconds = float(duration_match.group(3))
            total_ms = ((hours * 60 + minutes) * 60 + seconds) * 1000
            delay = _normalize_delay(total_ms)
            if delay is not None:
                return delay
        except ValueError:
            pass

    # Pattern 2: "Please retry in X[ms|s]"
    retry_in_match = re.search(r"Please retry in ([0-9.]+)(ms|s)", error_text, re.IGNORECASE)
    if retry_in_match:
        try:
            value = float(retry_in_match.group(1))
            if value > 0:
                ms = value if retry_in_match.group(2).lower() == "ms" else value * 1000
                delay = _normalize_delay(ms)
                if delay is not None:
                    return delay
        except ValueError:
            pass

    # Pattern 3: "retryDelay": "34.074824224s" (JSON field in error details)
    retry_delay_match = re.search(r'"retryDelay":\s*"([0-9.]+)(ms|s)"', error_text, re.IGNORECASE)
    if retry_delay_match:
        try:
            value = float(retry_delay_match.group(1))
            if value > 0:
                ms = value if retry_delay_match.group(2).lower() == "ms" else value * 1000
                delay = _normalize_delay(ms)
                if delay is not None:
                    return delay
        except ValueError:
            pass

    return None


# ---------------------------------------------------------------------------
# build_request (public)
# ---------------------------------------------------------------------------


def build_request(
    model: Model,
    context: Context,
    project_id: str,
    options: GoogleGeminiCliOptions | None = None,
    is_antigravity: bool = False,
) -> dict[str, Any]:
    """Build the Cloud Code Assist request body."""
    if options is None:
        options = GoogleGeminiCliOptions()

    contents = convert_messages(model, context)

    generation_config: dict[str, Any] = {}
    if options.temperature is not None:
        generation_config["temperature"] = options.temperature
    if options.max_tokens is not None:
        generation_config["maxOutputTokens"] = options.max_tokens

    # Thinking config
    if options.thinking.enabled and model.reasoning:
        thinking_config: dict[str, Any] = {"includeThoughts": True}
        if options.thinking.level is not None:
            thinking_config["thinkingLevel"] = options.thinking.level
        elif options.thinking.budget_tokens is not None:
            thinking_config["thinkingBudget"] = options.thinking.budget_tokens
        generation_config["thinkingConfig"] = thinking_config

    request: dict[str, Any] = {"contents": contents}

    if options.session_id is not None:
        request["sessionId"] = options.session_id

    # System instruction must be object with parts, not plain string
    if context.system_prompt:
        request["systemInstruction"] = {
            "parts": [{"text": sanitize_surrogates(context.system_prompt)}],
        }

    if generation_config:
        request["generationConfig"] = generation_config

    if context.tools and len(context.tools) > 0:
        # Claude models on Cloud Code Assist need the legacy `parameters` field;
        # the API translates it into Anthropic's `input_schema`.
        use_parameters = model.id.startswith("claude-")
        converted = convert_tools(context.tools, use_parameters)
        if converted is not None:
            request["tools"] = converted
        if options.tool_choice is not None:
            request["toolConfig"] = {
                "functionCallingConfig": {
                    "mode": map_tool_choice(options.tool_choice),
                },
            }

    if is_antigravity:
        existing_parts: list[dict[str, Any]] = []
        sys_instr = request.get("systemInstruction")
        if isinstance(sys_instr, dict):
            existing_parts = sys_instr.get("parts", [])
        request["systemInstruction"] = {
            "role": "user",
            "parts": [
                {"text": ANTIGRAVITY_SYSTEM_INSTRUCTION},
                {"text": f"Please ignore following [ignore]{ANTIGRAVITY_SYSTEM_INSTRUCTION}[/ignore]"},
                *existing_parts,
            ],
        }

    result: dict[str, Any] = {
        "project": project_id,
        "model": model.id,
        "request": request,
        "userAgent": "antigravity" if is_antigravity else "pi-coding-agent",
        "requestId": f"{'agent' if is_antigravity else 'pi'}-{int(time.time() * 1000)}-{os.urandom(5).hex()}",
    }
    if is_antigravity:
        result["requestType"] = "agent"

    return result


# ---------------------------------------------------------------------------
# stream_google_gemini_cli (public)
# ---------------------------------------------------------------------------


def stream_google_gemini_cli(
    model: Model,
    context: Context,
    options: GoogleGeminiCliOptions | None = None,
) -> AssistantMessageEventStream:
    """Stream a response from the Google Cloud Code Assist API.

    Returns an AssistantMessageEventStream that yields streaming events.
    """
    stream = AssistantMessageEventStream()

    async def _run() -> None:
        output = AssistantMessage(
            role="assistant",
            content=[],
            api="google-gemini-cli",
            provider=model.provider,
            model=model.id,
            usage=Usage(),
            stop_reason="stop",
            timestamp=int(time.time() * 1000),
        )

        signal = options.signal if options else None

        try:
            # apiKey is JSON-encoded: { token, projectId }
            api_key_raw = options.api_key if options else None
            if not api_key_raw:
                raise RuntimeError(
                    "Google Cloud Code Assist requires OAuth authentication. Use /login to authenticate."
                )

            try:
                parsed_key = json.loads(api_key_raw)
                access_token: str = parsed_key["token"]
                project_id: str = parsed_key["projectId"]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise RuntimeError(
                    "Invalid Google Cloud Code Assist credentials. Use /login to re-authenticate."
                ) from exc

            if not access_token or not project_id:
                raise RuntimeError(
                    "Missing token or projectId in Google Cloud credentials. Use /login to re-authenticate."
                )

            is_antigravity = model.provider == "google-antigravity"
            base_url = (model.base_url or "").strip() or None
            if base_url:
                endpoints: tuple[str, ...] = (base_url,)
            elif is_antigravity:
                endpoints = ANTIGRAVITY_ENDPOINT_FALLBACKS
            else:
                endpoints = (DEFAULT_ENDPOINT,)

            request_body = build_request(model, context, project_id, options, is_antigravity)
            if options and options.on_payload:
                options.on_payload(request_body)

            provider_headers = _get_antigravity_headers() if is_antigravity else dict(GEMINI_CLI_HEADERS)

            request_headers: dict[str, str] = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                **provider_headers,
            }
            if _is_claude_thinking_model(model.id):
                request_headers["anthropic-beta"] = CLAUDE_THINKING_BETA_HEADER
            if options and options.headers:
                request_headers.update(options.headers)

            request_body_json = json.dumps(request_body)

            # Fetch with retry logic for rate limits and transient errors
            response: httpx.Response | None = None
            last_error: Exception | None = None
            request_url: str | None = None

            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout=300.0)) as client:
                for attempt in range(MAX_RETRIES + 1):
                    if signal is not None and signal.is_set():
                        raise RuntimeError("Request was aborted")

                    try:
                        endpoint = endpoints[min(attempt, len(endpoints) - 1)]
                        request_url = f"{endpoint}/v1internal:streamGenerateContent?alt=sse"
                        response = await client.send(
                            client.build_request(
                                "POST",
                                request_url,
                                headers=request_headers,
                                content=request_body_json,
                            ),
                            stream=True,
                        )

                        if response.status_code >= 200 and response.status_code < 300:
                            break  # Success, exit retry loop

                        error_text = (await response.aread()).decode("utf-8", errors="replace")
                        await response.aclose()

                        # Check if retryable
                        if attempt < MAX_RETRIES and _is_retryable_error(response.status_code, error_text):
                            server_delay = extract_retry_delay(error_text, response.headers)
                            delay_ms = server_delay if server_delay is not None else BASE_DELAY_MS * (2**attempt)

                            # Check if server delay exceeds max allowed (default: 60s)
                            max_delay_ms = (options.max_retry_delay_ms if options else None) or 60000
                            if max_delay_ms > 0 and server_delay is not None and server_delay > max_delay_ms:
                                delay_seconds = math.ceil(server_delay / 1000)
                                raise RuntimeError(
                                    f"Server requested {delay_seconds}s retry delay "
                                    f"(max: {math.ceil(max_delay_ms / 1000)}s). "
                                    f"{_extract_error_message(error_text)}"
                                )

                            await _sleep(delay_ms, signal)
                            response = None
                            continue

                        # Not retryable or max retries exceeded
                        raise RuntimeError(
                            f"Cloud Code Assist API error ({response.status_code}): "
                            f"{_extract_error_message(error_text)}"
                        )

                    except RuntimeError:
                        raise
                    except Exception as exc:
                        last_error = exc
                        if response is not None:
                            await response.aclose()
                            response = None
                        # Network errors are retryable
                        if attempt < MAX_RETRIES:
                            delay_ms = BASE_DELAY_MS * (2**attempt)
                            await _sleep(delay_ms, signal)
                            continue
                        raise RuntimeError(f"Network error: {exc}") from exc

                if response is None or not (200 <= response.status_code < 300):
                    if last_error is not None:
                        raise RuntimeError(f"Network error: {last_error}") from last_error
                    raise RuntimeError("Failed to get response after retries")

                # ---------------------------------------------------------------
                # Stream processing
                # ---------------------------------------------------------------

                started = False

                def ensure_started() -> None:
                    nonlocal started
                    if not started:
                        stream.push(StartEvent(partial=output))
                        started = True

                def reset_output() -> None:
                    nonlocal started
                    output.content = []
                    output.usage = Usage()
                    output.stop_reason = "stop"
                    output.error_message = None
                    output.timestamp = int(time.time() * 1000)
                    started = False

                async def stream_response(active_response: httpx.Response) -> bool:
                    has_content = False
                    current_block: TextContent | ThinkingContent | None = None
                    blocks = output.content

                    def block_index() -> int:
                        return len(blocks) - 1

                    buffer = ""
                    try:
                        async for raw_bytes in active_response.aiter_bytes():
                            if signal is not None and signal.is_set():
                                raise RuntimeError("Request was aborted")

                            buffer += raw_bytes.decode("utf-8", errors="replace")
                            lines = buffer.split("\n")
                            buffer = lines[-1]
                            lines = lines[:-1]

                            for line in lines:
                                if not line.startswith("data:"):
                                    continue

                                json_str = line[5:].strip()
                                if not json_str:
                                    continue

                                try:
                                    chunk: dict[str, Any] = json.loads(json_str)
                                except json.JSONDecodeError:
                                    continue

                                # Unwrap the response
                                response_data = chunk.get("response")
                                if not response_data:
                                    continue

                                candidates = response_data.get("candidates")
                                candidate = candidates[0] if candidates else None
                                content_obj = candidate.get("content") if candidate else None
                                parts = content_obj.get("parts") if content_obj else None

                                if parts:
                                    for part in parts:
                                        part_text = part.get("text")
                                        if part_text is not None:
                                            has_content = True
                                            is_thinking = is_thinking_part(part)

                                            if (
                                                current_block is None
                                                or (is_thinking and current_block.type != "thinking")
                                                or (not is_thinking and current_block.type != "text")
                                            ):
                                                # End previous block
                                                if current_block is not None:
                                                    if current_block.type == "text":
                                                        assert isinstance(current_block, TextContent)
                                                        stream.push(
                                                            TextEndEvent(
                                                                content_index=block_index(),
                                                                content=current_block.text,
                                                                partial=output,
                                                            )
                                                        )
                                                    else:
                                                        assert isinstance(current_block, ThinkingContent)
                                                        stream.push(
                                                            ThinkingEndEvent(
                                                                content_index=block_index(),
                                                                content=current_block.thinking,
                                                                partial=output,
                                                            )
                                                        )

                                                # Start new block
                                                if is_thinking:
                                                    current_block = ThinkingContent(thinking="")
                                                    output.content.append(current_block)
                                                    ensure_started()
                                                    stream.push(
                                                        ThinkingStartEvent(
                                                            content_index=block_index(),
                                                            partial=output,
                                                        )
                                                    )
                                                else:
                                                    current_block = TextContent(text="")
                                                    output.content.append(current_block)
                                                    ensure_started()
                                                    stream.push(
                                                        TextStartEvent(
                                                            content_index=block_index(),
                                                            partial=output,
                                                        )
                                                    )

                                            # Append delta
                                            if current_block.type == "thinking":
                                                assert isinstance(current_block, ThinkingContent)
                                                current_block.thinking += part_text
                                                current_block.thinking_signature = retain_thought_signature(
                                                    current_block.thinking_signature,
                                                    part.get("thoughtSignature"),
                                                )
                                                stream.push(
                                                    ThinkingDeltaEvent(
                                                        content_index=block_index(),
                                                        delta=part_text,
                                                        partial=output,
                                                    )
                                                )
                                            else:
                                                assert isinstance(current_block, TextContent)
                                                current_block.text += part_text
                                                current_block.text_signature = retain_thought_signature(
                                                    current_block.text_signature,
                                                    part.get("thoughtSignature"),
                                                )
                                                stream.push(
                                                    TextDeltaEvent(
                                                        content_index=block_index(),
                                                        delta=part_text,
                                                        partial=output,
                                                    )
                                                )

                                        func_call = part.get("functionCall")
                                        if func_call:
                                            has_content = True
                                            # End previous text/thinking block
                                            if current_block is not None:
                                                if current_block.type == "text":
                                                    assert isinstance(current_block, TextContent)
                                                    stream.push(
                                                        TextEndEvent(
                                                            content_index=block_index(),
                                                            content=current_block.text,
                                                            partial=output,
                                                        )
                                                    )
                                                else:
                                                    assert isinstance(current_block, ThinkingContent)
                                                    stream.push(
                                                        ThinkingEndEvent(
                                                            content_index=block_index(),
                                                            content=current_block.thinking,
                                                            partial=output,
                                                        )
                                                    )
                                                current_block = None

                                            provided_id = func_call.get("id")
                                            needs_new_id = not provided_id or any(
                                                isinstance(b, ToolCall) and b.id == provided_id for b in output.content
                                            )
                                            fc_name = func_call.get("name", "")
                                            tool_call_id = (
                                                generate_tool_call_id(fc_name) if needs_new_id else provided_id
                                            )

                                            thought_sig = part.get("thoughtSignature")
                                            tool_call = ToolCall(
                                                id=tool_call_id or "",
                                                name=func_call.get("name", ""),
                                                arguments=func_call.get("args") or {},
                                                thought_signature=thought_sig if thought_sig else None,
                                            )

                                            output.content.append(tool_call)
                                            ensure_started()
                                            stream.push(
                                                ToolCallStartEvent(
                                                    content_index=block_index(),
                                                    partial=output,
                                                )
                                            )
                                            stream.push(
                                                ToolCallDeltaEvent(
                                                    content_index=block_index(),
                                                    delta=json.dumps(tool_call.arguments),
                                                    partial=output,
                                                )
                                            )
                                            stream.push(
                                                ToolCallEndEvent(
                                                    content_index=block_index(),
                                                    tool_call=tool_call,
                                                    partial=output,
                                                )
                                            )

                                # Finish reason
                                if candidate and candidate.get("finishReason"):
                                    output.stop_reason = map_stop_reason_string(candidate["finishReason"])
                                    if any(isinstance(b, ToolCall) for b in output.content):
                                        output.stop_reason = "toolUse"

                                # Usage metadata
                                usage_meta = response_data.get("usageMetadata")
                                if usage_meta:
                                    prompt_tokens = usage_meta.get("promptTokenCount", 0) or 0
                                    cache_read_tokens = usage_meta.get("cachedContentTokenCount", 0) or 0
                                    output.usage = Usage(
                                        input=prompt_tokens - cache_read_tokens,
                                        output=(
                                            (usage_meta.get("candidatesTokenCount", 0) or 0)
                                            + (usage_meta.get("thoughtsTokenCount", 0) or 0)
                                        ),
                                        cache_read=cache_read_tokens,
                                        cache_write=0,
                                        total_tokens=usage_meta.get("totalTokenCount", 0) or 0,
                                        cost=UsageCost(),
                                    )
                                    calculate_cost(model, output.usage)
                    finally:
                        await active_response.aclose()

                    # End final block
                    if current_block is not None:
                        if current_block.type == "text":
                            assert isinstance(current_block, TextContent)
                            stream.push(
                                TextEndEvent(
                                    content_index=block_index(),
                                    content=current_block.text,
                                    partial=output,
                                )
                            )
                        else:
                            assert isinstance(current_block, ThinkingContent)
                            stream.push(
                                ThinkingEndEvent(
                                    content_index=block_index(),
                                    content=current_block.thinking,
                                    partial=output,
                                )
                            )

                    return has_content

                # Empty stream retry loop
                received_content = False
                current_response = response

                for empty_attempt in range(MAX_EMPTY_STREAM_RETRIES + 1):
                    if signal is not None and signal.is_set():
                        raise RuntimeError("Request was aborted")

                    if empty_attempt > 0:
                        backoff_ms = EMPTY_STREAM_BASE_DELAY_MS * (2 ** (empty_attempt - 1))
                        await _sleep(backoff_ms, signal)

                        if not request_url:
                            raise RuntimeError("Missing request URL")

                        current_response = await client.send(
                            client.build_request(
                                "POST",
                                request_url,
                                headers=request_headers,
                                content=request_body_json,
                            ),
                            stream=True,
                        )

                        if not (200 <= current_response.status_code < 300):
                            retry_error_text = (await current_response.aread()).decode("utf-8", errors="replace")
                            await current_response.aclose()
                            raise RuntimeError(
                                f"Cloud Code Assist API error ({current_response.status_code}): {retry_error_text}"
                            )

                    streamed = await stream_response(current_response)
                    if streamed:
                        received_content = True
                        break

                    if empty_attempt < MAX_EMPTY_STREAM_RETRIES:
                        reset_output()

                if not received_content:
                    raise RuntimeError("Cloud Code Assist API returned an empty response")

                if signal is not None and signal.is_set():
                    raise RuntimeError("Request was aborted")

                if output.stop_reason in ("aborted", "error"):
                    raise RuntimeError("An unknown error occurred")

                stream.push(DoneEvent(reason=output.stop_reason, message=output))
                stream.end()

        except Exception as exc:
            output.stop_reason = "aborted" if (signal is not None and signal.is_set()) else "error"
            output.error_message = str(exc)
            stream.push(ErrorEvent(reason=output.stop_reason, error=output))
            stream.end()

    task = asyncio.ensure_future(_run())
    # Store task reference on stream to prevent garbage collection
    stream._background_task = task  # type: ignore[attr-defined]
    return stream


# ---------------------------------------------------------------------------
# stream_simple_google_gemini_cli (public)
# ---------------------------------------------------------------------------


def stream_simple_google_gemini_cli(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessageEventStream:
    """Simplified streaming interface with automatic thinking configuration."""
    api_key = options.api_key if options else None
    if not api_key:
        raise RuntimeError("Google Cloud Code Assist requires OAuth authentication. Use /login to authenticate.")

    base = build_base_options(model, options, api_key)

    if not (options and options.reasoning):
        return stream_google_gemini_cli(
            model,
            context,
            GoogleGeminiCliOptions(
                temperature=base.temperature,
                max_tokens=base.max_tokens,
                signal=base.signal,
                api_key=base.api_key,
                cache_retention=base.cache_retention,
                session_id=base.session_id,
                headers=base.headers,
                on_payload=base.on_payload,
                max_retry_delay_ms=base.max_retry_delay_ms,
                metadata=base.metadata,
                thinking=_ThinkingConfig(enabled=False),
            ),
        )

    effort = clamp_reasoning(options.reasoning)
    assert effort is not None

    if "3-pro" in model.id or "3-flash" in model.id:
        return stream_google_gemini_cli(
            model,
            context,
            GoogleGeminiCliOptions(
                temperature=base.temperature,
                max_tokens=base.max_tokens,
                signal=base.signal,
                api_key=base.api_key,
                cache_retention=base.cache_retention,
                session_id=base.session_id,
                headers=base.headers,
                on_payload=base.on_payload,
                max_retry_delay_ms=base.max_retry_delay_ms,
                metadata=base.metadata,
                thinking=_ThinkingConfig(
                    enabled=True,
                    level=_get_gemini_cli_thinking_level(effort, model.id),
                ),
            ),
        )

    default_budgets = ThinkingBudgets(minimal=1024, low=2048, medium=8192, high=16384)
    custom = options.thinking_budgets
    budgets = ThinkingBudgets(
        minimal=custom.minimal if custom and custom.minimal is not None else default_budgets.minimal,
        low=custom.low if custom and custom.low is not None else default_budgets.low,
        medium=custom.medium if custom and custom.medium is not None else default_budgets.medium,
        high=custom.high if custom and custom.high is not None else default_budgets.high,
    )

    min_output_tokens = 1024
    thinking_budget = getattr(budgets, effort) or 16384
    max_tokens = min((base.max_tokens or 0) + thinking_budget, model.max_tokens)

    if max_tokens <= thinking_budget:
        thinking_budget = max(0, max_tokens - min_output_tokens)

    return stream_google_gemini_cli(
        model,
        context,
        GoogleGeminiCliOptions(
            temperature=base.temperature,
            max_tokens=max_tokens,
            signal=base.signal,
            api_key=base.api_key,
            cache_retention=base.cache_retention,
            session_id=base.session_id,
            headers=base.headers,
            on_payload=base.on_payload,
            max_retry_delay_ms=base.max_retry_delay_ms,
            metadata=base.metadata,
            thinking=_ThinkingConfig(
                enabled=True,
                budget_tokens=thinking_budget,
            ),
        ),
    )
