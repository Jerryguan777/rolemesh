"""System prompt construction — Python port of packages/coding-agent/src/core/system-prompt.ts."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Skill:
    """Represents an agent skill loaded from a markdown file."""

    name: str = ""
    description: str = ""
    file_path: str = ""
    base_dir: str = ""
    source: str = ""
    disable_model_invocation: bool = False


@dataclass
class BuildSystemPromptOptions:
    """Options for building the system prompt."""

    custom_prompt: str | None = None
    selected_tools: list[str] | None = None
    append_system_prompt: str | None = None
    cwd: str | None = None
    context_files: list[dict[str, str]] | None = None  # [{"path": ..., "content": ...}]
    skills: list[Skill] | None = None


# Tool descriptions for the built-in coding tools
_TOOL_DESCRIPTIONS: dict[str, str] = {
    "read": "Read file contents",
    "bash": "Execute bash commands (ls, grep, find, etc.)",
    "edit": "Make surgical edits to files (find exact text and replace)",
    "write": "Create or overwrite files",
    "grep": "Search file contents for patterns (respects .gitignore)",
    "find": "Find files by glob pattern (respects .gitignore)",
    "ls": "List directory contents",
}


def _format_skills_for_prompt(skills: list[Skill]) -> str:
    """Format skills into an XML block for inclusion in the system prompt."""
    visible = [s for s in skills if not s.disable_model_invocation]
    if not visible:
        return ""

    lines = [
        "",
        "",
        "The following skills provide specialized instructions for specific tasks.",
        "Use the read tool to load a skill's file when the task matches its description.",
        "When a skill file references a relative path, resolve it against the skill directory "
        "(parent of SKILL.md / dirname of the path) and use that absolute path in tool commands.",
        "",
        "<available_skills>",
    ]

    def _escape_xml(text: str) -> str:
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;")
        )

    for skill in visible:
        lines.append("  <skill>")
        lines.append(f"    <name>{_escape_xml(skill.name)}</name>")
        lines.append(f"    <description>{_escape_xml(skill.description)}</description>")
        lines.append(f"    <location>{_escape_xml(skill.file_path)}</location>")
        lines.append("  </skill>")

    lines.append("</available_skills>")
    return "\n".join(lines)


def _get_readme_path() -> str:
    """Return path to the pi README (best-effort)."""
    return os.path.join(os.path.dirname(__file__), "..", "..", "README.md")


def _get_docs_path() -> str:
    """Return path to the pi docs directory (best-effort)."""
    return os.path.join(os.path.dirname(__file__), "..", "..", "docs")


def _get_examples_path() -> str:
    """Return path to the pi examples directory (best-effort)."""
    return os.path.join(os.path.dirname(__file__), "..", "..", "examples")


def build_system_prompt(options: BuildSystemPromptOptions | None = None) -> str:
    """Build the system prompt with tools, guidelines, and optional context.

    Args:
        options: Configuration for the system prompt. If None, uses defaults.

    Returns:
        Complete system prompt string.
    """
    if options is None:
        options = BuildSystemPromptOptions()

    resolved_cwd = options.cwd or os.getcwd()
    now = datetime.now()
    date_time = now.strftime("%A, %B %d, %Y %I:%M:%S %p")

    append_section = f"\n\n{options.append_system_prompt}" if options.append_system_prompt else ""

    context_files = options.context_files or []
    skills = options.skills or []

    if options.custom_prompt:
        prompt = options.custom_prompt

        if append_section:
            prompt += append_section

        # Append project context files
        if context_files:
            prompt += "\n\n# Project Context\n\n"
            prompt += "Project-specific instructions and guidelines:\n\n"
            for file_entry in context_files:
                file_path = file_entry.get("path", "")
                content = file_entry.get("content", "")
                prompt += f"## {file_path}\n\n{content}\n\n"

        # Append skills section (only if read tool is available or no tool filter)
        custom_prompt_has_read = options.selected_tools is None or "read" in options.selected_tools
        if custom_prompt_has_read and skills:
            prompt += _format_skills_for_prompt(skills)

        # Add date/time and working directory last
        prompt += f"\nCurrent date and time: {date_time}"
        prompt += f"\nCurrent working directory: {resolved_cwd}"

        return prompt

    # Default system prompt
    readme_path = _get_readme_path()
    docs_path = _get_docs_path()
    examples_path = _get_examples_path()

    # Build tools list based on selected tools (only built-in tools with known descriptions)
    default_tools = ["read", "bash", "edit", "write"]
    selected = options.selected_tools if options.selected_tools is not None else default_tools
    tools = [t for t in selected if t in _TOOL_DESCRIPTIONS]
    tools_list = "\n".join(f"- {t}: {_TOOL_DESCRIPTIONS[t]}" for t in tools) if tools else "(none)"

    has_bash = "bash" in tools
    has_edit = "edit" in tools
    has_write = "write" in tools
    has_grep = "grep" in tools
    has_find = "find" in tools
    has_ls = "ls" in tools
    has_read = "read" in tools

    guidelines: list[str] = []

    # File exploration guidelines
    if has_bash and not has_grep and not has_find and not has_ls:
        guidelines.append("Use bash for file operations like ls, rg, find")
    elif has_bash and (has_grep or has_find or has_ls):
        guidelines.append("Prefer grep/find/ls tools over bash for file exploration (faster, respects .gitignore)")

    if has_read and has_edit:
        guidelines.append("Use read to examine files before editing. You must use this tool instead of cat or sed.")

    if has_edit:
        guidelines.append("Use edit for precise changes (old text must match exactly)")

    if has_write:
        guidelines.append("Use write only for new files or complete rewrites")

    if has_edit or has_write:
        guidelines.append(
            "When summarizing your actions, output plain text directly - do NOT use cat or bash to display what you did"
        )

    guidelines.append("Be concise in your responses")
    guidelines.append("Show file paths clearly when working with files")

    guidelines_text = "\n".join(f"- {g}" for g in guidelines)

    _pi_intro = (
        "You are an expert coding assistant operating inside pi, a coding agent harness. "
        "You help users by reading files, executing commands, editing code, and writing new files."
    )
    _pi_docs_when = (
        "- When asked about: extensions (docs/extensions.md, examples/extensions/),"
        " themes (docs/themes.md), skills (docs/skills.md),"
        " prompt templates (docs/prompt-templates.md), TUI components (docs/tui.md),"
        " keybindings (docs/keybindings.md), SDK integrations (docs/sdk.md),"
        " custom providers (docs/custom-provider.md), adding models (docs/models.md),"
        " pi packages (docs/packages.md)"
    )
    prompt = f"""{_pi_intro}

Available tools:
{tools_list}

In addition to the tools above, you may have access to other custom tools depending on the project.

Guidelines:
{guidelines_text}

Pi documentation (read only when the user asks about pi itself, its SDK, extensions, themes, skills, or TUI):
- Main documentation: {readme_path}
- Additional docs: {docs_path}
- Examples: {examples_path} (extensions, custom tools, SDK)
{_pi_docs_when}
- When working on pi topics, read the docs and examples, and follow .md cross-references before implementing
- Always read pi .md files completely and follow links to related docs (e.g., tui.md for TUI API details)"""

    if append_section:
        prompt += append_section

    # Append project context files
    if context_files:
        prompt += "\n\n# Project Context\n\n"
        prompt += "Project-specific instructions and guidelines:\n\n"
        for file_entry in context_files:
            file_path = file_entry.get("path", "")
            content = file_entry.get("content", "")
            prompt += f"## {file_path}\n\n{content}\n\n"

    # Append skills section (only if read tool is available)
    if has_read and skills:
        prompt += _format_skills_for_prompt(skills)

    # Add date/time and working directory last
    prompt += f"\nCurrent date and time: {date_time}"
    prompt += f"\nCurrent working directory: {resolved_cwd}"

    return prompt
