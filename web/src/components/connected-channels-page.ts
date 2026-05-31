// Connected channels — v6.1 §P1.4 WebUI surface.
//
// The user's settings page for linking IM accounts to their RoleMesh
// identity. The DB and gateway sides of the flow are covered
// elsewhere; this component owns:
//
//   1. Listing the user's current Telegram identities (with one-row
//      Disconnect buttons), so the user can see which accounts are
//      bound and remove individual ones.
//   2. The "Connect Telegram" interactive flow:
//        - POST mints a token (and, when the tenant's bot @username
//          is on file, a deep-link).
//        - The waiting UI renders a deep-link button (preferred) plus
//          a copy-able short code (fallback) plus a countdown.
//        - A poll loop hits the listing endpoint every 3 s; when a
//          new identity appears the UI swaps to confirmation.
//
// Decisions baked in:
//   - The page deliberately does NOT distinguish "new" from "any" link
//     during the poll: we compare against the identity-id set captured
//     when the user clicked Connect, so even a tenant peer linking a
//     different account does not falsely flip our UI.
//   - 409 RESOURCE_NOT_AVAILABLE from POST surfaces as a static
//     "Configure a Telegram bot first" panel rather than a transient
//     error, since the underlying state is configuration not failure.

import { LitElement, html, nothing, type TemplateResult } from 'lit';
import { customElement, state } from 'lit/decorators.js';

import {
  ApiError,
  getApiClient,
  type ChannelLinkIdentity,
  type ChannelLinkToken,
} from '../api/client.js';
import { iconLink, iconClose } from './icons.js';

const POLL_INTERVAL_MS = 3000;
const TICK_INTERVAL_MS = 1000;

@customElement('rm-connected-channels-page')
export class ConnectedChannelsPage extends LitElement {
  @state() private identities: ChannelLinkIdentity[] = [];
  @state() private loading = true;
  @state() private listError: string | null = null;
  /** Active link attempt. `null` when idle (showing the connected list
   *  + a Connect button). Set after a successful POST and cleared
   *  either on cancel or when poll detects the new identity. */
  @state() private pending: ChannelLinkToken | null = null;
  @state() private pendingError: string | null = null;
  /** Seconds left on the active token. Derived from `pending.expires_at`
   *  and recomputed on each tick so the user sees a live countdown. */
  @state() private secondsLeft = 0;
  /** RESOURCE_NOT_AVAILABLE — separates "the tenant has not
   *  configured a Telegram bot" from a transient error. */
  @state() private noBotConfigured = false;
  /** Identity ids that existed when the user clicked Connect; the poll
   *  loop fires the success transition only when a NEW id appears.
   *  Without this, an existing link from a previous session would
   *  spuriously trigger the success UI. */
  private knownIdentityIds = new Set<string>();
  private pollHandle: ReturnType<typeof setInterval> | null = null;
  private tickHandle: ReturnType<typeof setInterval> | null = null;
  private readonly api = getApiClient();

  protected override createRenderRoot(): this {
    return this;
  }

  override connectedCallback(): void {
    super.connectedCallback();
    void this.refresh();
  }

  override disconnectedCallback(): void {
    super.disconnectedCallback();
    this.stopTimers();
  }

  private stopTimers(): void {
    if (this.pollHandle) {
      clearInterval(this.pollHandle);
      this.pollHandle = null;
    }
    if (this.tickHandle) {
      clearInterval(this.tickHandle);
      this.tickHandle = null;
    }
  }

  private async refresh(): Promise<void> {
    this.loading = true;
    this.listError = null;
    try {
      this.identities = await this.api.listTelegramLinks();
    } catch (err) {
      this.listError =
        err instanceof Error ? err.message : 'Failed to load links.';
      this.identities = [];
    } finally {
      this.loading = false;
    }
  }

  private async connect(): Promise<void> {
    this.pendingError = null;
    this.noBotConfigured = false;
    // Capture the pre-link identity set so poll only fires success on
    // a *new* row appearing — defends against a confusing UI flip if
    // the user already has an existing link (decision #13 allows multi-
    // bind, so an existing link is not the same as "done").
    this.knownIdentityIds = new Set(this.identities.map((i) => i.id));
    try {
      this.pending = await this.api.issueTelegramLinkToken();
    } catch (err) {
      if (
        err instanceof ApiError &&
        err.status === 409 &&
        err.body?.code === 'RESOURCE_NOT_AVAILABLE'
      ) {
        this.noBotConfigured = true;
        return;
      }
      this.pendingError =
        err instanceof Error ? err.message : 'Failed to start linking.';
      return;
    }
    this.recomputeCountdown();
    this.startPolling();
  }

  private cancelPending(): void {
    this.pending = null;
    this.pendingError = null;
    this.stopTimers();
  }

  private recomputeCountdown(): void {
    if (!this.pending) {
      this.secondsLeft = 0;
      return;
    }
    const expiresMs = Date.parse(this.pending.expires_at);
    const left = Math.max(0, Math.round((expiresMs - Date.now()) / 1000));
    this.secondsLeft = left;
    if (left === 0) {
      // Token expired before the user completed /start. Drop back to
      // the idle list so they can click Connect again; surface a
      // gentle hint via pendingError.
      this.pending = null;
      this.pendingError = 'Link token expired. Please try again.';
      this.stopTimers();
    }
  }

  private startPolling(): void {
    this.stopTimers();
    this.tickHandle = setInterval(() => {
      this.recomputeCountdown();
    }, TICK_INTERVAL_MS);
    this.pollHandle = setInterval(() => {
      void this.poll();
    }, POLL_INTERVAL_MS);
  }

  private async poll(): Promise<void> {
    if (!this.pending) return;
    try {
      const current = await this.api.listTelegramLinks();
      this.identities = current;
      const newOne = current.find((i) => !this.knownIdentityIds.has(i.id));
      if (newOne) {
        this.pending = null;
        this.pendingError = null;
        this.stopTimers();
      }
    } catch {
      // Swallow — the next poll round will try again. A transient
      // 5xx during the wait window shouldn't break the flow.
    }
  }

  private async disconnect(identityId: string): Promise<void> {
    try {
      await this.api.unlinkChannelIdentity(identityId);
      await this.refresh();
    } catch (err) {
      this.listError =
        err instanceof Error ? err.message : 'Failed to disconnect.';
    }
  }

  private async copyToken(): Promise<void> {
    if (!this.pending) return;
    try {
      await navigator.clipboard.writeText(this.pending.token);
    } catch {
      // Clipboard may be denied (insecure context, permission denied).
      // The token is already visible on screen as fallback.
    }
  }

  override render(): TemplateResult {
    return html`
      <style>
        rm-connected-channels-page {
          display: block;
          padding: 24px 22px;
          color: var(--rm-ink);
        }
        rm-connected-channels-page .cc-section {
          background: var(--rm-surface);
          border: 1px solid var(--rm-border);
          border-radius: var(--rm-radius);
          padding: 20px 22px;
          margin-bottom: 16px;
        }
        rm-connected-channels-page h3 {
          font-size: 14px;
          font-weight: 600;
          margin: 0 0 6px;
          display: flex;
          align-items: center;
          gap: 8px;
        }
        rm-connected-channels-page .cc-sub {
          font-size: var(--rm-text-xs);
          color: var(--rm-ink-3);
          margin: 0 0 16px;
          line-height: 1.5;
        }
        rm-connected-channels-page .cc-row {
          display: flex;
          align-items: center;
          justify-content: space-between;
          padding: 10px 0;
          border-top: 1px solid var(--rm-border);
        }
        rm-connected-channels-page .cc-row:first-of-type {
          border-top: none;
        }
        rm-connected-channels-page .cc-row .cc-label {
          font-size: 13.5px;
          font-weight: 500;
        }
        rm-connected-channels-page .cc-row .cc-meta {
          font-size: var(--rm-text-xs);
          color: var(--rm-ink-3);
        }
        rm-connected-channels-page button {
          font-family: inherit;
          font-size: 13px;
          padding: 7px 12px;
          border-radius: var(--rm-radius-sm);
          border: 1px solid var(--rm-border);
          background: var(--rm-surface-2);
          color: var(--rm-ink-2);
          cursor: pointer;
          transition: 0.12s;
        }
        rm-connected-channels-page button:hover {
          background: var(--rm-surface-3);
        }
        rm-connected-channels-page button.primary {
          background: var(--rm-accent);
          color: var(--rm-on-accent);
          border-color: var(--rm-accent);
        }
        rm-connected-channels-page button.primary:hover {
          opacity: 0.9;
        }
        rm-connected-channels-page button.danger {
          color: var(--rm-danger);
        }
        rm-connected-channels-page .cc-pending {
          margin-top: 10px;
          padding: 16px;
          border-radius: var(--rm-radius-sm);
          background: var(--rm-accent-subtle);
          border: 1px solid var(--rm-accent);
        }
        rm-connected-channels-page .cc-pending .cc-actions {
          display: flex;
          gap: 8px;
          margin-top: 10px;
        }
        rm-connected-channels-page .cc-code {
          font-family: ui-monospace, SFMono-Regular, monospace;
          font-size: 13px;
          padding: 8px 10px;
          background: var(--rm-surface);
          border-radius: var(--rm-radius-sm);
          border: 1px solid var(--rm-border);
          display: inline-block;
          margin-top: 8px;
          user-select: all;
        }
        rm-connected-channels-page .cc-countdown {
          font-size: var(--rm-text-xs);
          color: var(--rm-ink-3);
          margin-top: 6px;
        }
        rm-connected-channels-page .cc-error {
          color: var(--rm-danger);
          font-size: var(--rm-text-xs);
          margin-top: 8px;
        }
      </style>
      <section class="cc-section" data-testid="connected-channels-section">
        <h3>${iconLink(16)} Connected channels</h3>
        <p class="cc-sub">
          Link an IM account so a coworker bot can recognise you in
          1:1 chats. Unlinked accounts cannot start conversations
          with a bot.
        </p>
        ${this.renderTelegramBlock()}
      </section>
    `;
  }

  private renderTelegramBlock(): TemplateResult {
    if (this.loading) {
      return html`<p class="cc-sub" data-testid="cc-loading">Loading…</p>`;
    }
    if (this.listError) {
      return html`
        <p class="cc-error" data-testid="cc-list-error">${this.listError}</p>
        <button @click=${() => void this.refresh()}>Retry</button>
      `;
    }
    return html`
      ${this.identities.length === 0
        ? html`<p
            class="cc-sub"
            data-testid="cc-empty"
          >No Telegram accounts linked yet.</p>`
        : html`
            ${this.identities.map(
              (i) => html`
                <div class="cc-row" data-testid="cc-identity-row">
                  <div>
                    <div class="cc-label">
                      Telegram &middot;
                      <span data-testid="cc-channel-id">${i.channel_id}</span>
                    </div>
                    <div class="cc-meta">
                      ${i.created_at
                        ? `Linked ${i.created_at}`
                        : 'Linked'}
                    </div>
                  </div>
                  <button
                    class="danger"
                    data-testid="cc-disconnect"
                    @click=${() => void this.disconnect(i.id)}
                  >Disconnect</button>
                </div>
              `,
            )}
          `}
      ${this.pending
        ? this.renderPendingPanel()
        : html`
            <button
              class="primary"
              style="margin-top: 12px"
              data-testid="cc-connect-telegram"
              @click=${() => void this.connect()}
            >Connect Telegram</button>
            ${this.noBotConfigured
              ? html`<p
                  class="cc-error"
                  data-testid="cc-no-bot"
                >No Telegram bot is configured for this tenant. Add one
                under Coworkers → Bindings first.</p>`
              : nothing}
            ${this.pendingError
              ? html`<p
                  class="cc-error"
                  data-testid="cc-pending-error"
                >${this.pendingError}</p>`
              : nothing}
          `}
    `;
  }

  private renderPendingPanel(): TemplateResult {
    if (!this.pending) return html``;
    const deep = this.pending.deep_link;
    return html`
      <div class="cc-pending" data-testid="cc-pending">
        <div class="cc-label">Waiting for Telegram…</div>
        <div class="cc-sub">
          ${deep
            ? 'Open Telegram and send the prefilled /start command.'
            : 'Open your Telegram bot and send /start with the code below.'}
        </div>
        ${deep
          ? html`<a
              class="primary"
              style="display:inline-block;padding:7px 12px;border-radius:var(--rm-radius-sm);text-decoration:none"
              href=${deep}
              target="_blank"
              rel="noopener"
              data-testid="cc-deep-link"
            >Open Telegram</a>`
          : nothing}
        <div>
          <code class="cc-code" data-testid="cc-token">${this.pending.token}</code>
        </div>
        <div class="cc-actions">
          <button
            data-testid="cc-copy"
            @click=${() => void this.copyToken()}
          >Copy code</button>
          <button
            data-testid="cc-cancel"
            @click=${() => this.cancelPending()}
          >Cancel ${iconClose(14)}</button>
        </div>
        <p class="cc-countdown" data-testid="cc-countdown">
          Token expires in ${this.secondsLeft}s.
        </p>
      </div>
    `;
  }
}
