// NAV_ENTRIES — the single constant that drives BOTH the sidebar and
// the router (spec §5.1). Labels/slugs/capabilities mirror the Lit
// settings-shell.ts; the capability strings are backend actions from
// permissions.py, resolved through `GET /me` (never role names).

export interface NavEntry {
  label: string;
  /** null → the chat route (`#/`); otherwise `#/manage/{slug}`. */
  slug: string | null;
  /** Capability required to SEE the entry; null → any authenticated
   *  user. Enforcement is server-side — this only hides nav rows. */
  requires: string | null;
}

export const NAV_ENTRIES: readonly NavEntry[] = [
  { label: 'Chat', slug: null, requires: null },
  { label: 'Coworkers', slug: 'coworkers', requires: null },
  { label: 'MCP servers', slug: 'mcp-servers', requires: 'mcp.configure' },
  { label: 'Skills', slug: 'skills', requires: null },
  { label: 'Models', slug: 'models', requires: null },
  { label: 'Credentials', slug: 'credentials', requires: 'credential.byok.manage' },
  { label: 'Approval policies', slug: 'approval-policies', requires: 'approval_policy.manage' },
  { label: 'Safety rules', slug: 'safety', requires: 'safety.read' },
  { label: 'Safety log', slug: 'safety-log', requires: 'safety.read' },
  { label: 'General', slug: 'general', requires: 'tenant.manage' },
  { label: 'Members', slug: 'members', requires: 'user.manage' },
  { label: 'Connected channels', slug: 'connected-channels', requires: null },
  { label: 'Appearance', slug: 'appearance', requires: null },
];

export function entryForSlug(slug: string): NavEntry | undefined {
  return NAV_ENTRIES.find((e) => e.slug === slug);
}
