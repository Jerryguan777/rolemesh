import { LitElement, html } from 'lit';
import { customElement, state } from 'lit/decorators.js';

// v2 design tokens. Loaded once at app entry so the cream/terracotta
// palette is available to every component on first paint; CSS custom
// properties cascade through shadow DOM via inheritance, so this
// single import covers both light-DOM components (chat-panel) and
// shadow-DOM v2 primitives (rm-dialog, rm-wizard, …).
import './styles/tokens.css';
// Shared visual language for the settings pages (Coworkers / MCP /
// Skills / Models / Credentials). Lives next to tokens.css so all
// v2 stylesheets load before any component mounts.
import './styles/settings-pages.css';
// Card-scoped warm-palette override for the HITL approval card (re-points the
// @theme tokens to --rm-* inside rm-approval-card only).
import './styles/approval-card.css';

import './components/chat-panel.js';
import './components/message-list.js';
import './components/message-item.js';
import './components/message-editor.js';
import './components/sidebar.js';
import './components/reauth-banner.js';
import './components/login-page.js';
import './components/coming-soon.js';
import './components/chat-shell.js';
import './components/settings-shell.js';
import './components/activity-shell.js';
import { installLegacyRedirects, topLevelShell } from './router.js';
import { getApiClient } from './api/client.js';
import { setMe } from './auth/capabilities.js';
import {
  fetchAuthConfig,
  getStoredToken,
  handleCallback,
  isTokenExpired,
  scheduleRefresh,
} from './services/oidc-auth.js';

// Rewrite any v1.1 flat hash (`#/coworkers`, …) to its v2 nested
// home (`#/manage/coworkers`, …) before any shell mounts. The
// handler stays installed for the lifetime of the SPA so bookmarks
// opened mid-session also redirect.
installLegacyRedirects();

type AuthState = 'loading' | 'login' | 'authenticated';

// `<rm-app>` is the auth state machine + outermost host. Once
// authenticated, it picks one of three v2 shells based on the top
// level of the current hash:
//   `#/`           → `<rm-chat-shell>`
//   `#/manage/*`   → `<rm-settings-shell>`
//   `#/activity/*` → `<rm-activity-shell>` (mostly placeholder)
// A hashchange listener swaps shells without a full reload.
@customElement('rm-app')
export class RmApp extends LitElement {
  protected override createRenderRoot() {
    return this;
  }

  @state() private authState: AuthState = 'loading';
  @state() private shell: 'chat' | 'manage' | 'activity' = topLevelShell(
    location.hash,
  );

  override async connectedCallback() {
    super.connectedCallback();
    this.style.display = 'block';
    this.style.height = '100%';
    // Listen for unrecoverable auth failures (refresh exhausted, etc.)
    window.addEventListener('rm-auth-failed', () => {
      this.authState = 'login';
    });
    window.addEventListener('hashchange', this.onHashChange);
    await this.resolveAuth();
  }

  override disconnectedCallback() {
    super.disconnectedCallback();
    window.removeEventListener('hashchange', this.onHashChange);
  }

  private onHashChange = () => {
    this.shell = topLevelShell(location.hash);
  };

  // Outcome of phase 1 of resolveAuth. No `@state` is touched while a
  // TokenOutcome is being computed — the state machine stays 'loading'
  // until resolveAuth itself commits a transition. Three shapes:
  //   - 'token'  → a real bearer; do getMe → setMe before authenticating.
  //   - 'legacy' → no auth provider configured; authenticate WITHOUT a
  //                token, WITHOUT getMe, WITHOUT a refresh scheduler (D2 —
  //                chat-only deployments must not regress into a getMe 401).
  //   - 'login'  → cannot authenticate; render the login page.
  private async resolveToken(): Promise<
    | { kind: 'token'; token: string }
    | { kind: 'legacy' }
    | { kind: 'login' }
  > {
    const params = new URLSearchParams(location.search);

    // 1. Token in URL query params (backward compat / SaaS-passed)
    const urlToken = params.get('token');
    if (urlToken && !isTokenExpired(urlToken)) {
      return { kind: 'token', token: urlToken };
    }

    // 2. OIDC callback: code stored by /oauth2/callback page
    if (sessionStorage.getItem('oidc_code')) {
      const exchanged = await handleCallback();
      if (exchanged) {
        return { kind: 'token', token: exchanged.id_token };
      }
    }

    // 3. Stored token from previous session
    const stored = getStoredToken();
    if (stored && !isTokenExpired(stored)) {
      return { kind: 'token', token: stored };
    }

    // 4. OIDC configured but no token → show login page
    const config = await fetchAuthConfig();
    if (config) {
      return { kind: 'login' };
    }

    // 5. Legacy / no auth provider configured → chat-only deployment.
    return { kind: 'legacy' };
  }

  private async resolveAuth() {
    // Phase 1: resolve a token outcome WITHOUT touching @state. authState
    // stays 'loading' (render shows "Loading...") so no sub-shell mounts.
    const outcome = await this.resolveToken();

    if (outcome.kind === 'login') {
      this.authState = 'login';
      return;
    }

    // D2: the legacy / no-auth branch authenticates a chat-only deployment
    // WITHOUT a token. It must not call getMe (would 401 → dead-end login)
    // and must not schedule a refresh (no token to refresh). It flips
    // straight to authenticated via the single sink at the end of phase 3.
    if (outcome.kind === 'token') {
      this.startRefreshScheduler(outcome.token);

      // Phase 2: with authState still 'loading', populate the Me cache
      // before any shell mounts. setMe() writes a plain module variable —
      // invisible to Lit reactivity — so the state flip below MUST come
      // after this resolves (spec §7.2 atomic bootstrap).
      try {
        const me = await getApiClient().getMe();
        setMe(me);
      } catch (err) {
        console.error('failed to load /me', err);
        this.authState = 'login'; // fail closed; leave the cache unset
        return;
      }
    }

    // Phase 3: the ONLY authenticated transition. By now either the Me
    // cache is populated (token branch) or this is the legacy branch that
    // intentionally has no Me. The next render picks the right shell.
    this.authState = 'authenticated';
  }

  private startRefreshScheduler(token: string): void {
    scheduleRefresh(token, (newToken) => {
      window.dispatchEvent(
        new CustomEvent('rm-token-refreshed', { detail: newToken }),
      );
    });
  }

  override render() {
    if (this.authState === 'loading') {
      return html`<div
        class="h-full flex items-center justify-center text-ink-2 dark:text-d-ink-2"
      >Loading...</div>`;
    }
    if (this.authState === 'login') {
      return html`<rm-login-page></rm-login-page>`;
    }
    switch (this.shell) {
      case 'manage':
        return html`<rm-settings-shell></rm-settings-shell>`;
      case 'activity':
        return html`<rm-activity-shell></rm-activity-shell>`;
      case 'chat':
      default:
        return html`<rm-chat-shell></rm-chat-shell>`;
    }
  }
}

// Mount
const app = document.getElementById('app');
if (app) {
  app.innerHTML = '<rm-app></rm-app>';
}
