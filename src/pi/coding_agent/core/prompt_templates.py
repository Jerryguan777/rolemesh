"""Prompt template loading and expansion.

Port of packages/coding-agent/src/core/prompt-templates.ts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pi.coding_agent.core._frontmatter import parse_frontmatter
from pi.coding_agent.core.config import CONFIG_DIR_NAME, get_prompts_dir

# ============================================================================
# Types
# ============================================================================


@dataclass
class PromptTemplate:
    """A loaded prompt template."""

    name: str
    description: str
    content: str
    source: str  # "user", "project", or "path"
    file_path: str  # Absolute path to the template file


@dataclass
class LoadPromptTemplatesOptions:
    """Options for loading prompt templates."""

    cwd: str | None = None  # Default: cwd
    agent_dir: str | None = None  # Default: from get_prompts_dir()
    prompt_paths: list[str] | None = None  # Explicit prompt paths
    include_defaults: bool = True


# ============================================================================
# Argument parsing and substitution
# ============================================================================


def parse_command_args(args_string: str) -> list[str]:
    """Parse command arguments respecting quoted strings (bash-style).

    Returns array of arguments.
    """
    args: list[str] = []
    current = ""
    in_quote: str | None = None

    for char in args_string:
        if in_quote:
            if char == in_quote:
                in_quote = None
            else:
                current += char
        elif char in ('"', "'"):
            in_quote = char
        elif char in (" ", "\t"):
            if current:
                args.append(current)
                current = ""
        else:
            current += char

    if current:
        args.append(current)

    return args


def substitute_args(content: str, args: list[str]) -> str:
    """Substitute argument placeholders in template content.

    Supports:
    - $1, $2, ... for positional args
    - $@ and $ARGUMENTS for all args
    - ${@:N} for args from Nth onwards
    - ${@:N:L} for L args starting from Nth
    """
    import re

    result = content

    # Replace $1, $2, etc. with positional args FIRST (before wildcards)
    result = re.sub(
        r"\$(\d+)",
        lambda m: args[int(m.group(1)) - 1] if int(m.group(1)) - 1 < len(args) else "",
        result,
    )

    # Replace ${@:start} or ${@:start:length} with sliced args
    def _slice_args(m: re.Match) -> str:  # type: ignore[type-arg]
        start_str = m.group(1)
        length_str = m.group(2)
        start = max(0, int(start_str) - 1)  # Convert to 0-indexed
        if length_str:
            length = int(length_str)
            return " ".join(args[start : start + length])
        return " ".join(args[start:])

    result = re.sub(r"\$\{@:(\d+)(?::(\d+))?\}", _slice_args, result)

    # Pre-compute all args joined
    all_args = " ".join(args)

    # Replace $ARGUMENTS with all args joined
    result = result.replace("$ARGUMENTS", all_args)

    # Replace $@ with all args joined
    result = result.replace("$@", all_args)

    return result


# ============================================================================
# Template loading
# ============================================================================


def _load_template_from_file(
    file_path: str,
    source: str,
    source_label: str,
) -> PromptTemplate | None:
    """Load a prompt template from a file."""
    try:
        raw = Path(file_path).read_text(encoding="utf-8")
        fm, body = parse_frontmatter(raw)

        name = Path(file_path).stem  # filename without .md

        description = fm.get("description", "")
        if not description:
            first_line = next((line for line in body.splitlines() if line.strip()), "")
            if first_line:
                description = first_line[:60]
                if len(first_line) > 60:
                    description += "..."

        description = f"{description} {source_label}" if description else source_label

        return PromptTemplate(
            name=name,
            description=description,
            content=body,
            source=source,
            file_path=file_path,
        )
    except Exception:
        return None


def _load_templates_from_dir(
    dir_path: str,
    source: str,
    source_label: str,
) -> list[PromptTemplate]:
    """Scan a directory for .md files (non-recursive) and load them as prompt templates."""
    templates: list[PromptTemplate] = []
    dirp = Path(dir_path)

    if not dirp.exists():
        return templates

    try:
        for entry in sorted(dirp.iterdir(), key=lambda e: e.name):
            # Resolve symlinks
            try:
                is_file = entry.is_file()
                if entry.is_symlink():
                    is_file = os.path.isfile(entry)
            except OSError:
                continue

            if is_file and entry.name.endswith(".md"):
                tmpl = _load_template_from_file(str(entry), source, source_label)
                if tmpl:
                    templates.append(tmpl)
    except OSError:
        return templates

    return templates


# ============================================================================
# Public API
# ============================================================================


def load_prompt_templates(options: dict[str, Any] | None = None) -> list[PromptTemplate]:
    """Load all prompt templates from configured locations.

    Options:
    - cwd: Working directory for project-local templates.
    - agent_dir: Agent config directory for global templates.
    - prompt_paths: Explicit prompt template paths (files or directories).
    - include_defaults: Include default prompt directories. Default: True.
    """
    opts = options or {}
    cwd = opts.get("cwd") or str(Path.cwd())
    agent_dir = opts.get("agent_dir")
    prompt_paths: list[str] = opts.get("prompt_paths", [])
    include_defaults = opts.get("include_defaults", True)

    # Determine global prompts dir
    global_prompts_dir = str(Path(agent_dir) / "prompts") if agent_dir else str(get_prompts_dir())

    templates: list[PromptTemplate] = []

    if include_defaults:
        # 1. Load global templates
        templates.extend(_load_templates_from_dir(global_prompts_dir, "user", "(user)"))

        # 2. Load project templates
        project_prompts_dir = str(Path(cwd) / CONFIG_DIR_NAME / "prompts")
        templates.extend(_load_templates_from_dir(project_prompts_dir, "project", "(project)"))

    user_prompts_dir = global_prompts_dir
    project_prompts_dir = str(Path(cwd) / CONFIG_DIR_NAME / "prompts")

    def _is_under(target: str, root: str) -> bool:
        resolved_root = str(Path(root).resolve())
        resolved_target = str(Path(target).resolve())
        return resolved_target == resolved_root or resolved_target.startswith(resolved_root + os.sep)

    def _get_source_info(resolved_path: str) -> tuple[str, str]:
        if not include_defaults:
            if _is_under(resolved_path, user_prompts_dir):
                return "user", "(user)"
            if _is_under(resolved_path, project_prompts_dir):
                return "project", "(project)"
        base = Path(resolved_path).stem or "path"
        return "path", f"(path:{base})"

    # 3. Load explicit prompt paths
    for raw_path in prompt_paths:
        expanded = raw_path.strip()
        if expanded == "~":
            expanded = str(Path.home())
        elif expanded.startswith("~/"):
            expanded = str(Path.home() / expanded[2:])

        resolved = expanded if Path(expanded).is_absolute() else str(Path(cwd) / expanded)

        if not Path(resolved).exists():
            continue

        try:
            source, label = _get_source_info(resolved)
            if Path(resolved).is_dir():
                templates.extend(_load_templates_from_dir(resolved, source, label))
            elif Path(resolved).is_file() and resolved.endswith(".md"):
                tmpl = _load_template_from_file(resolved, source, label)
                if tmpl:
                    templates.append(tmpl)
        except Exception:
            pass

    return templates


def expand_prompt_template(text: str, templates: list[PromptTemplate]) -> str:
    """Expand a prompt template if it matches a template name.

    Returns the expanded content or the original text if not a template.
    Templates are invoked with /name [args].
    """
    if not text.startswith("/"):
        return text

    space_idx = text.find(" ")
    template_name = text[1:space_idx] if space_idx != -1 else text[1:]
    args_string = text[space_idx + 1 :] if space_idx != -1 else ""

    template = next((t for t in templates if t.name == template_name), None)
    if template:
        args = parse_command_args(args_string)
        return substitute_args(template.content, args)

    return text
