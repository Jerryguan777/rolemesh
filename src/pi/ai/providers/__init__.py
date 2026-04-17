"""Provider implementations for pi.ai."""

from pi.ai.providers.amazon_bedrock import BedrockOptions, stream_bedrock, stream_simple_bedrock
from pi.ai.providers.anthropic import AnthropicEffort, AnthropicOptions, stream_anthropic, stream_simple_anthropic
from pi.ai.providers.azure_openai_responses import (
    AzureOpenAIResponsesOptions,
    stream_azure_openai_responses,
    stream_simple_azure_openai_responses,
)
from pi.ai.providers.github_copilot_headers import (
    build_copilot_dynamic_headers,
    has_copilot_vision_input,
    infer_copilot_initiator,
)
from pi.ai.providers.google import GoogleOptions, GoogleThinkingLevel, stream_google, stream_simple_google
from pi.ai.providers.google_gemini_cli import (
    GoogleGeminiCliOptions,
    build_request,
    extract_retry_delay,
    stream_google_gemini_cli,
    stream_simple_google_gemini_cli,
)
from pi.ai.providers.google_shared import (
    is_thinking_part,
    map_stop_reason,
    map_stop_reason_string,
    map_tool_choice,
    requires_tool_call_id,
    retain_thought_signature,
)
from pi.ai.providers.google_vertex import GoogleVertexOptions, stream_google_vertex, stream_simple_google_vertex
from pi.ai.providers.openai_codex_responses import (
    OpenAICodexResponsesOptions,
    stream_openai_codex_responses,
    stream_simple_openai_codex_responses,
)
from pi.ai.providers.openai_completions import (
    OpenAICompletionsOptions,
    convert_messages,
    stream_openai_completions,
    stream_simple_openai_completions,
)
from pi.ai.providers.openai_responses import (
    OpenAIResponsesOptions,
    stream_openai_responses,
    stream_simple_openai_responses,
)
from pi.ai.providers.openai_responses_shared import (
    ConvertResponsesMessagesOptions,
    ConvertResponsesToolsOptions,
    OpenAIResponsesStreamOptions,
    convert_responses_messages,
    convert_responses_tools,
    process_responses_stream,
)
from pi.ai.providers.register_builtins import BUILT_IN_APIS, register_built_in_api_providers, reset_api_providers
from pi.ai.providers.simple_options import adjust_max_tokens_for_thinking, build_base_options, clamp_reasoning
from pi.ai.providers.transform_messages import transform_messages

__all__ = [
    "BUILT_IN_APIS",
    "AnthropicEffort",
    "AnthropicOptions",
    "AzureOpenAIResponsesOptions",
    "BedrockOptions",
    "ConvertResponsesMessagesOptions",
    "ConvertResponsesToolsOptions",
    "GoogleGeminiCliOptions",
    "GoogleOptions",
    "GoogleThinkingLevel",
    "GoogleVertexOptions",
    "OpenAICodexResponsesOptions",
    "OpenAICompletionsOptions",
    "OpenAIResponsesOptions",
    "OpenAIResponsesStreamOptions",
    "adjust_max_tokens_for_thinking",
    "build_base_options",
    "build_copilot_dynamic_headers",
    "build_request",
    "clamp_reasoning",
    "convert_messages",
    "convert_responses_messages",
    "convert_responses_tools",
    "extract_retry_delay",
    "has_copilot_vision_input",
    "infer_copilot_initiator",
    "is_thinking_part",
    "map_stop_reason",
    "map_stop_reason_string",
    "map_tool_choice",
    "process_responses_stream",
    "register_built_in_api_providers",
    "requires_tool_call_id",
    "reset_api_providers",
    "retain_thought_signature",
    "stream_anthropic",
    "stream_azure_openai_responses",
    "stream_bedrock",
    "stream_google",
    "stream_google_gemini_cli",
    "stream_google_vertex",
    "stream_openai_codex_responses",
    "stream_openai_completions",
    "stream_openai_responses",
    "stream_simple_anthropic",
    "stream_simple_azure_openai_responses",
    "stream_simple_bedrock",
    "stream_simple_google",
    "stream_simple_google_gemini_cli",
    "stream_simple_google_vertex",
    "stream_simple_openai_codex_responses",
    "stream_simple_openai_completions",
    "stream_simple_openai_responses",
    "transform_messages",
]
