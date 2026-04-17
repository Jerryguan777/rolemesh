"""Tests for rolemesh.ipc.web_protocol -- WebUI NATS message shapes."""

from __future__ import annotations

import json

from rolemesh.ipc.web_protocol import WebStreamChunk


def test_stream_chunk_text_roundtrip() -> None:
    chunk = WebStreamChunk(type="text", content="hello world")
    restored = WebStreamChunk.from_bytes(chunk.to_bytes())
    assert restored.type == "text"
    assert restored.content == "hello world"


def test_stream_chunk_done_roundtrip() -> None:
    chunk = WebStreamChunk(type="done")
    data = chunk.to_bytes()
    # done skips the content field entirely — stay backward-compatible with
    # old consumers that never had content on done frames.
    assert b"content" not in data
    restored = WebStreamChunk.from_bytes(data)
    assert restored.type == "done"
    assert restored.content == ""


def test_stream_chunk_status_roundtrip() -> None:
    """Status chunks carry a JSON-encoded payload in content."""
    payload = {"status": "tool_use", "tool": "Bash", "input": "ls /tmp"}
    chunk = WebStreamChunk(type="status", content=json.dumps(payload))
    restored = WebStreamChunk.from_bytes(chunk.to_bytes())
    assert restored.type == "status"
    assert json.loads(restored.content) == payload


def test_stream_chunk_status_empty_payload() -> None:
    """A status chunk with an empty JSON payload survives the round-trip."""
    chunk = WebStreamChunk(type="status", content="{}")
    restored = WebStreamChunk.from_bytes(chunk.to_bytes())
    assert restored.type == "status"
    assert restored.content == "{}"


def test_stream_chunk_from_bytes_tolerates_missing_content() -> None:
    """Frames produced by older orchestrators (done without content) parse ok."""
    raw = json.dumps({"type": "done"}).encode()
    restored = WebStreamChunk.from_bytes(raw)
    assert restored.type == "done"
    assert restored.content == ""
