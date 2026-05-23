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

import { LitElement, html } from 'lit';
import { customElement, state } from 'lit/decorators.js';

import { ApiError, getApiClient } from '../api/client.js';
import type { CredentialResponse, ModelProvider } from '../api/client.js';

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
  @state() private justSaved: Record<string, number> = {};
  // The form's draft API-key value. Keyed by provider so a user can
  // start typing one and switch providers without losing the entry.
  @state() private drafts: Record<string, string> = {};
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

  private async save(provider: ModelProvider): Promise<void> {
    const key = this.drafts[provider]?.trim();
    if (!key) {
      this.putErrors = { ...this.putErrors, [provider]: 'API key is required.' };
      return;
    }
    this.inFlight = { ...this.inFlight, [provider]: true };
    this.putErrors = { ...this.putErrors, [provider]: '' };
    try {
      await this.api.putCredential(provider, { api_key: key });
      // Discard the plaintext from form state ASAP.
      this.drafts = { ...this.drafts, [provider]: '' };
      this.justSaved = { ...this.justSaved, [provider]: Date.now() };
      await this.refresh();
    } catch (err) {
      this.putErrors = {
        ...this.putErrors,
        [provider]:
          err instanceof ApiError
            ? err.body?.message || `${err.status}`
            : (err as Error).message,
      };
    } finally {
      this.inFlight = { ...this.inFlight, [provider]: false };
    }
  }

  private async remove(provider: ModelProvider): Promise<void> {
    this.inFlight = { ...this.inFlight, [provider]: true };
    this.putErrors = { ...this.putErrors, [provider]: '' };
    try {
      await this.api.deleteCredential(provider);
      await this.refresh();
    } catch (err) {
      this.putErrors = {
        ...this.putErrors,
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

  private isJustSaved(provider: ModelProvider): boolean {
    const at = this.justSaved[provider];
    return !!at && Date.now() - at < 4000;
  }

  override render() {
    return html`
      <div class="h-full w-full overflow-y-auto px-6 py-6">
        <div class="max-w-3xl mx-auto">
          <div class="flex items-baseline justify-between mb-4">
            <div>
              <h1 class="text-[20px] font-semibold text-ink-0 dark:text-d-ink-0">
                Credentials
              </h1>
              <p class="text-[13px] text-ink-3 dark:text-d-ink-3 mt-0.5">
                One credential per provider. Setting a value
                overwrites the previous one — keys are
                envelope-encrypted server-side and never displayed
                back.
              </p>
            </div>
            <button
              type="button"
              class="text-[12px] px-2.5 py-1 rounded-md border border-surface-3 dark:border-d-surface-3
                text-ink-2 dark:text-d-ink-2 hover:bg-surface-2 dark:hover:bg-d-surface-2 cursor-pointer"
              @click=${() => void this.refresh()}
            >Refresh</button>
          </div>

          ${this.loading
            ? html`<div class="text-[13px] text-ink-3 dark:text-d-ink-3">Loading…</div>`
            : this.listError
              ? html`
                  <div
                    class="border border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-900/20
                      text-red-700 dark:text-red-300 text-[13px] px-3 py-2 rounded-lg"
                  >${this.listError}</div>
                `
              : html`
                  <div class="space-y-3">
                    ${PROVIDERS.map((p) => this.renderProvider(p))}
                  </div>
                `}
        </div>
      </div>
    `;
  }

  private renderProvider(provider: ModelProvider) {
    const existing = this.rowFor(provider);
    const draft = this.drafts[provider] ?? '';
    const err = this.putErrors[provider] || '';
    const busy = !!this.inFlight[provider];
    const saved = this.isJustSaved(provider);
    return html`
      <section
        class="border border-surface-3 dark:border-d-surface-3 rounded-xl px-4 py-3"
      >
        <header class="flex items-center justify-between mb-2">
          <div class="text-[14px] font-medium capitalize text-ink-0 dark:text-d-ink-0">
            ${provider}
          </div>
          ${existing
            ? html`<span class="text-[11.5px] text-ink-3 dark:text-d-ink-3">
                last set ${this.fmtDate(existing.updated_at)}
              </span>`
            : html`<span class="text-[11.5px] text-ink-4">not configured</span>`}
        </header>
        <div class="flex items-center gap-2">
          <input
            type="password"
            autocomplete="new-password"
            spellcheck="false"
            placeholder=${existing
              ? 'Enter a new key to rotate (existing key is hidden)'
              : 'Enter the API key'}
            class="flex-1 text-[13px] px-3 py-1.5 rounded-md border border-surface-3 dark:border-d-surface-3
              bg-surface-1 dark:bg-d-surface-1 text-ink-0 dark:text-d-ink-0
              focus:outline-none focus:ring-2 focus:ring-brand"
            .value=${draft}
            @input=${(e: Event) => {
              const v = (e.target as HTMLInputElement).value;
              this.drafts = { ...this.drafts, [provider]: v };
            }}
            ?disabled=${busy}
          />
          <button
            type="button"
            class="text-[12px] px-3 py-1.5 rounded-md bg-brand text-white
              hover:bg-brand-dark transition-colors cursor-pointer
              disabled:opacity-60 disabled:cursor-not-allowed"
            ?disabled=${busy || draft.length === 0}
            @click=${() => void this.save(provider)}
          >${existing ? 'Rotate' : 'Save'}</button>
          ${existing
            ? html`<button
                type="button"
                class="text-[12px] px-3 py-1.5 rounded-md border border-red-300 dark:border-red-700
                  text-red-700 dark:text-red-300 hover:bg-red-50 dark:hover:bg-red-900/20 cursor-pointer
                  disabled:opacity-60 disabled:cursor-not-allowed"
                ?disabled=${busy}
                @click=${() => void this.remove(provider)}
              >Delete</button>`
            : null}
        </div>
        ${err
          ? html`<div class="text-[12px] text-red-600 dark:text-red-300 mt-2">${err}</div>`
          : null}
        ${saved
          ? html`<div class="text-[12px] text-emerald-600 dark:text-emerald-300 mt-2">
              Saved — server-side hot-reload signaled to coworkers using this provider.
            </div>`
          : null}
      </section>
    `;
  }

  private fmtDate(iso: string): string {
    try {
      const d = new Date(iso);
      if (Number.isNaN(d.getTime())) return iso;
      return d.toLocaleString();
    } catch {
      return iso;
    }
  }
}
