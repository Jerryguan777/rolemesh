// Skills page (#/skills, #/skills/new).
//
// Lists tenant catalog skills and hosts the "+ New skill" form. The
// per-skill detail editor (#/skills/:id) lives in
// `<rm-skill-detail-page>` — this component delegates rendering to
// that element when the hash points at a specific id. Anti-mirror:
// hash parsing stays in one place rather than split between sibling
// routes (the v1.1 hash router is intentionally not regex-aware).

import { LitElement, html, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';

import { ApiError, getApiClient } from '../api/client.js';
import type { SkillSummary } from '../api/client.js';

import './skill-detail-page.js';
import './skill-dialog.js';
import './confirm-dialog.js';
import { iconPencil, iconTrash } from './icons.js';

type Mode = 'list' | 'new' | 'detail';

interface ParsedHash {
  mode: Mode;
  skillId: string | null;
}

function parseHash(hash: string): ParsedHash {
  // Accept both v1.1 flat (`#/skills/...`) and v2 nested
  // (`#/manage/skills/...`) shapes. The router.ts redirect handler
  // normalizes the flat form to the nested one on hashchange, but
  // (a) editSkill / submitNew may write the v2 form directly to skip
  // the redirect bounce, and (b) the in-flight flat URL exists for
  // one tick before redirect — both should resolve to the same
  // mode here.
  const normalized = hash.startsWith('#/manage/skills')
    ? '#/skills' + hash.slice('#/manage/skills'.length)
    : hash;
  if (normalized === '#/skills' || normalized === '#/skills/') {
    return { mode: 'list', skillId: null };
  }
  if (normalized === '#/skills/new') return { mode: 'new', skillId: null };
  const match = normalized.match(/^#\/skills\/([^/]+)$/);
  if (match) return { mode: 'detail', skillId: decodeURIComponent(match[1]) };
  return { mode: 'list', skillId: null };
}

@customElement('rm-skills-page')
export class SkillsPage extends LitElement {
  @state() private mode: Mode = 'list';
  @state() private skillId: string | null = null;
  // Tracks whether we've done the initial syncFromHash. Without it
  // the "no change → skip refresh" optimization swallows the first
  // mount when the URL already points at the list (the default).
  private synced = false;
  @state() private rows: SkillSummary[] = [];
  @state() private loading = false;
  @state() private listError: string | null = null;
  /** Per-row delete error. Cleared on refresh. */
  @state() private deleteError: Record<string, string> = {};
  /** Dialog state. `editTarget` null = create flow; non-null = edit
   *  flow. v2-C replaced the route-based create page (`#/skills/new`)
   *  with this dialog to match the prototype layout. */
  @state() private dialogOpen = false;
  @state() private editTarget: SkillSummary | null = null;
  /** Active deletion target — drives the rm-confirm-dialog open state. */
  @state() private deleteTarget: SkillSummary | null = null;
  @state() private deleteInFlight = false;

  private readonly api = getApiClient();
  private readonly onHashChange = (): void => this.syncFromHash();

  protected override createRenderRoot() {
    return this;
  }

  override connectedCallback() {
    super.connectedCallback();
    window.addEventListener('hashchange', this.onHashChange);
    this.syncFromHash();
  }

  override disconnectedCallback() {
    super.disconnectedCallback();
    window.removeEventListener('hashchange', this.onHashChange);
  }

  private syncFromHash(): void {
    const { mode, skillId } = parseHash(location.hash);
    const switched =
      !this.synced || this.mode !== mode || this.skillId !== skillId;
    this.synced = true;
    // mode='new' from the URL is a legacy path — bounce it to the
    // list and open the dialog instead. Keeps bookmarked `#/skills/new`
    // links functional without a renderNew() page.
    if (mode === 'new') {
      this.mode = 'list';
      this.skillId = null;
      try {
        history.replaceState(null, '', `${location.pathname}${location.search}#/manage/skills`);
      } catch {
        // happy-dom or sandboxes can refuse cross-path replaceState;
        // the dialog still opens, just with a stale URL.
      }
      this.dialogOpen = true;
      this.editTarget = null;
      void this.refreshList();
      return;
    }
    this.mode = mode;
    this.skillId = skillId;
    if (!switched) return;
    this.listError = null;
    if (mode === 'list') {
      void this.refreshList();
    }
  }

  private errMessage(err: unknown): string {
    if (err instanceof ApiError) return err.body?.message ?? `${err.status}`;
    return (err as Error).message;
  }

  private async refreshList(): Promise<void> {
    this.loading = true;
    this.listError = null;
    this.deleteError = {};
    try {
      this.rows = await this.api.listSkills();
    } catch (err) {
      this.rows = [];
      this.listError = this.errMessage(err);
    } finally {
      this.loading = false;
    }
  }

  private editSkill(row: SkillSummary): void {
    // Edit reuses the unified <rm-skill-dialog>. The dialog fetches
    // the full Skill (with file contents) on open via api.getSkill.
    // Legacy bookmark links to `#/manage/skills/<id>` still resolve
    // to <rm-skill-detail-page> (advanced multi-file editor).
    this.editTarget = row;
    this.dialogOpen = true;
  }

  private openCreateDialog(): void {
    this.editTarget = null;
    this.dialogOpen = true;
  }

  private askDelete(row: SkillSummary): void {
    this.deleteTarget = row;
  }

  private cancelDelete = (): void => {
    if (this.deleteInFlight) return;
    this.deleteTarget = null;
  };

  private async performDelete(): Promise<void> {
    const row = this.deleteTarget;
    if (!row || this.deleteInFlight) return;
    this.deleteInFlight = true;
    this.deleteError = { ...this.deleteError, [row.id]: '' };
    try {
      await this.api.deleteSkill(row.id);
      this.deleteTarget = null;
      await this.refreshList();
    } catch (err) {
      this.deleteError = {
        ...this.deleteError,
        [row.id]: this.errMessage(err),
      };
      this.deleteTarget = null;
    } finally {
      this.deleteInFlight = false;
    }
  }

  override render() {
    if (this.mode === 'detail' && this.skillId) {
      return html`<rm-skill-detail-page skill-id=${this.skillId}></rm-skill-detail-page>`;
    }
    return this.renderList();
  }

  private renderList() {
    return html`
      <div class="rm-spane">
        <div class="rm-ch">
          <h2>Skills</h2>
          <button
            type="button"
            class="rm-add"
            @click=${this.openCreateDialog}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
              stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="M12 5v14M5 12h14"/>
            </svg>
            New skill
          </button>
        </div>
        <p class="rm-sub">
          Tenant-wide catalog. Bind a skill to a coworker on the
          coworker detail page.
        </p>

        ${this.loading
          ? html`<div class="rm-banner-loading">Loading…</div>`
          : this.listError
            ? html`<div class="rm-banner-err">${this.listError}</div>`
            : this.rows.length === 0
              ? this.renderListEmpty()
              : this.renderRows()}

        <rm-skill-dialog
          ?open=${this.dialogOpen}
          .editing=${this.editTarget}
          @close=${() => {
            this.dialogOpen = false;
            this.editTarget = null;
          }}
          @skill-created=${() => { void this.refreshList(); }}
          @skill-updated=${() => { void this.refreshList(); }}
        ></rm-skill-dialog>
        ${this.renderDeleteDialog()}
      </div>
    `;
  }

  private renderDeleteDialog() {
    const target = this.deleteTarget;
    const bindCount = target?.bound_coworker_count ?? 0;
    return html`
      <rm-confirm-dialog
        title="Delete skill?"
        ?open=${target !== null}
        tone="danger"
        confirm-label="Delete"
        busy-label="Deleting…"
        ?busy=${this.deleteInFlight}
        data-testid="confirm-delete-dialog"
        @cancel=${this.cancelDelete}
        @confirm=${() => void this.performDelete()}
      >
        ${target
          ? html`
              <p style="margin: 0 0 12px;">
                Delete skill <strong>${target.name}</strong>?
              </p>
              <p style="margin: 0; color: var(--rm-ink-2); font-size: var(--rm-text-sm);">
                ${bindCount > 0
                  ? html`${bindCount} coworker(s) currently bind this
                    skill and will lose access. `
                  : nothing}Cannot be undone.
              </p>
            `
          : nothing}
      </rm-confirm-dialog>
    `;
  }

  private renderListEmpty() {
    return html`
      <div class="rm-empty">
        <span class="rm-empty-title">No skills yet</span>
        Click <b>+ New skill</b> above to create your first one.
      </div>
    `;
  }

  private renderRows() {
    return html`
      ${this.rows.map((r) => {
        const delErr = this.deleteError[r.id] || '';
        const initial = (r.name?.[0] ?? '?').toUpperCase();
        return html`
          <div
            class="rm-card"
            data-skill-id=${r.id}
            style="cursor: pointer;"
            role="link"
            tabindex="0"
            @click=${() => this.editSkill(r)}
          >
            <span class="rm-ic">${initial}</span>
            <span class="rm-mn">
              <b>${r.name}</b>
              <span>${r.description ?? '—'}</span>
            </span>
            <span class="rm-meta"
              >${r.bound_coworker_count} coworker${r.bound_coworker_count === 1 ? '' : 's'}</span>
            ${r.enabled
              ? nothing
              : html`<span class="rm-pill rm-pill-off">disabled</span>`}
            <span class="rm-row-acts">
              <button
                type="button"
                class="rm-iconbtn"
                title="Edit skill"
                data-testid="skill-edit"
                @click=${(e: Event) => {
                  e.stopPropagation();
                  this.editSkill(r);
                }}
              >${iconPencil(15)}</button>
              <button
                type="button"
                class="rm-iconbtn rm-iconbtn--danger"
                title="Delete skill"
                data-testid="skill-delete"
                @click=${(e: Event) => {
                  e.stopPropagation();
                  this.askDelete(r);
                }}
              >${iconTrash(15)}</button>
            </span>
            ${delErr
              ? html`<div class="rm-row-error">${delErr}</div>`
              : nothing}
          </div>
        `;
      })}
    `;
  }

}
