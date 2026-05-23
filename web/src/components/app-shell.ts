import { LitElement, html, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';

import { ROUTES, matchRoute, type RouteDef } from '../router.js';
import './router-outlet.js';
import './reauth-banner.js';

// Application-level chrome: a thin left nav listing all top-level
// destinations + the routed page body. Design §6.2.
//
// Dark mode is driven purely by Tailwind v4's default `dark:` variant
// (which is `prefers-color-scheme: dark`) — no JS toggle in Phase 1
// (design §15 #4). Components only need to be styled with `dark:`
// classes; the OS theme picks the variant automatically.
//
// Topbar: the spec mentions logo/title/user-menu. For Phase 0 we
// keep it intentionally light — a logo + the current page label
// when the page itself does not own its header bar. The chat page
// already paints its own header (connection status + sidebar
// collapse), so the shell suppresses its topbar there to avoid
// stacking two bars on the single most important page.
@customElement('rm-app-shell')
export class AppShell extends LitElement {
  @state() private currentHash: string = location.hash;

  protected override createRenderRoot() {
    return this;
  }

  override connectedCallback() {
    super.connectedCallback();
    this.style.display = 'block';
    this.style.height = '100%';
    window.addEventListener('hashchange', this.onHashChange);
  }

  override disconnectedCallback() {
    super.disconnectedCallback();
    window.removeEventListener('hashchange', this.onHashChange);
  }

  private onHashChange = (): void => {
    this.currentHash = location.hash;
  };

  private isActive(route: RouteDef): boolean {
    return matchRoute(this.currentHash).id === route.id;
  }

  private navigate(hash: string) {
    if (location.hash !== hash) {
      location.hash = hash;
    }
  }

  override render() {
    const active = matchRoute(this.currentHash);
    // Chat-panel has its own internal header; skipping the shell
    // topbar there keeps the densest page on one bar.
    const showTopbar = active.id !== 'chat';

    return html`
      <div class="flex flex-col h-full bg-surface-0 dark:bg-d-surface-0">
        <rm-reauth-banner></rm-reauth-banner>
        <div class="flex flex-1 min-h-0 overflow-hidden">
        <!-- App-level nav sidebar -->
        <nav
          class="w-52 shrink-0 h-full flex flex-col bg-surface-1 dark:bg-d-surface-1
            border-r border-surface-3 dark:border-d-surface-3 overflow-hidden"
          aria-label="Primary navigation"
        >
          <!-- Brand -->
          <div class="px-4 py-3 flex items-center gap-2 shrink-0">
            <div
              class="w-7 h-7 rounded-md bg-gradient-to-br from-brand-light to-brand
                flex items-center justify-center shadow-sm"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14"
                fill="none" stroke="white" stroke-width="2" stroke-linecap="round"
                stroke-linejoin="round" viewBox="0 0 24 24">
                <path d="M12 2L2 7l10 5 10-5-10-5z"/>
                <path d="M2 17l10 5 10-5"/>
                <path d="M2 12l10 5 10-5"/>
              </svg>
            </div>
            <span class="text-[14px] font-semibold text-ink-0 dark:text-d-ink-0">
              RoleMesh
            </span>
          </div>

          <!-- Nav items -->
          <ul class="flex-1 overflow-y-auto px-2 py-2 space-y-0.5">
            ${ROUTES.filter((r) => r.inSidebar).map((r) => {
              const active = this.isActive(r);
              return html`
                <li>
                  <button
                    type="button"
                    aria-current=${active ? 'page' : 'false'}
                    @click=${() => this.navigate(r.hash)}
                    class="w-full text-left px-3 py-2 rounded-lg text-[13px]
                      font-medium transition-colors cursor-pointer flex items-center justify-between
                      ${active
                        ? 'bg-brand/10 text-brand dark:text-brand-light'
                        : 'text-ink-1 dark:text-d-ink-1 hover:bg-surface-2 dark:hover:bg-d-surface-2'}"
                  >
                    <span class="truncate">${r.label}</span>
                    ${r.phase !== null
                      ? html`<span
                          class="ml-2 text-[10px] font-semibold uppercase tracking-wider
                            text-ink-3 dark:text-d-ink-3"
                          >P${r.phase}</span
                        >`
                      : nothing}
                  </button>
                </li>
              `;
            })}
          </ul>
        </nav>

        <!-- Main column: optional topbar + routed body -->
        <div class="flex-1 flex flex-col min-w-0">
          ${showTopbar
            ? html`
                <header
                  class="shrink-0 h-12 flex items-center px-4
                    border-b border-surface-3 dark:border-d-surface-3
                    bg-surface-0 dark:bg-d-surface-0"
                >
                  <span class="text-[14px] font-semibold text-ink-0 dark:text-d-ink-0">
                    ${active.label}
                  </span>
                </header>
              `
            : nothing}
          <main class="flex-1 min-h-0 flex flex-col overflow-hidden">
            <rm-router-outlet></rm-router-outlet>
          </main>
        </div>
        </div>
      </div>
    `;
  }
}
