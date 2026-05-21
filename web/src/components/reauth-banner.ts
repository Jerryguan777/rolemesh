// `<rm-reauth-banner>` — design §6.3 J.
//
// Listens for `rm-reauth-required` events (dispatched by the v1 WS
// client when an `event.run.requires_reauth` frame arrives) and
// renders a top-of-shell banner with a "Re-login" affordance.
//
// In bootstrap-token / Phase 1 builds the backend never actually
// emits the upstream event (the user-mode MCP flow that produces it
// is gated on the OIDC branch — design §6.3 J + 01b Open Question 2).
// The banner code ships now so the SPA is ready when the engine
// path turns on; dev / debugging access is via the in-page console:
//
//   window.__forceReauth({ reason: 'refresh_token_expired' })
//
// which surfaces the same UI without needing a real backend
// trigger. The dev hook is gated behind a `window` cast so it's
// not surfaced in TypeScript autocomplete elsewhere.

import { LitElement, html, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';

import { clearToken, fetchAuthConfig, startLogin } from '../services/oidc-auth.js';

export interface ReauthDetail {
  reason?: string;
  /** Optional run_id for telemetry / debugging. */
  runId?: string;
}

@customElement('rm-reauth-banner')
export class ReauthBanner extends LitElement {
  @state() private visible = false;
  @state() private reason: string | null = null;
  @state() private busy = false;

  protected override createRenderRoot() {
    return this;
  }

  override connectedCallback(): void {
    super.connectedCallback();
    window.addEventListener('rm-reauth-required', this.onRequired);
    // Dev hook — accessible from the browser console for QA without
    // a real backend trigger. Documented in the module docstring.
    (window as unknown as { __forceReauth?: (d?: ReauthDetail) => void }).__forceReauth = (
      detail?: ReauthDetail,
    ) => {
      window.dispatchEvent(
        new CustomEvent<ReauthDetail>('rm-reauth-required', {
          detail: detail ?? {},
        }),
      );
    };
  }

  override disconnectedCallback(): void {
    super.disconnectedCallback();
    window.removeEventListener('rm-reauth-required', this.onRequired);
    delete (window as unknown as { __forceReauth?: unknown }).__forceReauth;
  }

  private onRequired = (evt: Event): void => {
    const detail = (evt as CustomEvent<ReauthDetail>).detail ?? {};
    this.reason = detail.reason ?? null;
    this.visible = true;
  };

  private async handleRelogin(): Promise<void> {
    this.busy = true;
    try {
      const cfg = await fetchAuthConfig();
      if (cfg) {
        await startLogin(cfg);
        return;
      }
      // No OIDC provider configured — fall back to wiping the token
      // and reloading; the app's auth state machine surfaces the
      // bootstrap-token entry again (design §6.3 J fallback).
      clearToken();
      location.reload();
    } finally {
      this.busy = false;
    }
  }

  private handleDismiss(): void {
    this.visible = false;
  }

  override render() {
    if (!this.visible) return nothing;
    const detail = this.reason ? ` (${this.reason})` : '';
    return html`
      <div
        role="alert"
        aria-live="polite"
        class="shrink-0 px-4 py-2 bg-amber-50 dark:bg-amber-900/30
          border-b border-amber-200 dark:border-amber-800
          text-amber-900 dark:text-amber-200 text-[13px] flex items-center gap-3"
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"
          fill="none" stroke="currentColor" stroke-width="2"
          stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"
          class="shrink-0"
        >
          <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
          <line x1="12" y1="9" x2="12" y2="13"/>
          <line x1="12" y1="17" x2="12.01" y2="17"/>
        </svg>
        <span class="flex-1 min-w-0">
          Re-authentication required${detail}. Please re-login to continue.
        </span>
        <button
          type="button"
          class="px-3 py-1 rounded-md bg-amber-600 hover:bg-amber-700
            text-white text-[12px] font-medium cursor-pointer disabled:opacity-60"
          ?disabled=${this.busy}
          @click=${() => this.handleRelogin()}
        >
          ${this.busy ? 'Redirecting…' : 'Re-login'}
        </button>
        <button
          type="button"
          class="text-amber-700 dark:text-amber-300 text-[11px] underline cursor-pointer"
          @click=${() => this.handleDismiss()}
          title="Dismiss banner (the underlying issue is not resolved)"
        >
          Dismiss
        </button>
      </div>
    `;
  }
}
