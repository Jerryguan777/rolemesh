// Read-only Models catalog (#/models).
//
// Lists `GET /api/v1/models` grouped by provider. No admin-write
// surface in v1.1 — design §14 defers that to v2; the empty state
// instructs the operator to use the backend tooling.

import { LitElement, html, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';

import { ApiError, getApiClient } from '../api/client.js';
import type { Model } from '../api/client.js';

@customElement('rm-models-page')
export class ModelsPage extends LitElement {
  @state() private rows: Model[] = [];
  @state() private loading = true;
  @state() private error: string | null = null;
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
    this.error = null;
    try {
      this.rows = await this.api.listModels();
    } catch (err) {
      this.rows = [];
      this.error =
        err instanceof ApiError
          ? `${err.status} — ${err.message}`
          : (err as Error).message ?? 'unknown error';
    } finally {
      this.loading = false;
    }
  }

  private groupByProvider(): Record<string, Model[]> {
    const groups: Record<string, Model[]> = {};
    for (const r of this.rows) {
      (groups[r.provider] ??= []).push(r);
    }
    return groups;
  }

  override render() {
    return html`
      <div class="h-full w-full overflow-y-auto px-6 py-6">
        <div class="max-w-3xl mx-auto">
          <div class="flex items-baseline justify-between mb-4">
            <div>
              <h1 class="text-[20px] font-semibold text-ink-0 dark:text-d-ink-0">
                Models
              </h1>
              <p class="text-[13px] text-ink-3 dark:text-d-ink-3 mt-0.5">
                Platform-managed catalog. Read-only here — admin
                writes land in v2.
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
            : this.error
              ? html`
                  <div
                    class="border border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-900/20
                      text-red-700 dark:text-red-300 text-[13px] px-3 py-2 rounded-lg"
                  >${this.error}</div>
                `
              : this.rows.length === 0
                ? this.renderEmpty()
                : this.renderGroups()}
        </div>
      </div>
    `;
  }

  private renderEmpty() {
    return html`
      <div
        class="border border-dashed border-surface-3 dark:border-d-surface-3
          rounded-xl px-6 py-10 text-center text-[13px] text-ink-2 dark:text-d-ink-2"
      >
        <p class="mb-1.5 font-medium text-ink-1 dark:text-d-ink-1">
          No models available
        </p>
        <p class="leading-relaxed">
          The platform catalog appears empty. Re-run the schema seed
          (<code>_create_schema</code>) or check the
          <code>models</code> table directly.
        </p>
      </div>
    `;
  }

  private renderGroups() {
    const groups = this.groupByProvider();
    const providers = Object.keys(groups).sort();
    return html`
      <div class="space-y-4">
        ${providers.map((p) => this.renderProviderCard(p, groups[p]!))}
      </div>
    `;
  }

  private renderProviderCard(provider: string, items: Model[]) {
    return html`
      <section
        class="border border-surface-3 dark:border-d-surface-3 rounded-xl overflow-hidden"
      >
        <header
          class="px-4 py-2 bg-surface-2 dark:bg-d-surface-2
            text-[13px] font-medium text-ink-1 dark:text-d-ink-1
            flex items-center gap-2"
        >
          <span class="capitalize">${provider}</span>
          <span class="text-ink-4 text-[11.5px]">
            (${items.length})
          </span>
        </header>
        <ul class="divide-y divide-surface-3 dark:divide-d-surface-3">
          ${items.map(
            (m) => html`
              <li class="px-4 py-2.5 flex items-center gap-3">
                <div class="min-w-0 flex-1">
                  <div class="text-[13.5px] text-ink-0 dark:text-d-ink-0 truncate">
                    ${m.display_name}
                  </div>
                  <div class="text-[11.5px] text-ink-3 dark:text-d-ink-3 mt-0.5 font-mono truncate">
                    ${m.model_id}
                  </div>
                </div>
                <span class="text-[11.5px] text-ink-3 dark:text-d-ink-3">
                  ${m.model_family}
                </span>
                ${m.is_active
                  ? nothing
                  : html`<span
                      class="text-[11px] px-1.5 py-0.5 rounded bg-amber-100 dark:bg-amber-900/40
                        text-amber-800 dark:text-amber-200"
                    >inactive</span>`}
              </li>
            `,
          )}
        </ul>
      </section>
    `;
  }
}
