"""Slash command types and built-in commands.

Port of packages/coding-agent/src/core/slash-commands.ts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Slash command source type
SlashCommandSource = Literal["extension", "prompt", "skill"]

# Slash command location type
SlashCommandLocation = Literal["user", "project", "path"]


@dataclass
class SlashCommandInfo:
    """Information about a slash command."""

    name: str
    description: str | None = None
    source: SlashCommandSource = "prompt"
    location: SlashCommandLocation | None = None
    path: str | None = None


@dataclass
class BuiltinSlashCommand:
    """A built-in slash command definition."""

    name: str
    description: str


BUILTIN_SLASH_COMMANDS: list[BuiltinSlashCommand] = [
    BuiltinSlashCommand(name="settings", description="Open settings menu"),
    BuiltinSlashCommand(name="model", description="Select model (opens selector UI)"),
    BuiltinSlashCommand(name="scoped-models", description="Enable/disable models for Ctrl+P cycling"),
    BuiltinSlashCommand(name="export", description="Export session to HTML file"),
    BuiltinSlashCommand(name="share", description="Share session as a secret GitHub gist"),
    BuiltinSlashCommand(name="copy", description="Copy last agent message to clipboard"),
    BuiltinSlashCommand(name="name", description="Set session display name"),
    BuiltinSlashCommand(name="session", description="Show session info and stats"),
    BuiltinSlashCommand(name="changelog", description="Show changelog entries"),
    BuiltinSlashCommand(name="hotkeys", description="Show all keyboard shortcuts"),
    BuiltinSlashCommand(name="fork", description="Create a new fork from a previous message"),
    BuiltinSlashCommand(name="tree", description="Navigate session tree (switch branches)"),
    BuiltinSlashCommand(name="login", description="Login with OAuth provider"),
    BuiltinSlashCommand(name="logout", description="Logout from OAuth provider"),
    BuiltinSlashCommand(name="new", description="Start a new session"),
    BuiltinSlashCommand(name="compact", description="Manually compact the session context"),
    BuiltinSlashCommand(name="resume", description="Resume a different session"),
    BuiltinSlashCommand(name="reload", description="Reload extensions, skills, prompts, and themes"),
    BuiltinSlashCommand(name="quit", description="Quit pi"),
]

# snake_case alias for parity with TS camelCase export
builtin_slash_commands = BUILTIN_SLASH_COMMANDS
