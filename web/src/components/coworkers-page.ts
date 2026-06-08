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
import {
  canManage,
  hasCapability,
  isOwnResource,
} from '../auth/capabilities.js';
import './coworker-wizard.js';
import './credential-dialog.js';
import './mcp-server-dialog.js';
import './confirm-dialog.js';
import type { CoworkerWizard } from './coworker-wizard.js';
import { iconPencil, iconTrash, iconUsers } from './icons.js';

/** UX-only view filter over the already-server-filtered list. NOT a
 *  security boundary — the backend already visibility-filtered the rows
 *  (spec §7.3). These chips just re-narrow what's shown for the user's
 *  workflow. PR5 (skills page) mirrors this exact classification. */
export type CoworkerChip = 'all' | 'mine' | 'shared';

/** Classify one row against a chip selection. Pure + reusable so the
 *  skills page (PR5) can mirror it. Three-value safe: a row with a null
 *  `created_by_user_id` is never "mine" (it falls through `isOwnResource`,
 *  which returns false for null) and is only kept by the "shared" chip
 *  when its visibility says so — never by ownership. */
export function matchesChip(co: Coworker, chip: CoworkerChip): boolean {
  if (chip === 'all') return true;
  if (chip === 'mine') return isOwnResource(co);
  // 'shared' — others' shared rows only (own rows belong under "Mine").
  return co.visibility === 'shared' && !isOwnResource(co);
}

@customElement('rm-coworkers-page')
export class CoworkersPage extends LitElement {
  @state() private rows: Coworker[] = [];
  @state() private loading = true;
  @state() private error: string | null = null;
  /** Client-side view filter (spec §5.1 chips). Pure presentation — the
   *  list is already server-side visibility-filtered; this re-narrows it. */
  @state() private chip: CoworkerChip = 'all';
  /** Per-row share/unshare error, keyed by coworker id. Cleared on the
   *  next refresh, same lifecycle as `deleteError`. */
  @state() private shareError: Record<string, string> = {};
  /** Ids with a share/unshare POST in flight — disables that row's
   *  toggle so a double-click can't fire two visibility flips. */
  @state() private shareInFlight: Set<string> = new Set();
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
    this.shareError = {};
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

  /** Flip a coworker's visibility. `canManage` is the ONLY gate (spec
   *  §7.4 — no separate `*.share` capability); the toggle only renders
   *  where canManage is true, and the backend re-checks the ownership
   *  escape. Optimistic: we patch the local row from the returned
   *  coworker so the pill + tooltip flip without a full refresh. */
  private async toggleShare(c: Coworker): Promise<void> {
    if (this.shareInFlight.has(c.id)) return;
    this.shareInFlight = new Set(this.shareInFlight).add(c.id);
    this.shareError = { ...this.shareError, [c.id]: '' };
    try {
      const updated =
        c.visibility === 'shared'
          ? await this.api.unshareCoworker(c.id)
          : await this.api.shareCoworker(c.id);
      this.rows = this.rows.map((r) => (r.id === updated.id ? updated : r));
    } catch (err) {
      this.shareError = {
        ...this.shareError,
        [c.id]:
          err instanceof ApiError
            ? `${err.status} — ${err.body?.message ?? err.message}`
            : (err as Error).message ?? 'visibility change failed',
      };
    } finally {
      const next = new Set(this.shareInFlight);
      next.delete(c.id);
      this.shareInFlight = next;
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
    const canCreate = hasCapability('coworker.create');
    const visibleRows = this.rows.filter((c) => matchesChip(c, this.chip));
    return html`
      <div class="rm-spane">
        <div class="rm-ch">
          <h2>Coworkers</h2>
          ${canCreate
            ? html`<button
                type="button"
                class="rm-add"
                data-testid="coworker-new"
                @click=${() => { this.wizardOpen = true; }}
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
                  stroke="currentColor" stroke-width="2" aria-hidden="true">
                  <path d="M12 5v14M5 12h14"/>
                </svg>
                New coworker
              </button>`
            : nothing}
        </div>
        <p class="rm-sub">
          Each coworker is assembled from an engine, a model, bound MCP
          servers and skills. Click one to chat or edit.
        </p>

        ${this.loading || this.error || this.rows.length === 0
          ? nothing
          : this.renderChips()}

        ${this.loading
          ? html`<div class="rm-banner-loading">Loading…</div>`
          : this.error
            ? html`<div class="rm-banner-err">${this.error}</div>`
            : visibleRows.length === 0
              ? this.renderEmpty()
              : this.renderList(visibleRows)}
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

  private renderChips() {
    const chips: Array<[CoworkerChip, string]> = [
      ['all', 'All visible'],
      ['mine', 'Mine'],
      ['shared', 'Shared by others'],
    ];
    return html`
      <div class="rm-seg rm-chipbar" role="tablist" aria-label="Filter coworkers">
        ${chips.map(
          ([key, label]) => html`
            <button
              type="button"
              role="tab"
              class=${this.chip === key ? 'rm-seg--on' : ''}
              aria-selected=${this.chip === key ? 'true' : 'false'}
              data-testid="coworker-chip-${key}"
              @click=${() => { this.chip = key; }}
            >${label}</button>
          `,
        )}
      </div>
    `;
  }

  /** Empty-state copy varies by the active chip AND by whether the user
   *  can manage coworkers tenant-wide vs is a plain member (spec §5.1).
   *  Gated on a CAPABILITY (`coworker.manage`), never a role name — a
   *  member's next action is "ask an admin to share / create your own",
   *  an admin/owner's is "create the first one". */
  private renderEmpty() {
    const isManager = hasCapability('coworker.manage');
    let title: string;
    let body: unknown;
    if (this.chip === 'mine') {
      title = 'Nothing here yet';
      body = html`You haven’t created any coworkers yet.`;
    } else if (this.chip === 'shared') {
      title = 'Nothing shared';
      body = isManager
        ? html`No coworkers shared by others.`
        : html`No coworkers have been shared with you yet.`;
    } else {
      // 'all'
      title = 'No coworkers yet';
      body = isManager
        ? html`No coworkers in this tenant yet. Click
            <b>+ New coworker</b> above to create the first one.`
        : html`No coworkers yet. Create one with
            <b>+ New coworker</b>, or ask your admin to share theirs.`;
    }
    return html`
      <div class="rm-empty" data-testid="coworker-empty">
        <span class="rm-empty-title">${title}</span>
        ${body}
      </div>
    `;
  }

  /** Map CoworkerStatus → pill modifier class. */
  private pillClass(status: Coworker['status']): string {
    if (status === 'active') return 'rm-pill rm-pill-on';
    if (status === 'paused') return 'rm-pill rm-pill-warn';
    return 'rm-pill rm-pill-off';
  }

  /** Ownership cue (spec §5.1). There is NO owner display-name on the
   *  wire — only `created_by_user_id` — so own rows read "Created by
   *  you" in accent; everything else is a neutral label (never an
   *  invented person name). */
  private renderOwnTag(c: Coworker) {
    if (isOwnResource(c)) {
      return html`<span
        class="rm-own-tag rm-own-tag--mine"
        data-testid="coworker-own-tag"
      >Created by you</span>`;
    }
    return html`<span
      class="rm-own-tag rm-own-tag--other"
      data-testid="coworker-own-tag"
    >Shared by another member</span>`;
  }

  /** Visibility pill — green "Shared" / gray "Private" from the wire
   *  `visibility` (spec §7.4). */
  private renderVisibilityPill(c: Coworker) {
    const shared = c.visibility === 'shared';
    return html`<span
      class=${shared ? 'rm-pill rm-pill-on' : 'rm-pill rm-pill-off'}
      data-testid="coworker-visibility"
    >${shared ? 'Shared' : 'Private'}</span>`;
  }

  private renderRowActions(c: Coworker) {
    // Single gate for ALL management affordances — the ownership escape
    // (manage capability OR own the row), mirroring the backend. Members
    // see a "view only" hint instead of dead Edit/Delete/Share buttons.
    if (!canManage(c, 'coworker.manage')) {
      return html`<span class="rm-viewonly" data-testid="coworker-viewonly"
        >View only</span
      >`;
    }
    const shared = c.visibility === 'shared';
    const busy = this.shareInFlight.has(c.id);
    return html`
      <span class="rm-row-acts">
        <button
          type="button"
          class=${shared ? 'rm-iconbtn rm-iconbtn--on' : 'rm-iconbtn'}
          title=${shared
            ? 'Make private'
            : `Share with everyone in ${c.tenant_id}`}
          data-testid="coworker-share"
          ?disabled=${busy}
          aria-pressed=${shared ? 'true' : 'false'}
          @click=${(e: Event) => {
            e.stopPropagation();
            void this.toggleShare(c);
          }}
        >${iconUsers(15)}</button>
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
    `;
  }

  private renderList(rows: Coworker[]) {
    return html`
      ${rows.map(
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
              <span>
                ${c.agent_backend} · ${c.id.slice(0, 8)} ·
                ${this.renderOwnTag(c)}
              </span>
            </span>
            ${this.renderVisibilityPill(c)}
            <span class=${this.pillClass(c.status)}>${c.status}</span>
            ${this.renderRowActions(c)}
            ${this.deleteError[c.id]
              ? html`<div class="rm-row-error">${this.deleteError[c.id]}</div>`
              : nothing}
            ${this.shareError[c.id]
              ? html`<div class="rm-row-error">${this.shareError[c.id]}</div>`
              : nothing}
          </div>
        `,
      )}
    `;
  }
}
