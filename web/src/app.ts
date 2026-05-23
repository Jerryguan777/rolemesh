import { LitElement, html } from 'lit';
import { customElement, state } from 'lit/decorators.js';

// v2 design tokens. Loaded once at app entry so the cream/terracotta
// palette is available to every component on first paint; CSS custom
// properties cascade through shadow DOM via inheritance, so this
// single import covers both light-DOM components (chat-panel) and
// shadow-DOM v2 primitives (rm-dialog, rm-wizard, …).
import './styles/tokens.css';

import './components/chat-panel.js';
import './components/message-list.js';
import './components/message-item.js';
import './components/message-editor.js';
import './components/sidebar.js';
import './components/reauth-banner.js';
import './components/login-page.js';
import './components/coming-soon.js';
import './components/inline-approval.js';
import './components/chat-shell.js';
import './components/settings-shell.js';
import './components/activity-shell.js';
import { installLegacyRedirects, topLevelShell } from './router.js';
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

  private async resolveAuth() {
    const params = new URLSearchParams(location.search);

    // 1. Token in URL query params (backward compat / SaaS-passed)
    const urlToken = params.get('token');
    if (urlToken && !isTokenExpired(urlToken)) {
      this.authState = 'authenticated';
      this.startRefreshScheduler(urlToken);
      return;
    }

    // 2. OIDC callback: code stored by /oauth2/callback page
    if (sessionStorage.getItem('oidc_code')) {
      const exchanged = await handleCallback();
      if (exchanged) {
        this.authState = 'authenticated';
        this.startRefreshScheduler(exchanged.id_token);
        return;
      }
    }

    // 3. Stored token from previous session
    const stored = getStoredToken();
    if (stored && !isTokenExpired(stored)) {
      this.authState = 'authenticated';
      this.startRefreshScheduler(stored);
      return;
    }

    // 4. OIDC configured but no token → show login page
    const config = await fetchAuthConfig();
    if (config) {
      this.authState = 'login';
      return;
    }

    // 5. Fall back to chat panel (legacy / no auth provider configured)
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
