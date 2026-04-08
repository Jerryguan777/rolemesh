import { LitElement, html, css } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { fetchAuthConfig, startLogin, type AuthConfig } from '../services/oidc-auth.js';

@customElement('rm-login-page')
export class LoginPage extends LitElement {
  static override styles = css`
    :host {
      display: flex;
      align-items: center;
      justify-content: center;
      height: 100vh;
      width: 100vw;
      background: #f5f5f7;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    }
    .card {
      background: white;
      padding: 48px 56px;
      border-radius: 12px;
      box-shadow: 0 4px 24px rgba(0, 0, 0, 0.08);
      text-align: center;
      min-width: 320px;
    }
    h1 {
      margin: 0 0 8px;
      font-size: 24px;
      font-weight: 600;
    }
    p {
      margin: 0 0 32px;
      color: #666;
      font-size: 14px;
    }
    button {
      background: #0066ff;
      color: white;
      border: none;
      padding: 12px 32px;
      font-size: 15px;
      font-weight: 500;
      border-radius: 8px;
      cursor: pointer;
      transition: background 0.15s;
    }
    button:hover {
      background: #0052cc;
    }
    button:disabled {
      background: #ccc;
      cursor: not-allowed;
    }
    .error {
      color: #d33;
      margin-top: 16px;
      font-size: 13px;
    }
  `;

  @state() private config: AuthConfig | null = null;
  @state() private loading = true;
  @state() private error: string | null = null;

  override async connectedCallback() {
    super.connectedCallback();
    this.config = await fetchAuthConfig();
    if (!this.config) {
      this.error = 'Failed to load authentication configuration';
    }
    this.loading = false;
  }

  private async handleLogin() {
    if (!this.config) return;
    try {
      await startLogin(this.config);
    } catch (e) {
      this.error = `Login failed: ${e}`;
    }
  }

  override render() {
    return html`
      <div class="card">
        <h1>RoleMesh</h1>
        <p>Sign in to continue</p>
        <button @click=${this.handleLogin} ?disabled=${this.loading || !this.config}>
          ${this.loading ? 'Loading...' : 'Sign in with SSO'}
        </button>
        ${this.error ? html`<div class="error">${this.error}</div>` : ''}
      </div>
    `;
  }
}
