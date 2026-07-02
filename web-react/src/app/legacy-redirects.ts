// Ported from web/src/router.ts @ cf6b0f1 (the LEGACY_REDIRECTS map +
// applyLegacyRedirect; the router itself is react-router-dom here).
// Map flat v1.1 hashes to their v2 nested home. The redirect surface
// lives here so it's auditable from one read — lint-flat-route
// allowlists exactly this file.

const LEGACY_REDIRECTS: ReadonlyMap<string, string> = new Map([
  ['#/coworkers', '#/manage/coworkers'],
  ['#/mcp-servers', '#/manage/mcp-servers'],
  ['#/models', '#/manage/models'],
  ['#/credentials', '#/manage/credentials'],
  ['#/skills', '#/manage/skills'],
  ['#/admin/safety/rules', '#/manage/safety'],
  // Safety log moved from the Activity surface to Settings →
  // Governance. Both the v1.1 admin bookmark and the interim v2
  // Activity path land on the canonical settings home.
  ['#/admin/safety/decisions', '#/manage/safety-log'],
  ['#/activity/safety-decisions', '#/manage/safety-log'],
]);

/**
 * If `hash` matches a v1.1 flat path (with or without a trailing
 * sub-path) return the new v2 hash. Returns `null` for hashes that
 * do not need rewriting.
 *
 * Sub-paths and query strings are preserved: `#/skills/abc?x=1` →
 * `#/manage/skills/abc?x=1`. Bookmark fidelity is the whole point.
 */
export function applyLegacyRedirect(hash: string): string | null {
  for (const [oldPath, newPath] of LEGACY_REDIRECTS) {
    if (hash === oldPath) return newPath;
    if (hash.startsWith(oldPath + '/')) {
      return newPath + hash.slice(oldPath.length);
    }
    if (hash.startsWith(oldPath + '?')) {
      return newPath + hash.slice(oldPath.length);
    }
  }
  return null;
}
