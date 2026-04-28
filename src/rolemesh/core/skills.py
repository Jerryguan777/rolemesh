"""Skill-related domain helpers: name / path validation, frontmatter
parsing-on-write, frontmatter merging-at-projection.

The DB schema enforces the same rules at write time via CHECK
constraints and triggers, but doing the validation in the application
layer first gives clean 400 errors instead of relying on a generic
``IntegrityError`` from psycopg.

The three public entry points are:

* ``validate_skill_name`` — applied before any insert.
* ``validate_skill_file_path`` — applied to every file path in a
  skill payload.
* ``parse_inbound_skill_md`` — accepts the raw ``files["SKILL.md"]``
  text plus optional structured ``frontmatter_common`` /
  ``frontmatter_backend`` from the request body, returns
  ``(frontmatter_common, frontmatter_backend, body)`` for storage.
* ``merge_frontmatter_for_backend`` — combines stored common +
  backend-specific frontmatter for a target backend, drops other
  backends' keys. Used by the projector at spawn time.
* ``serialize_skill_md`` — wraps the merged frontmatter in
  ``---`` markers and appends body. Used by the projector.

The frontmatter field allowlist is intentionally explicit: unknown
keys are rejected so the design has to be touched whenever an SDK
introduces a new frontmatter knob.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Allowlists
# ---------------------------------------------------------------------------

# Common: read by both runtimes.
COMMON_FRONTMATTER_KEYS: frozenset[str] = frozenset({"name", "description"})

# Claude Agent SDK accepts these per its skills documentation.
CLAUDE_FRONTMATTER_KEYS: frozenset[str] = frozenset(
    {
        "argument-hint",
        "model",
        "allowed-tools",
    }
)

# Pi (pi-mono) skill loader.
PI_FRONTMATTER_KEYS: frozenset[str] = frozenset({"disable_model_invocation"})

# Backend names registered in ``frontmatter_backend`` JSONB. Adding a
# new backend means adding its name here and a matching key set above.
KNOWN_BACKENDS: frozenset[str] = frozenset({"claude", "pi"})

# All known backend-specific keys, used to reject keys-with-no-home.
_BACKEND_KEYS_BY_NAME: dict[str, frozenset[str]] = {
    "claude": CLAUDE_FRONTMATTER_KEYS,
    "pi": PI_FRONTMATTER_KEYS,
}

DESCRIPTION_MIN_LENGTH = 20
DESCRIPTION_MAX_LENGTH = 1024

# Skill name regex matches the DB CHECK in pg.py — keep them in sync.
_SKILL_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")

# Skill file path: positive whitelist. Each path segment must start
# with [A-Za-z0-9_] and only contain [A-Za-z0-9_.\-]. The DB enforces
# the same regex; this is the application-layer mirror.
_SKILL_FILE_PATH_RE = re.compile(
    r"^[A-Za-z0-9_][A-Za-z0-9_.-]*(/[A-Za-z0-9_][A-Za-z0-9_.-]*)*$"
)
# Defense in depth: forbid any segment that is purely dots
# (`.`, `..`, `...`). Even if the whitelist regex above is widened
# accidentally, this still rejects traversal segments.
_SKILL_FILE_PATH_DOT_SEGMENT_RE = re.compile(r"(^|/)\.+($|/)")

SKILL_MD_FILENAME = "SKILL.md"


class SkillValidationError(ValueError):
    """Domain validation error. REST handlers translate to 400."""


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def validate_skill_name(name: str) -> None:
    if not isinstance(name, str) or not _SKILL_NAME_RE.match(name):
        raise SkillValidationError(
            f"invalid skill name {name!r}: must match "
            r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$"
        )


def validate_skill_file_path(path: str) -> None:
    if not isinstance(path, str) or len(path) == 0:
        raise SkillValidationError("skill file path must be non-empty")
    if not _SKILL_FILE_PATH_RE.match(path):
        raise SkillValidationError(
            f"invalid skill file path {path!r}: each segment must start with "
            "an alphanumeric or underscore and contain only A-Za-z0-9, _, ., -"
        )
    if _SKILL_FILE_PATH_DOT_SEGMENT_RE.search(path):
        raise SkillValidationError(
            f"invalid skill file path {path!r}: dot-only segments are forbidden"
        )


def _validate_description(value: object) -> str:
    if not isinstance(value, str):
        raise SkillValidationError("frontmatter 'description' must be a string")
    if len(value) < DESCRIPTION_MIN_LENGTH:
        raise SkillValidationError(
            f"frontmatter 'description' too short ({len(value)} chars); "
            f"minimum is {DESCRIPTION_MIN_LENGTH}"
        )
    if len(value) > DESCRIPTION_MAX_LENGTH:
        raise SkillValidationError(
            f"frontmatter 'description' too long ({len(value)} chars); "
            f"maximum is {DESCRIPTION_MAX_LENGTH}"
        )
    return value


# ---------------------------------------------------------------------------
# Frontmatter parsing on write
# ---------------------------------------------------------------------------


_FRONTMATTER_BLOCK_RE = re.compile(
    r"\A---\r?\n(?P<yaml>.*?)\r?\n---\r?\n?(?P<body>.*)\Z",
    re.DOTALL,
)


def _split_frontmatter_block(text: str) -> tuple[dict[str, Any] | None, str]:
    """Return (parsed_frontmatter, body). If no leading ``---`` block,
    returns (None, text).
    """
    m = _FRONTMATTER_BLOCK_RE.match(text)
    if m is None:
        return None, text
    try:
        parsed = yaml.safe_load(m.group("yaml"))
    except yaml.YAMLError as exc:
        raise SkillValidationError(f"SKILL.md frontmatter is not valid YAML: {exc}") from exc
    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        raise SkillValidationError(
            "SKILL.md frontmatter must be a YAML mapping"
        )
    return parsed, m.group("body")


def _route_frontmatter_keys(
    raw: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Route an unstructured frontmatter dict into (common, backend).

    Unknown keys raise SkillValidationError with the offending key.
    """
    common: dict[str, Any] = {}
    backend: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise SkillValidationError(
                f"frontmatter keys must be strings, got {type(key).__name__}"
            )
        if key in COMMON_FRONTMATTER_KEYS:
            common[key] = value
            continue
        placed = False
        for backend_name, allowed in _BACKEND_KEYS_BY_NAME.items():
            if key in allowed:
                backend.setdefault(backend_name, {})[key] = value
                placed = True
                break
        if not placed:
            raise SkillValidationError(
                f"unknown frontmatter key {key!r}: not in common or any "
                f"registered backend allowlist (claude/pi)"
            )
    return common, backend


def parse_inbound_skill_md(
    skill_md_text: str,
    *,
    frontmatter_common_override: dict[str, Any] | None = None,
    frontmatter_backend_override: dict[str, dict[str, Any]] | None = None,
    expected_skill_name: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], str]:
    """Parse a SKILL.md payload into separated storage form.

    The returned ``(frontmatter_common, frontmatter_backend, body)``
    triple is what the DB stores: JSONB columns for the two
    frontmatter dicts, ``skill_files.SKILL.md.content`` for the body
    (no leading ``---`` block).

    Top-level overrides win over inline frontmatter.

    Validates:
    - YAML well-formedness
    - All keys are in the common or a backend allowlist
    - All known backends in ``frontmatter_backend_override`` are in KNOWN_BACKENDS
    - ``frontmatter_common.name`` (if set) equals ``expected_skill_name``
    - ``frontmatter_common.description`` is present and length-bounded
    """
    parsed_inline, body = _split_frontmatter_block(skill_md_text)
    if parsed_inline is None:
        common: dict[str, Any] = {}
        backend: dict[str, dict[str, Any]] = {}
    else:
        common, backend = _route_frontmatter_keys(parsed_inline)

    if frontmatter_common_override:
        for k in frontmatter_common_override:
            if k not in COMMON_FRONTMATTER_KEYS:
                raise SkillValidationError(
                    f"frontmatter_common override has key {k!r} which is not "
                    "in the common allowlist"
                )
        common.update(frontmatter_common_override)

    if frontmatter_backend_override:
        for backend_name, fields in frontmatter_backend_override.items():
            if backend_name not in KNOWN_BACKENDS:
                raise SkillValidationError(
                    f"unknown backend {backend_name!r} in frontmatter_backend; "
                    f"must be one of {sorted(KNOWN_BACKENDS)}"
                )
            allowed = _BACKEND_KEYS_BY_NAME[backend_name]
            for k in fields:
                if k not in allowed:
                    raise SkillValidationError(
                        f"frontmatter_backend.{backend_name}.{k} is not in the "
                        f"{backend_name} allowlist"
                    )
            backend.setdefault(backend_name, {}).update(fields)

    common.setdefault("name", expected_skill_name)
    if common.get("name") != expected_skill_name:
        raise SkillValidationError(
            f"frontmatter 'name' ({common['name']!r}) does not match the "
            f"skill's name ({expected_skill_name!r})"
        )

    if "description" not in common:
        raise SkillValidationError(
            "frontmatter 'description' is required (in inline SKILL.md "
            "frontmatter or in frontmatter_common)"
        )
    _validate_description(common["description"])

    return common, backend, body


# ---------------------------------------------------------------------------
# Frontmatter merging at projection time
# ---------------------------------------------------------------------------


def merge_frontmatter_for_backend(
    frontmatter_common: dict[str, Any],
    frontmatter_backend: dict[str, dict[str, Any]],
    target_backend: str,
) -> dict[str, Any]:
    """Build the per-backend frontmatter to write into SKILL.md.

    Keys from other backends are dropped — that's the whole point of
    the structured storage. ``target_backend`` accepts canonical
    backend names and the alias ``claude-code`` (which maps to
    ``claude`` in our storage).
    """
    canonical = "claude" if target_backend in ("claude", "claude-code") else target_backend
    if canonical not in KNOWN_BACKENDS:
        raise SkillValidationError(
            f"unknown target backend {target_backend!r}; "
            f"must be one of {sorted(KNOWN_BACKENDS) + ['claude-code']}"
        )
    merged: dict[str, Any] = dict(frontmatter_common)
    merged.update(frontmatter_backend.get(canonical, {}))
    return merged


def serialize_skill_md(merged_frontmatter: dict[str, Any], body: str) -> str:
    """Emit the SKILL.md text written into the spawn directory.

    Uses ``yaml.safe_dump`` with ``sort_keys=False`` to preserve the
    field order callers care about (``name`` and ``description``
    typically come first); the DB row stores frontmatter as JSONB so
    we cannot rely on Python's dict insertion order surviving — but
    when the splitter parses an inline block it does preserve order
    via ``yaml.safe_load``'s default mapping handler.
    """
    yaml_text = yaml.safe_dump(
        merged_frontmatter,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    ).rstrip("\n")
    return f"---\n{yaml_text}\n---\n{body}"
