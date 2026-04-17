"""Skill loading and formatting for pi.coding_agent.

Port of packages/coding-agent/src/core/skills.ts.
"""

from __future__ import annotations

import os
import re as _re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pi.coding_agent.core._frontmatter import parse_frontmatter
from pi.coding_agent.core.config import CONFIG_DIR_NAME, get_agent_dir

# ============================================================================
# Skill Block Parsing
# ============================================================================

_SKILL_BLOCK_RE = _re.compile(r'^<skill name="([^"]+)" location="([^"]+)">\n([\s\S]*?)\n</skill>(?:\n\n([\s\S]+))?$')


@dataclass
class ParsedSkillBlock:
    """Parsed skill block from a user message."""

    name: str
    location: str
    content: str
    user_message: str | None


def parse_skill_block(text: str) -> ParsedSkillBlock | None:
    """Parse a skill block from message text.

    Returns None if the text does not contain a skill block.
    """
    match = _SKILL_BLOCK_RE.match(text)
    if not match:
        return None
    user_msg = match.group(4)
    if user_msg is not None:
        user_msg = user_msg.strip() or None
    return ParsedSkillBlock(
        name=match.group(1),
        location=match.group(2),
        content=match.group(3),
        user_message=user_msg,
    )


# ============================================================================
# Constants
# ============================================================================

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
IGNORE_FILE_NAMES = [".gitignore", ".ignore", ".fdignore"]


# ============================================================================
# Types
# ============================================================================


@dataclass
class SkillFrontmatter:
    """Parsed skill frontmatter."""

    name: str | None = None
    description: str | None = None
    disable_model_invocation: bool = False


@dataclass
class Skill:
    """A loaded skill."""

    name: str
    description: str
    file_path: str
    base_dir: str
    source: str
    disable_model_invocation: bool


@dataclass
class LoadSkillsFromDirOptions:
    """Options for loading skills from a directory."""

    dir: str = ""
    source: str = ""


@dataclass
class LoadSkillsOptions:
    """Options for loading skills from all configured locations."""

    cwd: str | None = None  # Default: cwd
    agent_dir: str | None = None  # Default: ~/.pi/agent
    skill_paths: list[str] | None = None  # Explicit skill paths
    include_defaults: bool = True


@dataclass
class LoadSkillsResult:
    """Result of loading skills."""

    skills: list[Skill]
    diagnostics: list[dict[str, Any]]  # ResourceDiagnostic


# ============================================================================
# Validation
# ============================================================================


def _validate_name(name: str, parent_dir_name: str) -> list[str]:
    """Validate skill name per Agent Skills spec."""
    errors: list[str] = []

    if name != parent_dir_name:
        errors.append(f'name "{name}" does not match parent directory "{parent_dir_name}"')

    if len(name) > MAX_NAME_LENGTH:
        errors.append(f"name exceeds {MAX_NAME_LENGTH} characters ({len(name)})")

    import re

    if not re.match(r"^[a-z0-9-]+$", name):
        errors.append("name contains invalid characters (must be lowercase a-z, 0-9, hyphens only)")

    if name.startswith("-") or name.endswith("-"):
        errors.append("name must not start or end with a hyphen")

    if "--" in name:
        errors.append("name must not contain consecutive hyphens")

    return errors


def _validate_description(description: str | None) -> list[str]:
    """Validate description per Agent Skills spec."""
    errors: list[str] = []
    if not description or not description.strip():
        errors.append("description is required")
    elif len(description) > MAX_DESCRIPTION_LENGTH:
        errors.append(f"description exceeds {MAX_DESCRIPTION_LENGTH} characters ({len(description)})")
    return errors


# ============================================================================
# File loading
# ============================================================================


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;")
    )


def _load_skill_from_file(
    file_path: str,
    source: str,
) -> tuple[Skill | None, list[dict[str, Any]]]:
    """Load a skill from a single markdown file."""
    diagnostics: list[dict[str, Any]] = []

    try:
        raw_content = Path(file_path).read_text(encoding="utf-8")
        fm, _ = parse_frontmatter(raw_content)

        skill_dir = str(Path(file_path).parent)
        parent_dir_name = Path(skill_dir).name

        # Validate description
        description = fm.get("description")
        if isinstance(description, bool):
            description = str(description).lower()
        desc_errors = _validate_description(description)
        for err in desc_errors:
            diagnostics.append({"type": "warning", "message": err, "path": file_path})

        # Use name from frontmatter, or fall back to parent directory name
        name_raw = fm.get("name")
        name = str(name_raw) if name_raw else parent_dir_name

        # Validate name
        name_errors = _validate_name(name, parent_dir_name)
        for err in name_errors:
            diagnostics.append({"type": "warning", "message": err, "path": file_path})

        # Skip if no description
        if not description or not str(description).strip():
            return None, diagnostics

        disable_model_invocation = fm.get("disable-model-invocation", False)
        if isinstance(disable_model_invocation, str):
            disable_model_invocation = disable_model_invocation.lower() == "true"

        return (
            Skill(
                name=name,
                description=str(description),
                file_path=file_path,
                base_dir=skill_dir,
                source=source,
                disable_model_invocation=bool(disable_model_invocation),
            ),
            diagnostics,
        )

    except Exception as e:
        message = str(e) if str(e) else "failed to parse skill file"
        diagnostics.append({"type": "warning", "message": message, "path": file_path})
        return None, diagnostics


# ============================================================================
# Directory scanning
# ============================================================================


def _should_ignore(path_rel: str, ignore_patterns: list[str]) -> bool:
    """Simple gitignore-style pattern matching."""
    import fnmatch

    for pattern in ignore_patterns:
        if fnmatch.fnmatch(path_rel, pattern):
            return True
        if fnmatch.fnmatch(Path(path_rel).name, pattern):
            return True
    return False


def _read_ignore_patterns(dir_path: str, root_dir: str) -> list[str]:
    """Read ignore patterns from .gitignore etc. in a directory."""
    patterns: list[str] = []
    for ignore_file in IGNORE_FILE_NAMES:
        ignore_path = Path(dir_path) / ignore_file
        if ignore_path.exists():
            try:
                for line in ignore_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        patterns.append(line)
            except OSError:
                pass
    return patterns


def _load_skills_from_dir_internal(
    dir_path: str,
    source: str,
    include_root_files: bool,
    ignore_patterns: list[str] | None = None,
    root_dir: str | None = None,
) -> LoadSkillsResult:
    """Internal recursive skill loading."""
    skills: list[Skill] = []
    diagnostics: list[dict[str, Any]] = []
    dirp = Path(dir_path)

    if not dirp.exists():
        return LoadSkillsResult(skills=skills, diagnostics=diagnostics)

    resolved_root = root_dir or dir_path
    patterns = list(ignore_patterns or [])
    patterns.extend(_read_ignore_patterns(dir_path, resolved_root))

    try:
        for entry in sorted(dirp.iterdir(), key=lambda e: e.name):
            if entry.name.startswith(".") or entry.name == "node_modules":
                continue

            # Resolve symlinks
            try:
                is_dir = entry.is_dir()
                is_file = entry.is_file()
                if entry.is_symlink():
                    is_dir = os.path.isdir(entry)
                    is_file = os.path.isfile(entry)
            except OSError:
                continue

            if is_dir:
                sub_result = _load_skills_from_dir_internal(
                    str(entry),
                    source,
                    False,
                    patterns,
                    resolved_root,
                )
                skills.extend(sub_result.skills)
                diagnostics.extend(sub_result.diagnostics)
                continue

            if not is_file:
                continue

            is_root_md = include_root_files and entry.name.endswith(".md")
            is_skill_md = not include_root_files and entry.name == "SKILL.md"

            if not is_root_md and not is_skill_md:
                continue

            skill, skill_diags = _load_skill_from_file(str(entry), source)
            if skill:
                skills.append(skill)
            diagnostics.extend(skill_diags)

    except OSError:
        pass

    return LoadSkillsResult(skills=skills, diagnostics=diagnostics)


# ============================================================================
# Public API
# ============================================================================


def load_skills_from_dir(
    dir: str | Path,
    source: str,
) -> LoadSkillsResult:
    """Load skills from a directory.

    Discovery rules:
    - Direct .md children in the root
    - Recursive SKILL.md under subdirectories
    """
    return _load_skills_from_dir_internal(str(dir), source, True)


def load_skills(options: dict[str, Any] | None = None) -> LoadSkillsResult:
    """Load skills from all configured locations.

    Options:
    - cwd: Working directory for project-local skills.
    - agent_dir: Agent config directory for global skills.
    - skill_paths: Explicit skill paths (files or directories).
    - include_defaults: Include default skills directories. Default: True.
    """
    opts = options or {}
    cwd = opts.get("cwd") or str(Path.cwd())
    agent_dir = opts.get("agent_dir") or str(get_agent_dir())
    skill_paths: list[str] = opts.get("skill_paths", [])
    include_defaults = opts.get("include_defaults", True)

    skill_map: dict[str, Skill] = {}
    real_path_set: set[str] = set()
    all_diagnostics: list[dict[str, Any]] = []
    collision_diagnostics: list[dict[str, Any]] = []

    def add_skills(result: LoadSkillsResult) -> None:
        all_diagnostics.extend(result.diagnostics)
        for skill in result.skills:
            try:
                real_path = str(Path(skill.file_path).resolve())
            except OSError:
                real_path = skill.file_path

            if real_path in real_path_set:
                continue

            existing = skill_map.get(skill.name)
            if existing:
                collision_diagnostics.append(
                    {
                        "type": "collision",
                        "message": f'name "{skill.name}" collision',
                        "path": skill.file_path,
                        "collision": {
                            "resourceType": "skill",
                            "name": skill.name,
                            "winnerPath": existing.file_path,
                            "loserPath": skill.file_path,
                        },
                    }
                )
            else:
                skill_map[skill.name] = skill
                real_path_set.add(real_path)

    if include_defaults:
        add_skills(_load_skills_from_dir_internal(str(Path(agent_dir) / "skills"), "user", True))
        add_skills(_load_skills_from_dir_internal(str(Path(cwd) / CONFIG_DIR_NAME / "skills"), "project", True))

    user_skills_dir = str(Path(agent_dir) / "skills")
    project_skills_dir = str(Path(cwd) / CONFIG_DIR_NAME / "skills")

    def _is_under(target: str, root: str) -> bool:
        resolved_root = str(Path(root).resolve())
        resolved_target = str(Path(target).resolve())
        return resolved_target == resolved_root or resolved_target.startswith(resolved_root + os.sep)

    def _get_source(resolved_path: str) -> str:
        if not include_defaults:
            if _is_under(resolved_path, user_skills_dir):
                return "user"
            if _is_under(resolved_path, project_skills_dir):
                return "project"
        return "path"

    for raw_path in skill_paths:
        # Normalize ~
        expanded = raw_path.strip()
        if expanded == "~":
            expanded = str(Path.home())
        elif expanded.startswith("~/"):
            expanded = str(Path.home() / expanded[2:])

        resolved = str(Path(expanded).resolve()) if Path(expanded).is_absolute() else str(Path(cwd) / expanded)

        if not Path(resolved).exists():
            all_diagnostics.append(
                {
                    "type": "warning",
                    "message": "skill path does not exist",
                    "path": resolved,
                }
            )
            continue

        try:
            if Path(resolved).is_dir():
                source = _get_source(resolved)
                add_skills(_load_skills_from_dir_internal(resolved, source, True))
            elif Path(resolved).is_file() and resolved.endswith(".md"):
                skill, skill_diags = _load_skill_from_file(resolved, _get_source(resolved))
                if skill:
                    add_skills(LoadSkillsResult(skills=[skill], diagnostics=skill_diags))
                else:
                    all_diagnostics.extend(skill_diags)
            else:
                all_diagnostics.append(
                    {
                        "type": "warning",
                        "message": "skill path is not a markdown file",
                        "path": resolved,
                    }
                )
        except Exception as e:
            all_diagnostics.append(
                {
                    "type": "warning",
                    "message": str(e) or "failed to read skill path",
                    "path": resolved,
                }
            )

    return LoadSkillsResult(
        skills=list(skill_map.values()),
        diagnostics=[*all_diagnostics, *collision_diagnostics],
    )


def format_skills_for_prompt(skills: list[Skill]) -> str:
    """Format skills for inclusion in a system prompt.

    Uses XML format per Agent Skills standard.
    Skills with disable_model_invocation=True are excluded.
    """
    visible = [s for s in skills if not s.disable_model_invocation]

    if not visible:
        return ""

    lines = [
        "\n\nThe following skills provide specialized instructions for specific tasks.",
        "Use the read tool to load a skill's file when the task matches its description.",
        "When a skill file references a relative path, resolve it against the skill directory "
        "(parent of SKILL.md / dirname of the path) and use that absolute path in tool commands.",
        "",
        "<available_skills>",
    ]

    for skill in visible:
        lines.append("  <skill>")
        lines.append(f"    <name>{_xml_escape(skill.name)}</name>")
        lines.append(f"    <description>{_xml_escape(skill.description)}</description>")
        lines.append(f"    <location>{_xml_escape(skill.file_path)}</location>")
        lines.append("  </skill>")

    lines.append("</available_skills>")
    return "\n".join(lines)
