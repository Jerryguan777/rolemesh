// Copied from web/src/api/skill_constants.ts @ b443846; keep in sync manually until workspace extraction.
// Pinned skill constants — INV-5 (design §11).
//
// Python-side source of truth lives in
// `src/rolemesh/core/skills.py` (`SKILL_MANIFEST_NAME`); the
// matching string is asserted equal by
// `tests/test_skill_manifest_constant_consistency_ts.py`. Hand-edit
// either side and the test fails before a release.

export const SKILL_MANIFEST_NAME = "SKILL.md";

// Mirrors the Python ``SKILL_FILE_PATH_RE`` (whitelist; no leading dot,
// no traversal). The DB CHECK enforces the same rule on the server,
// but rejecting bad paths at the form level surfaces the error
// immediately without a round trip.
export const SKILL_FILE_PATH_RE =
  /^[A-Za-z0-9_][A-Za-z0-9_.-]*(\/[A-Za-z0-9_][A-Za-z0-9_.-]*)*$/;

// Validates a single segment doesn't collapse to dots-only ("..", "...").
// Combined with the regex above this rejects every traversal form
// (``../``, ``a/..``, ``a/../b``) without writing a separate parser.
export function isValidSkillFilePath(path: string): boolean {
  if (!SKILL_FILE_PATH_RE.test(path)) return false;
  return path.split("/").every((seg) => !/^\.+$/.test(seg));
}

