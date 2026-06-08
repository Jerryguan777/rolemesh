// <rm-platform-shell> — the platform-plane chrome (RBAC UI spec §2 / §4).
//
// Rendered by <rm-app> when `currentMe()?.plane === 'platform'` (a PLANE
// check, never a role-name check). It lives at the same level as
// <rm-settings-shell> and is structurally parallel to it — one nav group
// "PLATFORM" with four hash-routed entries under `#/platform/<slug>`:
//
//   ─ PLATFORM
//     Tenants          → <rm-platform-tenants-page>  (the real page)
//     Models           → <rm-coming-soon>            (deferred, phase 3)
//     Credential pool  → <rm-coming-soon>            (deferred, phase 3)
//     Platform safety  → <rm-coming-soon>            (deferred, phase 3)
//
// Each entry carries a `requires` capability and the rail filters by it
// via `hasCapability` (same idiom as settings-shell), so a future
// platform role with only a SUBSET of the four platform capabilities
// degrades gracefully — no role->capability table lives here. The strings
// name a capability, never a role; the backend permissions.py is the
// single source of truth and the wire carries the resolved list.
//
// Light DOM + `--rm-` tokens only, matching the settings-shell neighbor.

import { LitElement, html, nothing, type TemplateResult } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { keyed } from 'lit/directives/keyed.js';
import type { SVGTemplateResult } from 'lit';

import { currentMe, hasCapability } from '../auth/capabilities.js';
import { iconChip, iconKey, iconShield, iconUsers } from './icons.js';
import './reauth-banner.js';
import './coming-soon.js';
import './platform-tenants-page.js';
import './access-denied-page.js';

interface NavEntry {
  /** Sub-path under `#/platform/`. */
  slug: string;
  label: string;
  icon: () => SVGTemplateResult;
  render: () => TemplateResult;
  /** Platform capability required to see this entry. Always set on this
   *  shell (every platform page is gated). A typo silently hides the
   *  entry for everyone — the strings must match permissions.py exactly. */
  requires: string;
}

/**
 * The four platform-plane pages (spec §4). Order = the rail order. Only
 * Tenants is a real page in v3; the other three route to <rm-coming-soon>
 * stubs (Models CRUD / Credential pool / Platform safety are deferred —
 * their backend write endpoints exist; only the UI is pending, spec §5.8).
 */
const NAV_ENTRIES: NavEntry[] = [
  {
    slug: 'tenants',
    label: 'Tenants',
    icon: () => iconUsers(16),
    render: () => html`<rm-platform-tenants-page></rm-platform-tenants-page>`,
    requires: 'platform.tenant.manage',
  },
  {
    slug: 'models',
    label: 'Models',
    icon: () => iconChip(16),
    render: () =>
      html`<rm-coming-soon label="Models management" .phase=${3}></rm-coming-soon>`,
    requires: 'model.manage',
  },
  {
    slug: 'credentials',
    label: 'Credential pool',
    icon: () => iconKey(16),
    render: () =>
      html`<rm-coming-soon label="Credential pool" .phase=${3}></rm-coming-soon>`,
    requires: 'credential.pool.manage',
  },
  {
    slug: 'safety',
    label: 'Platform safety rules',
    icon: () => iconShield(16),
    render: () =>
      html`<rm-coming-soon
        label="Platform safety rules"
        .phase=${3}
      ></rm-coming-soon>`,
    requires: 'safety.platform.manage',
  },
];

/** Flat lookup map. Computed once at module load. */
const ENTRY_BY_SLUG: Record<string, NavEntry> = Object.fromEntries(
  NAV_ENTRIES.map((e) => [e.slug, e]),
);

const DEFAULT_SLUG = 'tenants';

/**
 * Resolve `#/platform/<slug>[/<rest>]` to a slug. Sub-paths collapse to
 * the parent slug (mirrors settings-shell.slugFromHash). Unknown slugs
 * fall back to the default.
 */
export function slugFromHash(hash: string): string {
  const m = hash.match(/^#\/platform\/([^/?#]+)/);
  if (!m) return DEFAULT_SLUG;
  const slug = m[1];
  return slug in ENTRY_BY_SLUG ? slug : DEFAULT_SLUG;
}

@customElement('rm-platform-shell')
export class RmPlatformShell extends LitElement {
  @state() private hash: string = location.hash;

  protected override createRenderRoot() {
    // Light DOM — page components resolve `--rm-` tokens + Tailwind
    // classes at the document root (matches settings-shell).
    return this;
  }

  override connectedCallback() {
    super.connectedCallback();
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
    const target = `#/platform/${slug}`;
    if (location.hash !== target) {
      location.hash = target;
    }
  }

  /** Whether `currentMe()` may see `entry` — the single gate. The shell
   *  inspects only the wire `capabilities` list via `hasCapability`,
   *  never a role name. */
  private canSee(entry: NavEntry): boolean {
    return hasCapability(entry.requires);
  }

  override render(): TemplateResult {
    // Defensive null-guard. <rm-app> holds authState at 'loading' until
    // setMe() runs, so this should not be reached in practice.
    const me = currentMe();
    if (!me) {
      return html`<div class="ps-loading" data-testid="platform-loading">
        Loading…
      </div>`;
    }

    const visibleEntries = NAV_ENTRIES.filter((e) => this.canSee(e));

    const activeSlug = slugFromHash(this.hash);
    const active = ENTRY_BY_SLUG[activeSlug];
    // URL-jump guard: active slug is a real entry but one this user can't
    // see. Keep the rail, render access-denied in the pane (no redirect).
    const denied = !!active && !this.canSee(active);
    const firstVisible = visibleEntries[0];

    return html`
      <style>
        rm-platform-shell {
          display: flex;
          flex-direction: column;
          height: 100%;
          min-height: 0;
          background: var(--rm-bg);
          color: var(--rm-ink);
          font-family: var(--rm-font-body);
        }
        rm-platform-shell .ps-layout {
          flex: 1;
          min-height: 0;
          display: grid;
          grid-template-columns: 248px 1fr;
        }
        rm-platform-shell .ps-nav {
          background: var(--rm-surface-2);
          border-right: 1px solid var(--rm-border);
          padding: 16px 12px;
          overflow-y: auto;
          display: flex;
          flex-direction: column;
        }
        rm-platform-shell .ps-nav .sttl {
          display: flex;
          align-items: center;
          justify-content: space-between;
          padding: 0 8px 12px;
        }
        rm-platform-shell .ps-nav .sttl b {
          font-size: var(--rm-text-md);
          font-weight: 600;
        }
        rm-platform-shell .ps-nav .ng {
          font-size: var(--rm-text-xs);
          font-weight: 600;
          color: var(--rm-ink-3);
          text-transform: uppercase;
          letter-spacing: 0.04em;
          padding: 14px 8px 5px;
        }
        rm-platform-shell .ps-nav .ni {
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
        rm-platform-shell .ps-nav .ni:hover { background: var(--rm-surface); }
        rm-platform-shell .ps-nav .ni.active {
          background: var(--rm-accent-subtle);
          color: var(--rm-accent-2);
          font-weight: 500;
        }
        rm-platform-shell .ps-nav .ni .ni-icon {
          display: inline-flex;
          color: var(--rm-ink-3);
          flex-shrink: 0;
        }
        rm-platform-shell .ps-nav .ni:hover .ni-icon,
        rm-platform-shell .ps-nav .ni.active .ni-icon {
          color: inherit;
        }
        rm-platform-shell .ps-close {
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
        rm-platform-shell .ps-close:hover {
          background: var(--rm-surface-3);
          color: var(--rm-ink);
        }
        rm-platform-shell .ps-main {
          display: flex;
          flex-direction: column;
          min-width: 0;
          overflow: hidden;
        }
        rm-platform-shell .ps-hd {
          height: 52px;
          display: flex;
          align-items: center;
          padding: 0 22px;
          border-bottom: 1px solid var(--rm-border);
          background: var(--rm-bg);
        }
        rm-platform-shell .ps-hd h2 {
          font-size: 16px;
          font-weight: 600;
          margin: 0;
        }
        rm-platform-shell .ps-body {
          flex: 1;
          overflow-y: auto;
          min-height: 0;
        }
        rm-platform-shell .ps-card {
          background: none;
          border: none;
          border-radius: 0;
          padding: 0;
          min-height: 100%;
        }
      </style>
      <rm-reauth-banner></rm-reauth-banner>
      <div class="ps-layout">
        <aside class="ps-nav" aria-label="Platform navigation">
          <div class="sttl">
            <b>Platform</b>
          </div>
          <div class="ng">Platform</div>
          ${visibleEntries.map((entry) => this.renderEntry(entry, activeSlug))}
        </aside>
        <div class="ps-main">
          <div class="ps-hd">
            <h2 data-testid="platform-active-title">
              ${denied ? 'Access denied' : active?.label ?? 'Platform'}
            </h2>
          </div>
          <div class="ps-body">
            <div class="ps-card" data-testid="platform-active-pane">
              ${denied
                ? html`<rm-access-denied
                    data-testid="access-denied"
                    .capability=${active!.requires}
                    .pageLabel=${active!.label}
                    .backSlug=${firstVisible?.slug ?? DEFAULT_SLUG}
                    .backLabel=${firstVisible?.label ?? 'Tenants'}
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

  private renderEntry(entry: NavEntry, activeSlug: string): TemplateResult {
    return html`
      <button
        class=${`ni ${entry.slug === activeSlug ? 'active' : ''}`}
        data-testid="platform-nav-entry"
        data-slug=${entry.slug}
        aria-current=${entry.slug === activeSlug ? 'page' : 'false'}
        @click=${() => this.navigate(entry.slug)}
      >
        <span class="ni-icon">${entry.icon()}</span>
        <span class="ni-label">${entry.label}</span>
      </button>
    `;
  }
}
