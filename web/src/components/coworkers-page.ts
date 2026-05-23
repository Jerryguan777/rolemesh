// Minimal Coworkers list (session 01c PR 3).
//
// Lists `GET /api/v1/coworkers` and offers a "Start chat" affordance
// per row that hash-routes to `#/?agent_id=<id>`. The chat URL
// contract is unchanged — chat-panel reads `agent_id` from the
// query string and uses it as the v1 `coworker_id` (the two are
// the same UUID).
//
// What this page is NOT (out of scope, deferred):
//   * Coworker creation wizard — 02a.
//   * Detail tabs (overview / skills / mcp / bindings / …) — Phase 2+.
//   * Pause / disable / delete affordances — Phase 2+.
//
// We render an empty-state with a clear pointer to the API surface
// for now, so a fresh tenant doesn't see a blank screen.

import { LitElement, html, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';

import { ApiError, getApiClient } from '../api/client.js';
import type { Coworker } from '../api/client.js';

@customElement('rm-coworkers-page')
export class CoworkersPage extends LitElement {
  @state() private rows: Coworker[] = [];
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
      this.rows = await this.api.listCoworkers();
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

  private startChat(coworker: Coworker): void {
    // chat-panel reads `agent_id` from the query string; setting it
    // before navigating to `#/` keeps the legacy URL contract intact.
    const params = new URLSearchParams(location.search);
    params.set('agent_id', coworker.id);
    params.delete('chat_id');
    const url = `${location.pathname}?${params.toString()}#/`;
    location.href = url;
  }

  override render() {
    return html`
      <div class="h-full w-full overflow-y-auto px-6 py-6">
        <div class="max-w-3xl mx-auto">
          <div class="flex items-baseline justify-between mb-4">
            <div>
              <h1 class="text-[20px] font-semibold text-ink-0 dark:text-d-ink-0">
                Coworkers
              </h1>
              <p class="text-[13px] text-ink-3 dark:text-d-ink-3 mt-0.5">
                Pick a coworker to start chatting. Creation wizard
                lands in Phase 2.
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
                : this.renderList()}
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
          No coworkers yet
        </p>
        <p class="leading-relaxed">
          Create one via <code>POST /api/v1/coworkers</code> from the
          backend tooling. A web UI for this lands in Phase 2 (02a).
        </p>
      </div>
    `;
  }

  private renderList() {
    return html`
      <ul class="divide-y divide-surface-3 dark:divide-d-surface-3 border border-surface-3 dark:border-d-surface-3 rounded-xl overflow-hidden">
        ${this.rows.map(
          (c) => html`
            <li class="flex items-center gap-4 px-4 py-3">
              <div
                class="w-9 h-9 rounded-lg bg-gradient-to-br from-brand-light to-brand
                  flex items-center justify-center text-white text-[14px] font-semibold
                  shrink-0"
              >${(c.name?.[0] ?? '?').toUpperCase()}</div>
              <div class="min-w-0 flex-1">
                <div class="text-[14px] font-medium text-ink-0 dark:text-d-ink-0 truncate">
                  ${c.name}
                </div>
                <div class="text-[11.5px] text-ink-3 dark:text-d-ink-3 flex items-center gap-2 mt-0.5 flex-wrap">
                  <span>${c.agent_backend}</span>
                  <span class="text-ink-4">·</span>
                  <span>${c.status}</span>
                  <span class="text-ink-4">·</span>
                  <span class="font-mono">${c.id.slice(0, 8)}</span>
                  ${c.agent_role === 'super_agent'
                    ? html`<span
                        class="text-ink-4">·</span>
                        <span class="text-brand">super</span>`
                    : nothing}
                </div>
              </div>
              <button
                type="button"
                class="text-[12px] px-3 py-1.5 rounded-md bg-brand text-white
                  hover:bg-brand-dark transition-colors cursor-pointer
                  disabled:opacity-60 disabled:cursor-not-allowed"
                ?disabled=${c.status === 'disabled'}
                title=${c.status === 'disabled'
                  ? 'Coworker is disabled'
                  : 'Open chat with this coworker'}
                @click=${() => this.startChat(c)}
              >Start chat</button>
            </li>
          `,
        )}
      </ul>
    `;
  }
}
