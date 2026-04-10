"""Edit/diff utilities — Python port of packages/coding-agent/src/core/tools/edit-diff.ts."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path


def detect_line_ending(content: str) -> str:
    """Detect the dominant line ending in content. Returns '\\r\\n' or '\\n'."""
    crlf_count = content.count("\r\n")
    lf_count = content.count("\n") - crlf_count
    return "\r\n" if crlf_count > lf_count else "\n"


def normalize_to_lf(text: str) -> str:
    """Normalize all line endings to LF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def restore_line_endings(text: str, ending: str) -> str:
    """Convert LF line endings back to the specified ending."""
    if ending == "\r\n":
        return normalize_to_lf(text).replace("\n", "\r\n")
    return text


# Regex for normalizing text in fuzzy matching (matches TS normalize_for_fuzzy_match)
_CURLY_SINGLE_QUOTE_RE = re.compile(r"[\u2018\u2019\u201a\u201b]")
_CURLY_DOUBLE_QUOTE_RE = re.compile(r"[\u201c\u201d\u201e\u201f]")
_DASH_RE = re.compile(r"[\u2010\u2011\u2012\u2013\u2014\u2015\u2212]")
_UNICODE_SPACES_RE = re.compile(r"[\u00a0\u2002-\u200a\u202f\u205f\u3000]")

# Sets used for index mapping after fuzzy normalization
_CURLY_SINGLE_QUOTES = frozenset("\u2018\u2019\u201a\u201b")
_CURLY_DOUBLE_QUOTES = frozenset("\u201c\u201d\u201e\u201f")
_DASHES = frozenset("\u2010\u2011\u2012\u2013\u2014\u2015\u2212")
_UNICODE_SPACES = frozenset("\u00a0\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000")


def normalize_for_fuzzy_match(text: str) -> str:
    """Normalize text for fuzzy matching: strip trailing whitespace per line,
    normalize typographic quotes/dashes/spaces."""
    # Strip trailing whitespace from each line
    lines = text.split("\n")
    lines = [line.rstrip() for line in lines]
    result = "\n".join(lines)

    # Normalize typographic characters
    result = _CURLY_SINGLE_QUOTE_RE.sub("'", result)
    result = _CURLY_DOUBLE_QUOTE_RE.sub('"', result)
    result = _DASH_RE.sub("-", result)
    result = _UNICODE_SPACES_RE.sub(" ", result)

    return result


@dataclass
class FuzzyMatchResult:
    """Result of a fuzzy text search."""

    found: bool
    index: int
    match_length: int
    used_fuzzy_match: bool
    content_for_replacement: str  # original or fuzzy-normalized content


def fuzzy_find_text(content: str, old_text: str) -> FuzzyMatchResult:
    """Search for old_text in content, with optional fuzzy fallback.

    First tries exact match; if not found, tries fuzzy match by normalizing
    both sides. Returns the original content slice for replacement.
    """
    # Try exact match first
    idx = content.find(old_text)
    if idx != -1:
        return FuzzyMatchResult(
            found=True,
            index=idx,
            match_length=len(old_text),
            used_fuzzy_match=False,
            content_for_replacement=content[idx : idx + len(old_text)],
        )

    # Try fuzzy match: normalize both sides
    normalized_content = normalize_for_fuzzy_match(content)
    normalized_old = normalize_for_fuzzy_match(old_text)

    fuzzy_idx = normalized_content.find(normalized_old)
    if fuzzy_idx == -1:
        return FuzzyMatchResult(
            found=False,
            index=-1,
            match_length=0,
            used_fuzzy_match=True,
            content_for_replacement="",
        )

    # Map back to original content position
    # Since normalization strips trailing spaces per line, lengths may differ.
    # We need to find the original content slice corresponding to
    # normalized_content[fuzzy_idx:fuzzy_idx+len(normalized_old)].
    # Strategy: count characters in normalized content up to fuzzy_idx to find original position.
    orig_start = _map_normalized_index_to_original(content, normalized_content, fuzzy_idx)
    orig_end = _map_normalized_index_to_original(content, normalized_content, fuzzy_idx + len(normalized_old))
    match_length = orig_end - orig_start

    return FuzzyMatchResult(
        found=True,
        index=orig_start,
        match_length=match_length,
        used_fuzzy_match=True,
        content_for_replacement=content[orig_start:orig_end],
    )


def _normalizes_to(orig_char: str, norm_char: str) -> bool:
    """Return True if orig_char is a known 1-to-1 replacement that produces norm_char."""
    if norm_char == "'" and orig_char in _CURLY_SINGLE_QUOTES:
        return True
    if norm_char == '"' and orig_char in _CURLY_DOUBLE_QUOTES:
        return True
    if norm_char == "-" and orig_char in _DASHES:
        return True
    return norm_char == " " and orig_char in _UNICODE_SPACES


def _map_normalized_index_to_original(original: str, normalized: str, norm_idx: int) -> int:
    """Map a character index in normalized text back to original text.

    normalize_for_fuzzy_match either:
      - Deletes characters (trailing spaces stripped -> orig has extra chars)
      - Replaces characters 1-to-1 (unicode quotes/dashes -> ASCII)

    Walk orig_pos and norm_pos in parallel:
      - If chars match directly or via a known 1-to-1 replacement: advance both.
      - Otherwise: orig has an extra character that was deleted; advance only orig_pos.
    """
    if norm_idx == 0:
        return 0
    if norm_idx >= len(normalized):
        return len(original)

    orig_pos = 0
    norm_pos = 0

    while norm_pos < norm_idx and orig_pos < len(original):
        orig_char = original[orig_pos]
        norm_char = normalized[norm_pos]

        if orig_char == norm_char or _normalizes_to(orig_char, norm_char):
            # Direct match or known 1-to-1 replacement: both advance together
            orig_pos += 1
            norm_pos += 1
        else:
            # orig_char was deleted by normalization (e.g., trailing space)
            orig_pos += 1

    return orig_pos


def strip_bom(content: str) -> tuple[str, str]:
    """Strip UTF-8 BOM from content. Returns (bom, text_without_bom)."""
    if content.startswith("\ufeff"):
        return "\ufeff", content[1:]
    return "", content


@dataclass
class DiffResult:
    """Result of a diff operation."""

    diff: str
    first_changed_line: int | None


def generate_diff_string(
    old_content: str,
    new_content: str,
    context_lines: int = 4,
) -> DiffResult:
    """Generate a human-readable diff string between old and new content.

    Uses SequenceMatcher to find changes. Format:
      +NNN line  for added lines (NNN = new file line number)
      -NNN line  for removed lines (NNN = old file line number)
       NNN line  for context lines
       ... ...   for skipped sections
    """
    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()

    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    opcodes = matcher.get_opcodes()

    # Determine line number width for formatting
    max_line_num = max(len(old_lines), len(new_lines), 1)
    line_num_width = len(str(max_line_num))

    output_parts: list[str] = []
    first_changed_line: int | None = None

    # Build ranges of lines to show (context around changes)
    # Each opcode: (tag, i1, i2, j1, j2)
    # i1..i2 = old lines, j1..j2 = new lines

    # Find changed opcode indices
    changed_indices = [i for i, op in enumerate(opcodes) if op[0] != "equal"]

    if not changed_indices:
        return DiffResult(diff="", first_changed_line=None)

    output_parts = []
    shown_old_start = -1  # last old line shown

    for j, ci in enumerate(changed_indices):
        tag, i1, i2, j1, j2 = opcodes[ci]

        # Context before this change: from previous change's end
        ctx_start_old = max(i1 - context_lines, 0)

        # Find the actual new-line index corresponding to ctx_start_old
        # Walk backwards from j1
        ctx_start_new = max(j1 - (i1 - ctx_start_old), 0)

        # If there's a gap since last shown, emit ellipsis
        if ctx_start_old > shown_old_start + 1:
            output_parts.append(" ... ...")

        # Emit context lines before this change
        for k in range(ctx_start_old, i1):
            new_ln = ctx_start_new + (k - ctx_start_old) + 1
            line = old_lines[k] if k < len(old_lines) else ""
            output_parts.append(f" {str(new_ln).rjust(line_num_width)} {line}")
            shown_old_start = k

        if first_changed_line is None:
            first_changed_line = j1 + 1

        # Emit the changed lines
        if tag == "replace":
            for k in range(i1, i2):
                line = old_lines[k] if k < len(old_lines) else ""
                output_parts.append(f"-{str(k + 1).rjust(line_num_width)} {line}")
            for k in range(j1, j2):
                line = new_lines[k] if k < len(new_lines) else ""
                output_parts.append(f"+{str(k + 1).rjust(line_num_width)} {line}")
        elif tag == "delete":
            for k in range(i1, i2):
                line = old_lines[k] if k < len(old_lines) else ""
                output_parts.append(f"-{str(k + 1).rjust(line_num_width)} {line}")
        elif tag == "insert":
            for k in range(j1, j2):
                line = new_lines[k] if k < len(new_lines) else ""
                output_parts.append(f"+{str(k + 1).rjust(line_num_width)} {line}")

        shown_old_start = i2 - 1

        # Context after this change: use pre-computed next changed index (O(1) lookup)
        next_changed = changed_indices[j + 1] if j + 1 < len(changed_indices) else None
        next_ci = ci + 1 if ci + 1 < len(opcodes) else None
        if next_ci is not None:
            next_tag, _ni1, ni2, _nj1, _nj2 = opcodes[next_ci]
            if next_tag == "equal":
                # Show up to context_lines after current change
                ctx_end_old = min(i2 + context_lines, ni2)

                if next_changed is not None:
                    _ntag2, ni21, _ni22, _nj21, _nj22 = opcodes[next_changed]
                    # Context before next change is handled when we process next_changed
                    # So here we only emit up to context_lines after this change
                    ctx_end_old = min(i2 + context_lines, ni21)

                for k in range(i2, ctx_end_old):
                    new_ln = j2 + (k - i2) + 1
                    line = old_lines[k] if k < len(old_lines) else ""
                    output_parts.append(f" {str(new_ln).rjust(line_num_width)} {line}")
                    shown_old_start = k
        else:
            # Last change: emit trailing context
            ctx_end_old = min(i2 + context_lines, len(old_lines))
            for k in range(i2, ctx_end_old):
                new_ln = j2 + (k - i2) + 1
                line = old_lines[k] if k < len(old_lines) else ""
                output_parts.append(f" {str(new_ln).rjust(line_num_width)} {line}")
                shown_old_start = k

    return DiffResult(
        diff="\n".join(output_parts),
        first_changed_line=first_changed_line,
    )


@dataclass
class EditDiffResult:
    """Result of computing an edit diff (preview without applying)."""

    diff: str
    first_changed_line: int | None = None


@dataclass
class EditDiffError:
    """Error from computing an edit diff."""

    error: str


def compute_edit_diff(
    path: str,
    old_text: str,
    new_text: str,
    cwd: str,
) -> EditDiffResult | EditDiffError:
    """Compute the diff for an edit operation without applying it.

    Used for preview rendering in the TUI before the tool executes.
    """
    from .path_utils import resolve_to_cwd

    absolute_path = resolve_to_cwd(path, cwd)

    try:
        file_path = Path(absolute_path)
        if not file_path.is_file():
            return EditDiffError(error=f"File not found: {path}")

        raw_content = file_path.read_text(encoding="utf-8", errors="replace")

        # Strip BOM before matching
        _, content = strip_bom(raw_content)

        normalized_content = normalize_to_lf(content)
        normalized_old_text = normalize_to_lf(old_text)
        normalized_new_text = normalize_to_lf(new_text)

        # Find the old text using fuzzy matching
        match_result = fuzzy_find_text(normalized_content, normalized_old_text)

        if not match_result.found:
            return EditDiffError(
                error=f"Could not find the exact text in {path}. "
                "The old text must match exactly including all whitespace and newlines.",
            )

        # Count occurrences using fuzzy-normalized content
        fuzzy_content = normalize_for_fuzzy_match(normalized_content)
        fuzzy_old_text = normalize_for_fuzzy_match(normalized_old_text)
        occurrences = fuzzy_content.count(fuzzy_old_text)

        if occurrences > 1:
            return EditDiffError(
                error=f"Found {occurrences} occurrences of the text in {path}. "
                "The text must be unique. Please provide more context to make it unique.",
            )

        # Compute the new content
        base_content = match_result.content_for_replacement
        new_content = (
            base_content[: match_result.index]
            + normalized_new_text
            + base_content[match_result.index + match_result.match_length :]
        )

        # Check if it would actually change anything
        if base_content == new_content:
            return EditDiffError(
                error=f"No changes would be made to {path}. The replacement produces identical content.",
            )

        # Generate the diff
        diff_result = generate_diff_string(base_content, new_content)
        return EditDiffResult(
            diff=diff_result.diff,
            first_changed_line=diff_result.first_changed_line,
        )
    except Exception as e:
        return EditDiffError(error=str(e))
