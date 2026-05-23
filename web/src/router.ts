// Hash-router map.
//
// One central place for "what does each hash render". The app-shell
// sidebar items are derived from this list so adding a new page is
// one entry, not five edits across components. We deliberately keep
// the v1.1 design's hash router (no React Router) — Phase 0 is not
// the time to switch.
//
// Pages not yet implemented render the <rm-coming-soon> placeholder
// with their phase tag so the running dev UI advertises what is
// landing where.

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

export interface RouteDef {
  id: RouteId;
  /** Sidebar label. */
  label: string;
  /** Canonical hash (the sidebar link target). */
  hash: string;
  /**
   * Hash prefixes that should also highlight this sidebar item.
   * Order does not matter; first match wins in {@link matchRoute}.
   */
  prefixes: readonly string[];
  /**
   * Phase the page lands in. Used by the coming-soon placeholder
   * and (later) by feature flags. `null` means "shipped already".
   */
  phase: 1 | 2 | 3 | 4 | null;
  /**
   * Should the sidebar render this entry? Some routes
   * (e.g. /admin/safety/decisions) are reachable but
   * not first-class nav items.
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
    hash: '#/coworkers',
    prefixes: ['#/coworkers'],
    phase: 1,
    inSidebar: true,
    render: () => html`<rm-coworkers-page></rm-coworkers-page>`,
  },
  {
    id: 'mcp',
    label: 'MCP servers',
    hash: '#/mcp-servers',
    prefixes: ['#/mcp-servers'],
    phase: 2,
    inSidebar: true,
    render: () => html`<rm-mcp-servers-page></rm-mcp-servers-page>`,
  },
  {
    id: 'models',
    label: 'Models',
    hash: '#/models',
    prefixes: ['#/models'],
    phase: 2,
    inSidebar: true,
    render: () => html`<rm-models-page></rm-models-page>`,
  },
  {
    id: 'skills',
    label: 'Skills',
    hash: '#/skills',
    prefixes: ['#/skills'],
    // Phase 3 landed in 03b PR 4; the placeholder is replaced by the
    // live catalog page.
    phase: null,
    inSidebar: true,
    render: () => html`<rm-skills-page></rm-skills-page>`,
  },
  {
    id: 'credentials',
    label: 'Credentials',
    hash: '#/credentials',
    prefixes: ['#/credentials'],
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
    label: 'Approvals',
    hash: '#/approvals',
    prefixes: ['#/approvals'],
    // Phase 3 landed in 03a; the placeholder is replaced by the
    // live queue page.
    phase: null,
    inSidebar: true,
    render: () => html`<rm-approvals-page></rm-approvals-page>`,
  },
  {
    id: 'safety-rules',
    label: 'Safety',
    hash: '#/admin/safety/rules',
    // Highlight 'Safety' for both the rules and decisions sub-routes.
    prefixes: ['#/admin/safety/rules', '#/admin/safety'],
    phase: null,
    inSidebar: true,
    render: () => html`<rm-safety-rules-page></rm-safety-rules-page>`,
  },
  {
    id: 'safety-decisions',
    label: 'Safety decisions',
    hash: '#/admin/safety/decisions',
    prefixes: ['#/admin/safety/decisions'],
    phase: null,
    inSidebar: false,
    render: () => html`<rm-safety-decisions-page></rm-safety-decisions-page>`,
  },
];

const CHAT: RouteDef = ROUTES[0];

/** Resolve a hash to the matching route, defaulting to chat. */
export function matchRoute(hash: string): RouteDef {
  // Longest-prefix-wins to keep `#/admin/safety/decisions` from
  // resolving to the `#/admin/safety/rules` entry purely because
  // 'rules' is registered first.
  let best: RouteDef | null = null;
  let bestLen = -1;
  for (const r of ROUTES) {
    for (const p of r.prefixes) {
      if (!p) continue;
      if (hash === p || hash.startsWith(p + '/') || hash === p) {
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
