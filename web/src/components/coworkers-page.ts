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
import { customElement, query, state } from 'lit/decorators.js';

import { ApiError, getApiClient } from '../api/client.js';
import type { Coworker, Model, ModelProvider } from '../api/client.js';
import './coworker-wizard.js';
import './coworker-edit-dialog.js';
import './credential-dialog.js';
import './mcp-server-dialog.js';
import type { CoworkerWizard } from './coworker-wizard.js';
import { iconPencil, iconTrash } from './icons.js';

@customElement('rm-coworkers-page')
export class CoworkersPage extends LitElement {
  @state() private rows: Coworker[] = [];
  @state() private models: Model[] = [];
  @state() private loading = true;
  @state() private error: string | null = null;
  @state() private wizardOpen = false;
  @state() private credDialogOpen = false;
  @state() private credDialogProvider: ModelProvider | null = null;
  @state() private mcpDialogOpen = false;
  @state() private editDialogOpen = false;
  @state() private editTarget: Coworker | null = null;
  /** Per-row delete error, keyed by coworker id. Cleared on the next
   *  refresh so a successful retry returns the row to its normal state. */
  @state() private deleteError: Record<string, string> = {};
  @query('rm-coworker-wizard') private wizardEl?: CoworkerWizard;
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
    this.deleteError = {};
    // Fetch coworkers and models in parallel — the edit dialog needs
    // the model catalogue to render its dropdown; the list itself
    // doesn't need it for the row label since the chat-shell carries
    // the lookup. Failure on either is non-fatal: empty models list
    // hides the dropdown, empty coworker list shows the empty state.
    const [cwResult, mdResult] = await Promise.allSettled([
      this.api.listCoworkers(),
      this.api.listModels(),
    ]);
    if (cwResult.status === 'fulfilled') {
      this.rows = cwResult.value;
    } else {
      const err = cwResult.reason;
      this.rows = [];
      this.error =
        err instanceof ApiError
          ? `${err.status} — ${err.message}`
          : (err as Error).message ?? 'unknown error';
    }
    this.models = mdResult.status === 'fulfilled' ? mdResult.value : [];
    this.loading = false;
  }

  private openEdit(c: Coworker): void {
    this.editTarget = c;
    this.editDialogOpen = true;
  }

  private async confirmDelete(c: Coworker): Promise<void> {
    const ok = window.confirm(
      `Delete coworker "${c.name}"?\n\n` +
        'This also drops every conversation, run, and message the ' +
        'coworker has on file (cascaded by the database). Cannot be undone.',
    );
    if (!ok) return;
    this.deleteError = { ...this.deleteError, [c.id]: '' };
    try {
      await this.api.deleteCoworker(c.id);
      await this.refresh();
    } catch (err) {
      this.deleteError = {
        ...this.deleteError,
        [c.id]:
          err instanceof ApiError
            ? `${err.status} — ${err.body?.message ?? err.message}`
            : (err as Error).message ?? 'delete failed',
      };
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
            <div class="flex items-center gap-2">
              <button
                type="button"
                class="text-[12px] px-3 py-1.5 rounded-md bg-brand text-white
                  hover:bg-brand-dark transition-colors cursor-pointer"
                @click=${() => { this.wizardOpen = true; }}
              >+ New coworker</button>
              <button
                type="button"
                class="text-[12px] px-2.5 py-1 rounded-md border border-surface-3 dark:border-d-surface-3
                  text-ink-2 dark:text-d-ink-2 hover:bg-surface-2 dark:hover:bg-d-surface-2 cursor-pointer"
                @click=${() => void this.refresh()}
              >Refresh</button>
            </div>
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
        <rm-coworker-wizard
          ?open=${this.wizardOpen}
          @close=${() => {
            this.wizardOpen = false;
            // Refresh once the wizard closes — a successful create
            // also navigates away via location.href, but a partial-
            // commit close should still surface the new coworker.
            void this.refresh();
          }}
          @request-credential=${(e: CustomEvent<{ provider: ModelProvider }>) => {
            this.credDialogProvider = e.detail.provider;
            this.credDialogOpen = true;
          }}
          @request-add-mcp-server=${() => {
            this.mcpDialogOpen = true;
          }}
        ></rm-coworker-wizard>
        <rm-credential-dialog
          ?open=${this.credDialogOpen}
          .provider=${this.credDialogProvider}
          @close=${() => { this.credDialogOpen = false; }}
          @credential-saved=${() => {
            void this.wizardEl?.refreshCredentials();
          }}
        ></rm-credential-dialog>
        <rm-mcp-server-dialog
          ?open=${this.mcpDialogOpen}
          @close=${() => { this.mcpDialogOpen = false; }}
          @mcp-server-created=${() => {
            void this.wizardEl?.refreshMCPServers();
          }}
        ></rm-mcp-server-dialog>
        <rm-coworker-edit-dialog
          ?open=${this.editDialogOpen}
          .coworker=${this.editTarget}
          .models=${this.models}
          @close=${() => {
            this.editDialogOpen = false;
            this.editTarget = null;
          }}
          @coworker-saved=${() => { void this.refresh(); }}
        ></rm-coworker-edit-dialog>
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
    // CSS contract: `.coworker-row` is the hover target; `.row-acts`
    // group sits at opacity:0 until the row is hovered or focused.
    // Inline <style> here matches the v2 pattern in chat-shell —
    // page-scoped rules ride with the rendered output instead of
    // living in a separate stylesheet.
    return html`
      <style>
        rm-coworkers-page .row-acts {
          opacity: 0;
          transition: opacity 0.13s;
        }
        rm-coworkers-page .coworker-row:hover .row-acts,
        rm-coworkers-page .coworker-row:focus-within .row-acts {
          opacity: 1;
        }
        rm-coworkers-page .icon-btn {
          width: 28px;
          height: 28px;
          border-radius: 7px;
          display: grid;
          place-items: center;
          color: var(--rm-ink-3);
          background: none;
          border: none;
          cursor: pointer;
          transition: 0.13s;
        }
        rm-coworkers-page .icon-btn:hover {
          background: var(--rm-surface-3);
          color: var(--rm-ink);
        }
        rm-coworkers-page .icon-btn.danger:hover {
          background: var(--rm-bad-subtle);
          color: var(--rm-bad);
        }
      </style>
      <ul class="divide-y divide-surface-3 dark:divide-d-surface-3 border border-surface-3 dark:border-d-surface-3 rounded-xl overflow-hidden">
        ${this.rows.map(
          (c) => html`
            <li class="coworker-row flex items-center gap-4 px-4 py-3" data-coworker-id=${c.id}>
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
                ${this.deleteError[c.id]
                  ? html`<div class="text-[11.5px] text-red-600 dark:text-red-300 mt-1">
                      ${this.deleteError[c.id]}
                    </div>`
                  : nothing}
              </div>
              <div class="row-acts flex items-center gap-1">
                <button
                  type="button"
                  class="icon-btn"
                  title="Edit coworker"
                  data-testid="coworker-edit"
                  @click=${() => this.openEdit(c)}
                >${iconPencil(15)}</button>
                <button
                  type="button"
                  class="icon-btn danger"
                  title="Delete coworker"
                  data-testid="coworker-delete"
                  @click=${() => void this.confirmDelete(c)}
                >${iconTrash(15)}</button>
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
