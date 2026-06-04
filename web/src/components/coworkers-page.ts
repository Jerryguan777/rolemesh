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
import type { Coworker, ModelProvider } from '../api/client.js';
import './coworker-wizard.js';
import './credential-dialog.js';
import './mcp-server-dialog.js';
import './confirm-dialog.js';
import type { CoworkerWizard } from './coworker-wizard.js';
import { iconPencil, iconTrash } from './icons.js';

@customElement('rm-coworkers-page')
export class CoworkersPage extends LitElement {
  @state() private rows: Coworker[] = [];
  @state() private loading = true;
  @state() private error: string | null = null;
  @state() private wizardOpen = false;
  @state() private credDialogOpen = false;
  @state() private credDialogProvider: ModelProvider | null = null;
  @state() private mcpDialogOpen = false;
  /** When set, the wizard runs in edit mode (pre-fills + PATCH on
   *  submit). `null` = create mode. */
  @state() private editTarget: Coworker | null = null;
  /** Per-row delete error, keyed by coworker id. Cleared on the next
   *  refresh so a successful retry returns the row to its normal state. */
  @state() private deleteError: Record<string, string> = {};
  /** When non-null, the delete confirmation dialog is open with this
   *  coworker as the target. Replaces the native `window.confirm`
   *  which ignored the theme and broke the v2 visual language. */
  @state() private deleteTarget: Coworker | null = null;
  /** True while the delete API call is in flight — disables the
   *  Confirm button so a double-click can't fire two DELETEs. */
  @state() private deleteInFlight = false;
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

  private openEdit(c: Coworker): void {
    // Edit reuses the create wizard — same 6 steps, pre-filled. The
    // wizard self-fetches the coworker's MCP / skill bindings on open
    // (see seedFromEditing).
    this.editTarget = c;
    this.wizardOpen = true;
  }

  private askDelete(c: Coworker): void {
    this.deleteTarget = c;
  }

  private cancelDelete = (): void => {
    if (this.deleteInFlight) return;
    this.deleteTarget = null;
  };

  private async performDelete(): Promise<void> {
    const c = this.deleteTarget;
    if (!c || this.deleteInFlight) return;
    this.deleteInFlight = true;
    this.deleteError = { ...this.deleteError, [c.id]: '' };
    try {
      await this.api.deleteCoworker(c.id);
      this.deleteTarget = null;
      await this.refresh();
    } catch (err) {
      this.deleteError = {
        ...this.deleteError,
        [c.id]:
          err instanceof ApiError
            ? `${err.status} — ${err.body?.message ?? err.message}`
            : (err as Error).message ?? 'delete failed',
      };
      // Close on error too — the per-row banner surfaces the message,
      // and leaving the modal up would trap the user under it.
      this.deleteTarget = null;
    } finally {
      this.deleteInFlight = false;
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
      <div class="rm-spane">
        <div class="rm-ch">
          <h2>Coworkers</h2>
          <button
            type="button"
            class="rm-add"
            @click=${() => { this.wizardOpen = true; }}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
              stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="M12 5v14M5 12h14"/>
            </svg>
            New coworker
          </button>
        </div>
        <p class="rm-sub">
          Each coworker is assembled from an engine, a model, bound MCP
          servers and skills. Click one to chat or edit.
        </p>

        ${this.loading
          ? html`<div class="rm-banner-loading">Loading…</div>`
          : this.error
            ? html`<div class="rm-banner-err">${this.error}</div>`
            : this.rows.length === 0
              ? this.renderEmpty()
              : this.renderList()}
        <rm-coworker-wizard
          ?open=${this.wizardOpen}
          .editing=${this.editTarget}
          @close=${() => {
            this.wizardOpen = false;
            this.editTarget = null;
            // Refresh once the wizard closes — a successful create
            // also navigates away via location.href, but a partial-
            // commit close (or successful edit) should still surface
            // the row state change.
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
        ${this.renderDeleteDialog()}
      </div>
    `;
  }

  private renderDeleteDialog() {
    const target = this.deleteTarget;
    return html`
      <rm-confirm-dialog
        title=${target ? `Delete coworker "${target.name}"?` : 'Delete coworker?'}
        ?open=${target !== null}
        tone="danger"
        confirm-label="Delete"
        busy-label="Deleting…"
        ?busy=${this.deleteInFlight}
        data-testid="confirm-delete-dialog"
        @cancel=${this.cancelDelete}
        @confirm=${() => void this.performDelete()}
      >
        <p style="margin: 0;">
          This also drops every conversation, run, and message the
          coworker has on file (cascaded by the database). Cannot be
          undone.
        </p>
      </rm-confirm-dialog>
    `;
  }

  private renderEmpty() {
    return html`
      <div class="rm-empty">
        <span class="rm-empty-title">No coworkers yet</span>
        Click <b>+ New coworker</b> above to start the 6-step setup.
      </div>
    `;
  }

  /** Map CoworkerStatus → pill modifier class. */
  private pillClass(status: Coworker['status']): string {
    if (status === 'active') return 'rm-pill rm-pill-on';
    if (status === 'paused') return 'rm-pill rm-pill-warn';
    return 'rm-pill rm-pill-off';
  }

  private renderList() {
    return html`
      ${this.rows.map(
        (c) => html`
          <div
            class="rm-card"
            data-coworker-id=${c.id}
            @click=${() => this.startChat(c)}
            role="button"
            tabindex="0"
            style="cursor: pointer;"
          >
            <span class="rm-ic">${(c.name?.[0] ?? '?').toUpperCase()}</span>
            <span class="rm-mn">
              <b>${c.name}</b>
              <span>${c.agent_backend} · ${c.id.slice(0, 8)}</span>
            </span>
            <span class=${this.pillClass(c.status)}>${c.status}</span>
            <span class="rm-row-acts">
              <button
                type="button"
                class="rm-iconbtn"
                title="Edit coworker"
                data-testid="coworker-edit"
                @click=${(e: Event) => {
                  e.stopPropagation();
                  this.openEdit(c);
                }}
              >${iconPencil(15)}</button>
              <button
                type="button"
                class="rm-iconbtn rm-iconbtn--danger"
                title="Delete coworker"
                data-testid="coworker-delete"
                @click=${(e: Event) => {
                  e.stopPropagation();
                  this.askDelete(c);
                }}
              >${iconTrash(15)}</button>
            </span>
            ${this.deleteError[c.id]
              ? html`<div class="rm-row-error">${this.deleteError[c.id]}</div>`
              : nothing}
          </div>
        `,
      )}
    `;
  }
}
