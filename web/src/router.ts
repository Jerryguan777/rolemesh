// Hash router — v2 IA helpers + legacy bookmark redirects.
//
// v2 splits the SPA into three top-level shells (design §2):
//
//   `#/`            → `<rm-chat-shell>`
//   `#/manage/*`    → `<rm-settings-shell>`
//   `#/activity/*`  → `<rm-activity-shell>` (mostly placeholder until v2-C)
//
// The shells pick what to render internally; this module only owns
// the top-level resolution + the redirect surface for v1.1 bookmarks
// (`#/coworkers`, `#/admin/safety/decisions`, …). Each redirect goes
// through `location.replace` so the legacy hash does not pollute
// browser back-button history.

export type TopLevelShell = 'chat' | 'manage' | 'activity';

/**
 * Map flat v1.1 hashes to their v2 nested home. The redirect surface
 * lives here so it's auditable from one read.
 */
const LEGACY_REDIRECTS: ReadonlyMap<string, string> = new Map([
  ['#/coworkers',                '#/manage/coworkers'],
  ['#/mcp-servers',              '#/manage/mcp-servers'],
  ['#/models',                   '#/manage/models'],
  ['#/credentials',              '#/manage/credentials'],
  ['#/skills',                   '#/manage/skills'],
  ['#/approvals',                '#/manage/approval-policies'],
  ['#/admin/safety/rules',       '#/manage/safety'],
  ['#/admin/safety/decisions',   '#/activity/safety-decisions'],
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

/**
 * Resolve the top-level v2 shell that owns `hash`. `<rm-app>` calls
 * this on mount and on every hashchange to pick which shell custom
 * element to render.
 */
export function topLevelShell(hash: string): TopLevelShell {
  if (hash.startsWith('#/manage')) return 'manage';
  if (hash.startsWith('#/activity')) return 'activity';
  return 'chat';
}

/**
 * Install the redirect handler. Returns a teardown function for
 * tests. Calls `location.replace` (NOT `assign`) so the legacy hash
 * does not pollute the back-button history; pressing Back after a
 * redirect should return to wherever the user actually came from,
 * not bounce them through the deprecated URL.
 *
 * The redirect runs once on install too, so a bookmarked `#/coworkers`
 * gets rewritten before any shell mounts.
 */
export function installLegacyRedirects(): () => void {
  const tryRedirect = () => {
    const next = applyLegacyRedirect(location.hash);
    if (next && next !== location.hash) {
      // `location.replace` accepts a full URL; preserve pathname +
      // search so origin and query params survive the rewrite.
      const url = location.pathname + location.search + next;
      location.replace(url);
    }
  };
  tryRedirect();
  window.addEventListener('hashchange', tryRedirect);
  return () => window.removeEventListener('hashchange', tryRedirect);
}
