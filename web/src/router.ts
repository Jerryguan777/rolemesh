// Hash router.
//
// One central place for "what does each hash render". The legacy
// v1.1 `<rm-app-shell>` sidebar items are derived from `ROUTES`, so
// adding a new page is one entry, not five edits across components.
// We deliberately keep the hash-based router (no React Router) —
// design §6.1.
//
// v2 IA — three top-level shells (design §2):
//
//   `#/`            → chat shell (`<rm-chat-shell>`, PR 3)
//   `#/manage/*`    → settings shell (`<rm-settings-shell>`, PR 4)
//   `#/activity/*`  → activity shell (v2-C; placeholder for now)
//
// Old v1.1 bookmarks (`#/coworkers`, `#/mcp-servers`, …) are NOT
// broken: a `location.replace` redirect map below rewrites them to
// their new nested home on first hashchange. We use replace, not
// assign, so the browser back button does not get stuck on the old
// path. Each existing route also gains the new hash as an extra
// prefix so the same route definition continues to highlight under
// the v1.1 app-shell while PRs 3/4 land.

import { html, type TemplateResult } from 'lit';

export type RouteId =
  | 'chat'
  | 'coworkers'
  | 'mcp'
  | 'models'
  | 'skills'
  | 'credentials'
  | 'bindings'
  | 'approvals'
  | 'safety-rules'
  | 'safety-decisions';

export type TopLevelShell = 'chat' | 'manage' | 'activity';

export interface RouteDef {
  id: RouteId;
  /** Sidebar label. */
  label: string;
  /** Canonical hash (the sidebar link target). */
  hash: string;
  /**
   * Hash prefixes that should also highlight this sidebar item.
   * Order does not matter; longest match wins in {@link matchRoute}.
   */
  prefixes: readonly string[];
  /**
   * Phase the page lands in. Used by the coming-soon placeholder
   * and (later) by feature flags. `null` means "shipped already".
   */
  phase: 1 | 2 | 3 | 4 | null;
  /**
   * Should the v1.1 sidebar render this entry? Some routes
   * (e.g. /admin/safety/decisions) are reachable but not first-class
   * nav items. v2 sidebars (chat-shell + settings-shell) build their
   * nav independently and ignore this field.
   */
  inSidebar: boolean;
  /** Lit template factory for the page body. */
  render: () => TemplateResult;
}

// Sidebar ordering: chat first; rest follow phase progression
// (Coworkers / MCP / Models / Skills / Credentials / Bindings /
// Approvals / Safety) per the session prompt's resolution of
// open question #3.
export const ROUTES: readonly RouteDef[] = [
  {
    id: 'chat',
    label: 'Chat',
    hash: '#/',
    prefixes: ['#/', '', '#'],
    phase: null,
    inSidebar: true,
    render: () => html`<rm-chat-panel class="flex-1 min-h-0"></rm-chat-panel>`,
  },
  {
    id: 'coworkers',
    label: 'Coworkers',
    hash: '#/manage/coworkers',
    // Keep the v1.1 flat hash as a prefix so the legacy app-shell
    // can match it during the transition; redirects below rewrite
    // user-typed flat hashes to the canonical nested one.
    prefixes: ['#/manage/coworkers', '#/coworkers'],
    phase: 1,
    inSidebar: true,
    render: () => html`<rm-coworkers-page></rm-coworkers-page>`,
  },
  {
    id: 'mcp',
    label: 'MCP servers',
    hash: '#/manage/mcp-servers',
    prefixes: ['#/manage/mcp-servers', '#/mcp-servers'],
    phase: 2,
    inSidebar: true,
    render: () => html`<rm-mcp-servers-page></rm-mcp-servers-page>`,
  },
  {
    id: 'models',
    label: 'Models',
    hash: '#/manage/models',
    prefixes: ['#/manage/models', '#/models'],
    phase: 2,
    inSidebar: true,
    render: () => html`<rm-models-page></rm-models-page>`,
  },
  {
    id: 'skills',
    label: 'Skills',
    hash: '#/manage/skills',
    prefixes: ['#/manage/skills', '#/skills'],
    // Phase 3 landed in 03b PR 4; the placeholder is replaced by the
    // live catalog page.
    phase: null,
    inSidebar: true,
    render: () => html`<rm-skills-page></rm-skills-page>`,
  },
  {
    id: 'credentials',
    label: 'Credentials',
    hash: '#/manage/credentials',
    prefixes: ['#/manage/credentials', '#/credentials'],
    phase: 2,
    inSidebar: true,
    render: () => html`<rm-credentials-page></rm-credentials-page>`,
  },
  {
    id: 'bindings',
    label: 'Bindings',
    hash: '#/bindings',
    prefixes: ['#/bindings'],
    phase: 2,
    inSidebar: true,
    render: () => html`<rm-coming-soon label="Bindings" phase=${2}></rm-coming-soon>`,
  },
  {
    id: 'approvals',
    label: 'Approval policies',
    hash: '#/manage/approval-policies',
    // The v1.1 page is a queue (one approval per row); v2 reuses it
    // unchanged under the new "approval policies" slot until the
    // dedicated policies UI lands in a later cycle. The label
    // mismatch is acceptable for the cosmetic-only slot.
    prefixes: ['#/manage/approval-policies', '#/approvals'],
    phase: null,
    inSidebar: true,
    render: () => html`<rm-approvals-page></rm-approvals-page>`,
  },
  {
    id: 'safety-rules',
    label: 'Safety',
    hash: '#/manage/safety',
    // Highlight 'Safety' for the v2 home and any legacy admin path.
    prefixes: ['#/manage/safety', '#/admin/safety/rules', '#/admin/safety'],
    phase: null,
    inSidebar: true,
    render: () => html`<rm-safety-rules-page></rm-safety-rules-page>`,
  },
  {
    id: 'safety-decisions',
    label: 'Safety decisions',
    // Decisions are an Activity surface, not a Settings page — they
    // describe what happened, not what to configure. The v2-C
    // activity shell will own this; until then it renders through
    // the v1.1 app-shell via the legacy prefix.
    hash: '#/activity/safety-decisions',
    prefixes: ['#/activity/safety-decisions', '#/admin/safety/decisions'],
    phase: null,
    inSidebar: false,
    render: () => html`<rm-safety-decisions-page></rm-safety-decisions-page>`,
  },
];

const CHAT: RouteDef = ROUTES[0];

/** Resolve a hash to the matching route, defaulting to chat. */
export function matchRoute(hash: string): RouteDef {
  // Longest-prefix-wins so `#/admin/safety/decisions` does not
  // resolve to the `#/admin/safety` (rules) entry purely because of
  // registration order.
  let best: RouteDef | null = null;
  let bestLen = -1;
  for (const r of ROUTES) {
    for (const p of r.prefixes) {
      if (!p) continue;
      if (hash === p || hash.startsWith(p + '/')) {
        if (p.length > bestLen) {
          best = r;
          bestLen = p.length;
        }
      }
    }
  }
  if (best) return best;
  // Empty hash / '#' / '#/' all land on chat.
  if (hash === '' || hash === '#' || hash === '#/') return CHAT;
  return CHAT;
}

// ─────────────────────────────────────────────────────────────────
// v2 IA helpers
// ─────────────────────────────────────────────────────────────────

/**
 * Map flat v1.1 hashes to their v2 nested home. Wired into the
 * `installLegacyRedirects()` hashchange handler below; calling code
 * never invokes this directly. Listed in one table so the redirect
 * surface is auditable from a single read.
 *
 * Kept as a Map (not Object) so iteration order matches insertion
 * and the longest-prefix lookup in {@link applyLegacyRedirect} is
 * deterministic.
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
 * Sub-paths are preserved: `#/skills/abc` → `#/manage/skills/abc`.
 * That keeps skill detail bookmarks working after the redirect.
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
 * Compute the top-level v2 shell that owns the current hash. Used by
 * `<rm-app>` (PR 3) to pick between chat / settings / activity
 * shells; legacy app-shell ignores it.
 */
export function topLevelShell(hash: string): TopLevelShell {
  if (hash.startsWith('#/manage')) return 'manage';
  if (hash.startsWith('#/activity')) return 'activity';
  return 'chat';
}

/**
 * Install the redirect handler. Returns a teardown function for
 * tests. Calls `location.replace` (NOT `assign`) so the legacy hash
 * does not pollute the back-button history; if a user pressed Back
 * after a redirect, they should return to wherever they came from,
 * not bounce through the deprecated URL.
 *
 * The redirect runs on initial load too so a bookmarked `#/coworkers`
 * gets rewritten before any shell mounts.
 */
export function installLegacyRedirects(): () => void {
  const tryRedirect = () => {
    const next = applyLegacyRedirect(location.hash);
    if (next && next !== location.hash) {
      // `location.replace` requires a full URL; build it from the
      // current page so query params and origin survive intact.
      const url = location.pathname + location.search + next;
      location.replace(url);
    }
  };
  tryRedirect();
  window.addEventListener('hashchange', tryRedirect);
  return () => window.removeEventListener('hashchange', tryRedirect);
}
