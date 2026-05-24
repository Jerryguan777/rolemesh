// Tenant LLM credentials page (#/credentials).
//
// Design §6.3 F + §8.1 envelope encryption: the SPA NEVER sees the
// existing key (no re-display, no last4, no "key length: 32" hint).
// The form is PUT-only — input is cleared the instant a write
// completes so a curious shoulder-surfer can't recover the value
// from form state.
//
// 409 RESOURCE_IN_USE is surfaced with the offending coworker count
// so the operator can fix the binding before retrying DELETE.

import { LitElement, html, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';

import { ApiError, getApiClient } from '../api/client.js';
import type { CredentialResponse, ModelProvider } from '../api/client.js';
import './credential-dialog.js';
import { iconPencil, iconTrash } from './icons.js';

// Mirrors the OpenAPI ModelProvider enum.
const PROVIDERS: readonly ModelProvider[] = [
  'anthropic',
  'openai',
  'google',
  'bedrock',
] as const;

@customElement('rm-credentials-page')
export class CredentialsPage extends LitElement {
  @state() private rows: CredentialResponse[] = [];
  @state() private loading = true;
  @state() private listError: string | null = null;
  // Per-provider transient UI state.
  @state() private putErrors: Record<string, string> = {};
  @state() private inFlight: Record<string, boolean> = {};
  /** Per-provider delete error keyed for the rm-card row. Cleared
   *  on the next refresh; UI surfaces it under the affected card. */
  @state() private deleteError: Record<string, string> = {};
  /** Drives <rm-credential-dialog>. `dialogProvider` may be null
   *  meaning "let the user pick" (Add new flow); set to a specific
   *  provider when the user clicks Edit / Add on an existing row. */
  @state() private dialogOpen = false;
  @state() private dialogProvider: ModelProvider | null = null;
  private readonly api = getApiClient();

  protected override createRenderRoot() {
    return this;
  }

  override connectedCallback() {
    super.connectedCallback();
    void this.refresh();
  }

  private async refresh(): Promise<void> {
    this.loading = true;
    this.listError = null;
    try {
      this.rows = await this.api.listCredentials();
    } catch (err) {
      this.rows = [];
      this.listError =
        err instanceof ApiError
          ? `${err.status} — ${err.message}`
          : (err as Error).message ?? 'unknown error';
    } finally {
      this.loading = false;
    }
  }

  private rowFor(provider: ModelProvider): CredentialResponse | null {
    return this.rows.find((r) => r.provider === provider) ?? null;
  }

  /** Opens the credential dialog for a specific provider. Used by
   *  both "Edit" (existing) and "Connect" (missing) row clicks. */
  private openDialog(provider: ModelProvider): void {
    this.dialogProvider = provider;
    this.dialogOpen = true;
  }

  // Renamed from `remove` to avoid overriding HTMLElement.prototype.remove;
  // see mcp-servers-page.ts for the matching diagnostic — Lit's NodePart
  // teardown calls element.remove() with zero args and would throw here.
  private async removeCredential(provider: ModelProvider): Promise<void> {
    const ok = window.confirm(
      `Delete the ${provider} credential?\n\n` +
        'Models from this provider will stop running for every coworker ' +
        'until a new credential is set. Cannot be undone.',
    );
    if (!ok) return;
    this.inFlight = { ...this.inFlight, [provider]: true };
    this.deleteError = { ...this.deleteError, [provider]: '' };
    try {
      await this.api.deleteCredential(provider);
      await this.refresh();
    } catch (err) {
      this.deleteError = {
        ...this.deleteError,
        [provider]: this.errMessage(err),
      };
    } finally {
      this.inFlight = { ...this.inFlight, [provider]: false };
    }
  }

  private errMessage(err: unknown): string {
    if (err instanceof ApiError) {
      // Friendly 409 surface — the response body carries the
      // affected coworker ids; we just count them.
      if (err.status === 409 && err.body?.details) {
        const ids = (err.body.details as Record<string, unknown>).coworker_ids;
        if (Array.isArray(ids)) {
          return `This credential is in use by ${ids.length} coworker(s). Detach them before deleting.`;
        }
      }
      return err.body?.message ?? `${err.status}`;
    }
    return (err as Error).message;
  }

  override render() {
    return html`
      <div class="rm-spane">
        <div class="rm-ch">
          <h2>Credentials</h2>
          <button
            type="button"
            class="rm-add"
            @click=${() => {
              // null provider = let the dialog show its provider picker
              this.dialogProvider = null;
              this.dialogOpen = true;
            }}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
              stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="M12 5v14M5 12h14"/>
            </svg>
            Add credential
          </button>
        </div>
        <p class="rm-sub">
          One credential per provider. Keys are envelope-encrypted
          server-side and never displayed back.
        </p>

        ${this.loading
          ? html`<div class="rm-banner-loading">Loading…</div>`
          : this.listError
            ? html`<div class="rm-banner-err">${this.listError}</div>`
            : html`${PROVIDERS.map((p) => this.renderProvider(p))}`}

        <rm-credential-dialog
          ?open=${this.dialogOpen}
          .provider=${this.dialogProvider}
          @close=${() => {
            this.dialogOpen = false;
            this.dialogProvider = null;
          }}
          @credential-saved=${() => { void this.refresh(); }}
        ></rm-credential-dialog>
      </div>
    `;
  }

  private renderProvider(provider: ModelProvider) {
    const existing = this.rowFor(provider);
    const err = this.deleteError[provider] || this.putErrors[provider] || '';
    const initials = provider.slice(0, 2).toUpperCase();
    return html`
      <div class="rm-card" data-provider=${provider}>
        <span class="rm-ic">${initials}</span>
        <span class="rm-mn">
          <b style="text-transform: capitalize;">${provider}</b>
          <span>${existing
            ? `set ${this.fmtDate(existing.updated_at)}`
            : 'not configured — coworkers using this provider cannot run'}</span>
        </span>
        ${existing
          ? html`<span class="rm-pill rm-pill-on">set</span>`
          : html`<span class="rm-pill rm-pill-warn">missing</span>`}
        <span class="rm-row-acts">
          <button
            type="button"
            class="rm-iconbtn"
            title=${existing ? 'Rotate credential' : 'Add credential'}
            data-testid="credential-edit"
            @click=${() => this.openDialog(provider)}
          >${iconPencil(15)}</button>
          ${existing
            ? html`<button
                type="button"
                class="rm-iconbtn rm-iconbtn--danger"
                title="Delete credential"
                data-testid="credential-delete"
                @click=${() => void this.removeCredential(provider)}
              >${iconTrash(15)}</button>`
            : nothing}
        </span>
        ${err
          ? html`<div class="rm-row-error">${err}</div>`
          : nothing}
      </div>
    `;
  }

  private fmtDate(iso: string): string {
    try {
      const d = new Date(iso);
      if (Number.isNaN(d.getTime())) return iso;
      return d.toLocaleDateString();
    } catch {
      return iso;
    }
  }
}
