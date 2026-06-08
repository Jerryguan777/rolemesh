// <rm-settings-shell> — v2 settings chrome.
//
// Layout (per docs/webui-ui-redesign-v2-prototype.html `.settings`
// surface, full-page variant):
//
//   ┌──────────────┬──────────────────────────────────────────┐
//   │ Settings  ×  │ Page title                               │
//   │              ├──────────────────────────────────────────┤
//   │ Coworkers    │                                          │
//   │              │   <slot>                                 │
//   │ BUILDING…    │   v1.1 page component, slotted unchanged │
//   │  MCP servers │                                          │
//   │  Skills      │                                          │
//   │  …           │                                          │
//   └──────────────┴──────────────────────────────────────────┘
//
// Cosmetic-only reskin: each `#/manage/*` sub-route slots the same
// v1.1 page component it did before, wrapped in a thin padding +
// background container. Business logic, API calls, form fields are
// 0-touched (locked decision; v2-A prompt).
//
// The shell decides which page to render itself rather than going
// through `<rm-router-outlet>` — that outlet was app-shell's lookup,
// and we are replacing app-shell. The route → component map lives
// in `MANAGE_PAGES` below and is intentionally explicit so it's
// auditable from one read.

import { LitElement, html, nothing, type TemplateResult } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { keyed } from 'lit/directives/keyed.js';

import { currentMe, hasCapability } from '../auth/capabilities.js';

import {
  iconBook,
  iconChevronDown,
  iconChip,
  iconClipboardCheck,
  iconClose,
  iconFileText,
  iconHome,
  iconKey,
  iconLink,
  iconServer,
  iconSettings,
  iconShield,
  iconSun,
  iconUser,
  iconUsers,
} from './icons.js';
import type { SVGTemplateResult } from 'lit';
import './reauth-banner.js';
import './coworkers-page.js';
import './mcp-servers-page.js';
import './skills-page.js';
import './models-page.js';
import './credentials-page.js';
import './safety-rules-page.js';
import './safety-decisions-page.js';
import './approval-policies-page.js';
import './appearance-page.js';
import './coming-soon.js';
import './skill-detail-page.js';
import './connected-channels-page.js';
import './access-denied-page.js';

interface NavEntry {
  /** Sub-path under `#/manage/`. */
  slug: string;
  label: string;
  /** Small SVG that sits left of the label in the rail. Matches the
   *  prototype `.ni > svg` pattern (docs/webui-ui-redesign-v2-prototype.html
   *  lines 489-502). */
  icon: () => SVGTemplateResult;
  /** Optional badge text shown right-aligned in the nav row. */
  badge?: string;
  /** Renders the page body. */
  render: () => TemplateResult;
  /** Capability required to see this entry (spec §3). `null` means any
   *  authenticated user. At render time the shell hides entries whose
   *  `requires` is set and not held by `currentMe()`. The strings here
   *  must match the backend action names in `permissions.py` EXACTLY —
   *  a typo silently hides the entry for everyone. The frontend keeps NO
   *  role->capability matrix; it only names the capability each page
   *  needs and reads `me.capabilities` from the wire. */
  requires: string | null;
}

interface NavGroup {
  /** Group heading shown in uppercase. Empty string = ungrouped
   *  (used by the Coworkers entry that sits at the top by itself). */
  heading: string;
  entries: NavEntry[];
}

/**
 * The 12 settings pages. Order matches the v2-A prompt's grouping
 * (Coworkers · Building blocks · Governance · Workspace · Account).
 *
 * Each entry just hands off to a v1.1 component — `coworkers-page`
 * et al — with the exception of `appearance-page` (new, shows
 * detected system theme) and the two `coming-soon` placeholders
 * (general + members; v3).
 *
 * Every entry carries a `requires` capability (spec §3); the shell
 * filters the rail by it at render time. The strings are the only
 * role-related constant here — and they name a *capability*, never a
 * role. The role->capability mapping lives solely in the backend.
 */
const NAV_GROUPS: NavGroup[] = [
  {
    heading: '',
    entries: [
      {
        slug: 'coworkers',
        label: 'Coworkers',
        icon: () => iconUser(16),
        render: () => html`<rm-coworkers-page></rm-coworkers-page>`,
        requires: null,
      },
    ],
  },
  {
    heading: 'Building blocks',
    entries: [
      {
        slug: 'mcp-servers',
        label: 'MCP servers',
        icon: () => iconServer(16),
        render: () => html`<rm-mcp-servers-page></rm-mcp-servers-page>`,
        requires: 'mcp.configure',
      },
      {
        slug: 'skills',
        label: 'Skills',
        icon: () => iconBook(16),
        render: () => html`<rm-skills-page></rm-skills-page>`,
        requires: null,
      },
      {
        slug: 'models',
        label: 'Models',
        icon: () => iconChip(16),
        render: () => html`<rm-models-page></rm-models-page>`,
        requires: null,
      },
      {
        slug: 'credentials',
        label: 'Credentials',
        icon: () => iconKey(16),
        render: () => html`<rm-credentials-page></rm-credentials-page>`,
        requires: 'credential.byok.manage',
      },
    ],
  },
  {
    heading: 'Governance',
    entries: [
      {
        slug: 'approval-policies',
        label: 'Approval policies',
        icon: () => iconClipboardCheck(16),
        render: () =>
          html`<rm-approval-policies-page></rm-approval-policies-page>`,
        requires: 'approval_policy.manage',
      },
      {
        slug: 'safety',
        label: 'Safety rules',
        icon: () => iconShield(16),
        render: () => html`<rm-safety-rules-page></rm-safety-rules-page>`,
        requires: 'safety.read',
      },
      {
        slug: 'safety-log',
        label: 'Safety log',
        icon: () => iconFileText(16),
        render: () => html`<rm-safety-decisions-page></rm-safety-decisions-page>`,
        requires: 'safety.read',
      },
    ],
  },
  {
    heading: 'Workspace',
    entries: [
      {
        slug: 'general',
        label: 'General',
        icon: () => iconHome(16),
        render: () =>
          html`<rm-coming-soon label="General" phase=${3}></rm-coming-soon>`,
        requires: 'tenant.manage',
      },
      {
        // D3 — Members stays a coming-soon stub in v3; this PR only adds
        // the nav gate. No real members-page is built here.
        slug: 'members',
        label: 'Members',
        icon: () => iconUsers(16),
        render: () =>
          html`<rm-coming-soon label="Members" phase=${3}></rm-coming-soon>`,
        requires: 'user.manage',
      },
    ],
  },
  {
    heading: 'Account',
    entries: [
      {
        slug: 'connected-channels',
        label: 'Connected channels',
        icon: () => iconLink(16),
        render: () =>
          html`<rm-connected-channels-page></rm-connected-channels-page>`,
        requires: null,
      },
      {
        slug: 'appearance',
        label: 'Appearance',
        icon: () => iconSun(16),
        render: () => html`<rm-appearance-page></rm-appearance-page>`,
        requires: null,
      },
    ],
  },
];

/** Flat lookup map. Computed once at module load. */
const ENTRY_BY_SLUG: Record<string, NavEntry> = Object.fromEntries(
  NAV_GROUPS.flatMap((g) => g.entries.map((e) => [e.slug, e])),
);

const DEFAULT_SLUG = 'coworkers';

/**
 * Resolve `#/manage/<slug>[/<rest>]` to a slug. Sub-paths (e.g.
 * `#/manage/skills/abc`) collapse to the parent slug — the v1.1
 * page handles internal routing via the same URL itself.
 */
export function slugFromHash(hash: string): string {
  const m = hash.match(/^#\/manage\/([^/?#]+)/);
  if (!m) return DEFAULT_SLUG;
  const slug = m[1];
  return slug in ENTRY_BY_SLUG ? slug : DEFAULT_SLUG;
}

@customElement('rm-settings-shell')
export class RmSettingsShell extends LitElement {
  @state() private hash: string = location.hash;

  protected override createRenderRoot() {
    // Light DOM — v1.1 page components rely on Tailwind classes that
    // are resolved at the document root.
    return this;
  }

  override connectedCallback() {
    super.connectedCallback();
    // Inline style overrides the <style> rule (specificity) — set
    // flex-column so the reauth-banner can sit above the grid
    // layout on first paint, before the rendered <style> applies.
    this.style.display = 'flex';
    this.style.flexDirection = 'column';
    this.style.height = '100%';
    window.addEventListener('hashchange', this.onHashChange);
  }

  override disconnectedCallback() {
    super.disconnectedCallback();
    window.removeEventListener('hashchange', this.onHashChange);
  }

  private onHashChange = () => {
    this.hash = location.hash;
  };

  private navigate(slug: string) {
    const target = `#/manage/${slug}`;
    if (location.hash !== target) {
      location.hash = target;
    }
  }

  private backToChat = () => {
    location.hash = '#/';
  };

  /**
   * Whether `currentMe()` is allowed to see `entry`. An entry with a
   * `null` requires is visible to any authenticated user; otherwise the
   * user must hold the named capability. This is the single gate — the
   * shell never inspects role names, only the wire `capabilities` list
   * through `hasCapability`.
   */
  private canSee(entry: NavEntry): boolean {
    return !entry.requires || hasCapability(entry.requires);
  }

  override render(): TemplateResult {
    // Defensive null-guard. PR1's bootstrap guarantees `me` is in cache
    // before any shell mounts (app.ts holds authState at 'loading' until
    // setMe() runs), so this branch should not be reached in practice.
    const me = currentMe();
    if (!me) {
      return html`<div class="ss-loading" data-testid="settings-loading">
        Loading…
      </div>`;
    }

    // Filter to the groups (and entries) this user can see. A group with
    // every entry hidden disappears entirely — heading included.
    const visibleGroups = NAV_GROUPS.map((g) => ({
      ...g,
      entries: g.entries.filter((e) => this.canSee(e)),
    })).filter((g) => g.entries.length > 0);

    const activeSlug = slugFromHash(this.hash);
    const active = ENTRY_BY_SLUG[activeSlug];
    // URL-jump guard: the active slug resolved to a real entry, but one
    // this user can't see. Keep the (filtered) rail visible and render
    // the access-denied page in the pane — never silently redirect.
    const denied = !!active && !this.canSee(active);

    // First slug the user CAN see — the access-denied "back" target.
    const firstVisible = visibleGroups[0]?.entries[0];
    return html`
      <style>
        rm-settings-shell {
          display: flex;
          flex-direction: column;
          height: 100%;
          min-height: 0;
          background: var(--rm-bg);
          color: var(--rm-ink);
          font-family: var(--rm-font-body);
        }
        rm-settings-shell .ss-layout {
          flex: 1;
          min-height: 0;
          display: grid;
          grid-template-columns: 248px 1fr;
        }
        rm-settings-shell .ss-nav {
          background: var(--rm-surface-2);
          border-right: 1px solid var(--rm-border);
          padding: 16px 12px;
          overflow-y: auto;
          display: flex;
          flex-direction: column;
        }
        rm-settings-shell .ss-nav .sttl {
          display: flex;
          align-items: center;
          justify-content: space-between;
          padding: 0 8px 12px;
        }
        rm-settings-shell .ss-nav .sttl b {
          font-size: var(--rm-text-md);
          font-weight: 600;
        }
        rm-settings-shell .ss-nav .ng {
          font-size: var(--rm-text-xs);
          font-weight: 600;
          color: var(--rm-ink-3);
          text-transform: uppercase;
          letter-spacing: 0.04em;
          padding: 14px 8px 5px;
        }
        rm-settings-shell .ss-nav .ni {
          display: flex;
          align-items: center;
          gap: 9px;
          padding: 7px 9px;
          border-radius: var(--rm-radius-sm);
          font-size: 13.5px;
          color: var(--rm-ink-2);
          cursor: pointer;
          background: none;
          border: none;
          width: 100%;
          text-align: left;
          font-family: inherit;
          transition: 0.12s;
        }
        rm-settings-shell .ss-nav .ni:hover { background: var(--rm-surface); }
        rm-settings-shell .ss-nav .ni.active {
          background: var(--rm-accent-subtle);
          color: var(--rm-accent-2);
          font-weight: 500;
        }
        /* Nav icon — sits a notch lighter than the label text at rest;
         * follows the row's color on hover/active because the SVG uses
         * stroke="currentColor". */
        rm-settings-shell .ss-nav .ni .ni-icon {
          display: inline-flex;
          color: var(--rm-ink-3);
          flex-shrink: 0;
        }
        rm-settings-shell .ss-nav .ni:hover .ni-icon,
        rm-settings-shell .ss-nav .ni.active .ni-icon {
          color: inherit;
        }
        rm-settings-shell .ss-close {
          width: 28px;
          height: 28px;
          border-radius: 7px;
          display: grid;
          place-items: center;
          color: var(--rm-ink-3);
          background: none;
          border: none;
          cursor: pointer;
          font-family: inherit;
        }
        rm-settings-shell .ss-close:hover {
          background: var(--rm-surface-3);
          color: var(--rm-ink);
        }
        rm-settings-shell .ss-main {
          display: flex;
          flex-direction: column;
          min-width: 0;
          overflow: hidden;
        }
        rm-settings-shell .ss-hd {
          height: 52px;
          display: flex;
          align-items: center;
          padding: 0 22px;
          border-bottom: 1px solid var(--rm-border);
          background: var(--rm-bg);
        }
        rm-settings-shell .ss-hd h2 {
          font-size: 16px;
          font-weight: 600;
          margin: 0;
        }
        rm-settings-shell .ss-body {
          flex: 1;
          overflow-y: auto;
          min-height: 0;
        }
        /* v2-A flagged "double card" — the inner card wrapped each
         * v1.1 page in a surface bordered box, but every v1.1 page
         * already paints its own surface + padding. v2-C drops the
         * wrapper to a transparent positioning container so the
         * inner page owns the look-and-feel. */
        rm-settings-shell .ss-card {
          background: none;
          border: none;
          border-radius: 0;
          padding: 0;
          min-height: 100%;
        }
      </style>
      <rm-reauth-banner></rm-reauth-banner>
      <div class="ss-layout">
      <aside class="ss-nav" aria-label="Settings navigation">
        <div class="sttl">
          <b>Settings</b>
          <button
            class="ss-close"
            data-testid="settings-back"
            aria-label="Back to chat"
            @click=${this.backToChat}
          >${iconClose(16)}</button>
        </div>
        ${visibleGroups.map((group) => this.renderGroup(group, activeSlug))}
      </aside>
      <div class="ss-main">
        <div class="ss-hd">
          <h2 data-testid="settings-active-title">
            ${denied ? 'Access denied' : active?.label ?? 'Settings'}
          </h2>
        </div>
        <div class="ss-body">
          <div class="ss-card" data-testid="settings-active-pane">
            ${denied
              ? html`<rm-access-denied
                  data-testid="access-denied"
                  .capability=${active!.requires ?? ''}
                  .pageLabel=${active!.label}
                  .backSlug=${firstVisible?.slug ?? DEFAULT_SLUG}
                  .backLabel=${firstVisible?.label ?? 'Coworkers'}
                ></rm-access-denied>`
              : active
                ? keyed(activeSlug, active.render())
                : nothing}
          </div>
        </div>
      </div>
      </div>
    `;
  }

  private renderGroup(group: NavGroup, activeSlug: string): TemplateResult {
    return html`
      ${group.heading
        ? html`<div class="ng">${group.heading}</div>`
        : nothing}
      ${group.entries.map(
        (entry) => html`
          <button
            class=${`ni ${entry.slug === activeSlug ? 'active' : ''}`}
            data-testid="settings-nav-entry"
            data-slug=${entry.slug}
            aria-current=${entry.slug === activeSlug ? 'page' : 'false'}
            @click=${() => this.navigate(entry.slug)}
          >
            <span class="ni-icon">${entry.icon()}</span>
            <span class="ni-label">${entry.label}</span>
          </button>
        `,
      )}
    `;
  }
}
