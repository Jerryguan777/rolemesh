import { LitElement, html } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import './components/chat-panel.js';
import './components/message-list.js';
import './components/message-item.js';
import './components/message-editor.js';
import './components/sidebar.js';
import './components/login-page.js';
import './components/safety-rules-page.js';
import './components/safety-decisions-page.js';
import {
  fetchAuthConfig,
  getStoredToken,
  handleCallback,
  isTokenExpired,
  scheduleRefresh,
} from './services/oidc-auth.js';

type AuthState = 'loading' | 'login' | 'authenticated';
type Route = 'chat' | 'admin-safety-rules' | 'admin-safety-decisions';

// Map from location.hash to Route. Hash-based routing so the Vite dev
// server + static FastAPI serve work without a history-API fallback.
function routeFromHash(hash: string): Route {
  if (hash === '#/admin/safety/rules') return 'admin-safety-rules';
  if (hash === '#/admin/safety/decisions') return 'admin-safety-decisions';
  return 'chat';
}

@customElement('rm-app')
export class RmApp extends LitElement {
  protected override createRenderRoot() {
    return this;
  }

  @state() private authState: AuthState = 'loading';
  @state() private route: Route = routeFromHash(location.hash);

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

  private onHashChange = (): void => {
    this.route = routeFromHash(location.hash);
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

  private renderRouted() {
    switch (this.route) {
      case 'admin-safety-rules':
        return html`<rm-safety-rules-page></rm-safety-rules-page>`;
      case 'admin-safety-decisions':
        return html`<rm-safety-decisions-page></rm-safety-decisions-page>`;
      default:
        return html`<rm-chat-panel class="flex-1 min-h-0"></rm-chat-panel>`;
    }
  }

  override render() {
    if (this.authState === 'loading') {
      return html`<div style="display:flex;align-items:center;justify-content:center;height:100vh;color:#666;">Loading...</div>`;
    }
    if (this.authState === 'login') {
      return html`<rm-login-page></rm-login-page>`;
    }
    return html`
      <div class="h-full flex flex-col bg-surface-0 dark:bg-d-surface-0">
        <div class="flex-1 min-h-0 overflow-auto">${this.renderRouted()}</div>
      </div>
    `;
  }
}

// Mount
const app = document.getElementById('app');
if (app) {
  app.innerHTML = '<rm-app></rm-app>';
}
