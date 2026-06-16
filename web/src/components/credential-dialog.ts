// <rm-credential-dialog> — per-provider credential capture.
//
// Wraps <rm-dialog> (v2-A primitive). Knows which provider the user
// is configuring; renders the right set of fields (anthropic = one
// API key, bedrock = API key + region, etc.) and writes via PUT
// /api/v1/credentials/{provider} with the open-shape
// `extras: { ... }` map per OpenAPI.
//
// Invariants:
//   - Never displays an existing credential's value back. The
//     payload from GET is metadata-only by design; this dialog never
//     pre-fills extras either, because the server does not return
//     them on read (the credential vault is write-only outside the
//     proxy). The dialog renders empty fields with a "Set new
//     value" placeholder when the provider already has a credential.
//   - We do not console.log / banner any submitted body — only error
//     messages. The plaintext key never leaves the form except via
//     the PUT body itself.
//   - On success the dialog fires `@credential-saved` with the
//     provider; the host (the coworkers page mounting the wizard)
//     listens and asks the wizard to refresh credentials so the
//     "needs credential" group re-renders ready.
//
// Sibling to the wizard (locked decision #3) — never mounted inside
// it. Native <dialog>'s top-layer stacks above whatever the wizard
// overlay is doing.

import { LitElement, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';

import './dialog.js';
import { ApiError, getApiClient } from '../api/client.js';
import type {
  CredentialUpsert,
  CredentialValidationResult,
  ModelProvider,
} from '../api/client.js';

/** Per-provider field shape. The `extras_keys` list pins which keys
 *  flow into `extras: {...}`. `optional_extras_keys` are not validated
 *  for non-emptiness. */
export interface ProviderSchema {
  provider: ModelProvider;
  label: string;
  /** Help text under the title. */
  blurb: string;
  /** The `api_key` field's placeholder + label. Every provider has
   *  exactly one API-key-shaped field (it lands in the top-level
   *  `api_key` field per OpenAPI). */
  apiKey: { label: string; placeholder: string; helperText?: string };
  /** Required extras the user must fill in. Each lands in
   *  `extras[key]`. */
  requiredExtras: { key: string; label: string; placeholder?: string; defaultValue?: string }[];
  /** Optional extras. */
  optionalExtras: { key: string; label: string; placeholder?: string }[];
}

export const PROVIDER_SCHEMAS: ProviderSchema[] = [
  {
    provider: 'anthropic',
    label: 'Anthropic',
    blurb: 'Anthropic Claude API key. Used directly by the Claude proxy.',
    apiKey: {
      label: 'API key',
      placeholder: 'sk-ant-…',
    },
    requiredExtras: [],
    optionalExtras: [],
  },
  {
    provider: 'openai',
    label: 'OpenAI',
    blurb: 'OpenAI / compatible API. Override the base URL for self-hosted gateways.',
    apiKey: {
      label: 'API key',
      placeholder: 'sk-…',
    },
    requiredExtras: [],
    optionalExtras: [
      {
        key: 'api_base',
        label: 'API base URL (optional)',
        placeholder: 'https://api.openai.com/v1',
      },
    ],
  },
  {
    provider: 'google',
    label: 'Google',
    blurb: 'Google AI Studio / Gemini API key.',
    apiKey: {
      label: 'API key',
      placeholder: 'AI…',
    },
    requiredExtras: [],
    optionalExtras: [],
  },
  {
    provider: 'bedrock',
    label: 'AWS Bedrock',
    blurb:
      'Bedrock long-term API key + region. The credential proxy authenticates to Bedrock with this key as a Bearer token.',
    apiKey: {
      label: 'Bedrock API key',
      placeholder: 'ABSK…',
      helperText:
        'Console → Bedrock → API keys → Generate long-term API key. Stored as the credential\'s primary key; the region lands in extras.',
    },
    requiredExtras: [
      {
        key: 'region',
        label: 'Region',
        placeholder: 'us-east-1',
        defaultValue: 'us-east-1',
      },
    ],
    optionalExtras: [],
  },
];

export function schemaFor(provider: ModelProvider): ProviderSchema {
  const s = PROVIDER_SCHEMAS.find((x) => x.provider === provider);
  if (!s) throw new Error(`unknown provider: ${provider}`);
  return s;
}

@customElement('rm-credential-dialog')
export class CredentialDialog extends LitElement {
  /** Open + closed states are controlled by the host. */
  @property({ type: Boolean }) open = false;
  /** Locked provider. When null the dialog shows a provider picker. */
  @property({ attribute: false }) provider: ModelProvider | null = null;

  @state() private apiKey = '';
  @state() private extras: Record<string, string> = {};
  @state() private busy = false;
  @state() private err: string | null = null;
  /** Live "Test connection" state. `testing` disables the form while a
   *  probe is in flight; `testResult` carries the last verdict for inline
   *  display. Cleared whenever a field changes so a stale green tick can
   *  never imply a key the user has since edited is still valid. */
  @state() private testing = false;
  @state() private testResult: CredentialValidationResult | null = null;
  /** When `provider` prop is null, the user picks one here. */
  @state() private pickedProvider: ModelProvider = 'anthropic';

  private readonly api = getApiClient();

  protected override createRenderRoot() {
    return this;
  }

  override willUpdate(changed: Map<string, unknown>) {
    if (changed.has('open') && this.open) {
      // Reset state on (re)open.
      this.apiKey = '';
      this.err = null;
      this.busy = false;
      this.testing = false;
      this.testResult = null;
      this.extras = this.buildDefaultExtras(this.currentProvider());
    }
    if (changed.has('provider') && this.provider) {
      this.pickedProvider = this.provider;
      this.extras = this.buildDefaultExtras(this.provider);
    }
    // A prior verdict must not outlive the inputs it was about: editing
    // the key/region/provider invalidates the last test.
    if (
      this.testResult !== null &&
      (changed.has('apiKey') ||
        changed.has('extras') ||
        changed.has('pickedProvider'))
    ) {
      this.testResult = null;
    }
  }

  private currentProvider(): ModelProvider {
    return this.provider ?? this.pickedProvider;
  }

  private buildDefaultExtras(provider: ModelProvider): Record<string, string> {
    const schema = schemaFor(provider);
    const out: Record<string, string> = {};
    for (const e of schema.requiredExtras) {
      if (e.defaultValue !== undefined) out[e.key] = e.defaultValue;
    }
    return out;
  }

  /** Validate the form and assemble the `CredentialUpsert` body shared by
   *  Save and Test. Returns null (and sets `this.err`) on a client-side
   *  validation miss so callers can bail without duplicating the checks. */
  private buildBody(): CredentialUpsert | null {
    const provider = this.currentProvider();
    const schema = schemaFor(provider);
    if (this.apiKey.trim() === '') {
      this.err = `${schema.apiKey.label} is required.`;
      return null;
    }
    // Required extras must be non-empty.
    for (const e of schema.requiredExtras) {
      const v = (this.extras[e.key] ?? '').trim();
      if (v === '') {
        this.err = `${e.label} is required.`;
        return null;
      }
    }
    // Strip optional keys whose values are empty so we do not POST
    // `extras: { aws_session_token: '' }`.
    const extras: Record<string, string> = {};
    for (const e of schema.requiredExtras) {
      extras[e.key] = (this.extras[e.key] ?? '').trim();
    }
    for (const e of schema.optionalExtras) {
      const v = (this.extras[e.key] ?? '').trim();
      if (v !== '') extras[e.key] = v;
    }
    return {
      api_key: this.apiKey.trim(),
      extras: Object.keys(extras).length ? extras : null,
    };
  }

  /** Probe the credential against the provider without saving. A bad key
   *  comes back as a 200 with `ok=false`, so we render the verdict from
   *  the result rather than the catch (which is for transport faults). */
  private async validate(): Promise<void> {
    this.err = null;
    this.testResult = null;
    const body = this.buildBody();
    if (body === null) return;
    const provider = this.currentProvider();
    this.testing = true;
    try {
      this.testResult = await this.api.validateCredential(provider, body);
    } catch (err) {
      this.err =
        err instanceof ApiError
          ? err.body?.message ?? `${err.status}`
          : (err as Error).message;
    } finally {
      this.testing = false;
    }
  }

  private async save(): Promise<void> {
    const provider = this.currentProvider();
    const body = this.buildBody();
    if (body === null) return;

    this.busy = true;
    this.err = null;
    try {
      await this.api.putCredential(provider, body);
      // Drop the plaintext from form state immediately.
      this.apiKey = '';
      this.extras = this.buildDefaultExtras(provider);
      this.dispatchEvent(
        new CustomEvent<{ provider: ModelProvider }>('credential-saved', {
          detail: { provider },
          bubbles: true,
          composed: true,
        }),
      );
      this.open = false;
      this.dispatchEvent(
        new CustomEvent('close', { bubbles: true, composed: true }),
      );
    } catch (err) {
      this.err =
        err instanceof ApiError
          ? err.body?.message ?? `${err.status}`
          : (err as Error).message;
    } finally {
      this.busy = false;
    }
  }

  private close = () => {
    this.open = false;
    this.dispatchEvent(
      new CustomEvent('close', { bubbles: true, composed: true }),
    );
  };

  override render() {
    const provider = this.currentProvider();
    const schema = schemaFor(provider);
    return html`
      <rm-dialog
        title=${`Add ${schema.label} credential`}
        ?open=${this.open}
        ?close-on-backdrop=${!this.busy}
        ?close-on-esc=${!this.busy}
        width="480px"
        @close=${this.close}
      >
        <div class="text-[13px] text-ink-3 dark:text-d-ink-3 mb-3">
          ${schema.blurb}
        </div>

        ${this.provider === null ? this.renderProviderPicker() : nothing}

        <div class="mb-3">
          <label class="block text-[12.5px] font-medium mb-1">
            ${schema.apiKey.label}
          </label>
          <input
            type="password"
            autocomplete="new-password"
            spellcheck="false"
            class="w-full text-[13.5px] px-3 py-2 rounded-md border border-surface-3
              dark:border-d-surface-3 bg-surface-1 dark:bg-d-surface-1
              text-ink-0 dark:text-d-ink-0 focus:outline-none focus:ring-2 focus:ring-brand"
            placeholder=${schema.apiKey.placeholder}
            .value=${this.apiKey}
            @input=${(e: Event) => {
              this.apiKey = (e.target as HTMLInputElement).value;
            }}
            ?disabled=${this.busy}
          />
          ${schema.apiKey.helperText
            ? html`<div class="text-[11.5px] text-ink-3 dark:text-d-ink-3 mt-1">
                ${schema.apiKey.helperText}
              </div>`
            : nothing}
        </div>

        ${[...schema.requiredExtras, ...schema.optionalExtras].map(
          (e) => html`
            <div class="mb-3">
              <label class="block text-[12.5px] font-medium mb-1">${e.label}</label>
              <input
                type="text"
                spellcheck="false"
                class="w-full text-[13.5px] px-3 py-2 rounded-md border border-surface-3
                  dark:border-d-surface-3 bg-surface-1 dark:bg-d-surface-1
                  text-ink-0 dark:text-d-ink-0 focus:outline-none focus:ring-2 focus:ring-brand
                  font-mono"
                placeholder=${e.placeholder ?? ''}
                .value=${this.extras[e.key] ?? ''}
                @input=${(ev: Event) => {
                  this.extras = {
                    ...this.extras,
                    [e.key]: (ev.target as HTMLInputElement).value,
                  };
                }}
                ?disabled=${this.busy}
              />
            </div>
          `,
        )}

        <div class="text-[11.5px] text-ink-3 dark:text-d-ink-3 mt-2">
          The credential is envelope-encrypted server-side and never displayed back.
        </div>

        ${this.err
          ? html`<div
              class="text-[12.5px] text-red-600 dark:text-red-300 mt-2"
              role="alert"
            >${this.err}</div>`
          : nothing}

        ${this.renderTestResult()}

        <div slot="footer" style="display: flex; gap: 8px; justify-content: flex-end;">
          <button
            type="button"
            class="rm-btn rm-btn--secondary"
            ?disabled=${this.busy}
            @click=${this.close}
          >Cancel</button>
          <button
            type="button"
            class="rm-btn rm-btn--secondary"
            ?disabled=${this.busy || this.testing}
            @click=${() => void this.validate()}
          >${this.testing ? 'Testing…' : 'Test connection'}</button>
          <button
            type="button"
            class="rm-btn rm-btn--primary"
            ?disabled=${this.busy || this.testing}
            @click=${() => void this.save()}
          >${this.busy ? 'Saving…' : 'Save credential'}</button>
        </div>
      </rm-dialog>
    `;
  }

  /** Inline verdict from the last "Test connection". Green when the key
   *  is usable, amber when the endpoint is only reachable (Bedrock — key
   *  not exercised), red when rejected/unreachable. */
  private renderTestResult() {
    const r = this.testResult;
    if (r === null) return nothing;
    const tone = r.ok
      ? r.level === 'reachable'
        ? 'text-amber-600 dark:text-amber-300'
        : 'text-green-600 dark:text-green-300'
      : 'text-red-600 dark:text-red-300';
    const icon = r.ok ? (r.level === 'reachable' ? '⚠' : '✓') : '✕';
    return html`<div
      class="text-[12.5px] ${tone} mt-2"
      role="status"
    >${icon} ${r.detail}</div>`;
  }

  private renderProviderPicker() {
    return html`
      <div class="mb-3">
        <label class="block text-[12.5px] font-medium mb-1">Provider</label>
        <select
          class="w-full text-[13.5px] px-3 py-2 rounded-md border border-surface-3
            dark:border-d-surface-3 bg-surface-1 dark:bg-d-surface-1
            text-ink-0 dark:text-d-ink-0 focus:outline-none focus:ring-2 focus:ring-brand"
          .value=${this.pickedProvider}
          @change=${(e: Event) => {
            this.pickedProvider = (e.target as HTMLSelectElement)
              .value as ModelProvider;
            this.extras = this.buildDefaultExtras(this.pickedProvider);
            this.apiKey = '';
          }}
          ?disabled=${this.busy}
        >
          ${PROVIDER_SCHEMAS.map(
            (s) => html`<option value=${s.provider}>${s.label}</option>`,
          )}
        </select>
      </div>
    `;
  }
}
