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
import './components/safety-rules-page.js';
import './components/safety-decisions-page.js';
import './components/coming-soon.js';
import './components/coworkers-page.js';
import './components/credentials-page.js';
import './components/mcp-servers-page.js';
import './components/models-page.js';
import './components/approvals-page.js';
import './components/skills-page.js';
import './components/skill-detail-page.js';
import './components/coworker-skills-tab.js';
import './components/inline-approval.js';
import './components/app-shell.js';
import './components/chat-shell.js';
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
// authenticated, it hands the entire page over to `<rm-app-shell>`
// which owns the application chrome (sidebar / topbar / outlet).
// The shell's outlet reads `location.hash` to decide which page
// component to render — see `web/src/router.ts`.
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
    // Re-resolve which shell owns the URL whenever the hash changes,
    // so navigations between `#/`, `#/manage/...`, `#/activity/...`
    // swap shells without a full reload.
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
        // Token now in sessionStorage; chat-panel reads it from there
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
      // Notify chat-panel to update its agent client and reconnect WebSocket
      window.dispatchEvent(
        new CustomEvent('rm-token-refreshed', { detail: newToken })
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
    // v2 split: `#/` → new chat shell, everything else → legacy
    // app-shell during the PR 3→PR 4 transition. PR 4 will replace
    // the `manage` branch with `<rm-settings-shell>` and add a real
    // activity placeholder.
    if (this.shell === 'chat') {
      return html`<rm-chat-shell></rm-chat-shell>`;
    }
    return html`<rm-app-shell></rm-app-shell>`;
  }
}

// Mount
const app = document.getElementById('app');
if (app) {
  app.innerHTML = '<rm-app></rm-app>';
}
