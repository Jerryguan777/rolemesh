// Pinned skill constants — INV-5 (design §11).
//
// Python-side source of truth lives in
// `src/rolemesh/core/skills.py` (`SKILL_MANIFEST_NAME`); the
// matching string is asserted equal by
// `tests/test_skill_manifest_constant_consistency_ts.py`. Hand-edit
// either side and the test fails before a release.

export const SKILL_MANIFEST_NAME = "SKILL.md";
