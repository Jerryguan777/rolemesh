// Skills page (#/skills, #/skills/new).
//
// Lists tenant catalog skills and hosts the "+ New skill" form. The
// per-skill detail editor (#/skills/:id) lives in
// `<rm-skill-detail-page>` — this component delegates rendering to
// that element when the hash points at a specific id. Anti-mirror:
// hash parsing stays in one place rather than split between sibling
// routes (the v1.1 hash router is intentionally not regex-aware).
//
// Role-aware affordances (RBAC UI PR5, spec §5.2 / §7.3 / §7.4) mirror
// the coworkers page (PR4):
//   * "+ New skill" gated on the `skill.create` capability.
//   * Filter chips (All visible / Mine / Shared by others) re-classify
//     the ALREADY-server-filtered list — UX only, NOT a security gate
//     (the backend visibility-filters GET /skills; we render as-is).
//   * Per-row Edit / Delete / Share gated by `canManage(skill,
//     'skill.manage')` — the ownership escape. Rows a member can't
//     manage show a "View only" hint instead of dead buttons.
//   * "Created by you" accent cue on own rows; green "Shared" / gray
//     "Private" pill from the wire `visibility`.

import { LitElement, html, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';

import { ApiError, getApiClient } from '../api/client.js';
import type { Skill, SkillSummary } from '../api/client.js';
import {
  canManage,
  hasCapability,
  isOwnResource,
} from '../auth/capabilities.js';

import './skill-detail-page.js';
import './skill-dialog.js';
import './confirm-dialog.js';
import { iconPencil, iconTrash, iconUsers } from './icons.js';

type Mode = 'list' | 'new' | 'detail';

/** UX-only view filter over the already-server-filtered list. NOT a
 *  security boundary — the backend already visibility-filtered the rows
 *  (spec §7.3). These chips just re-narrow what's shown for the user's
 *  workflow. Mirrors the coworkers page (PR4) `CoworkerChip`. */
export type SkillChip = 'all' | 'mine' | 'shared';

/** Classify one skill row against a chip selection. Pure; mirrors the
 *  coworkers page (PR4) `matchesChip` but typed to `SkillSummary` so the
 *  classification stays structural (visibility + ownership). Three-value
 *  safe: a row with a null `created_by_user_id` is never "mine" (it falls
 *  through `isOwnResource`, which returns false for null) and is only kept
 *  by the "shared" chip when its `visibility` says so — never by
 *  ownership. */
export function matchesChip(skill: SkillSummary, chip: SkillChip): boolean {
  if (chip === 'all') return true;
  if (chip === 'mine') return isOwnResource(skill);
  // 'shared' — others' shared rows only (own rows belong under "Mine").
  return skill.visibility === 'shared' && !isOwnResource(skill);
}

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
  /** Client-side view filter (spec §5.2 chips). Pure presentation — the
   *  list is already server-side visibility-filtered; this re-narrows it. */
  @state() private chip: SkillChip = 'all';
  /** Per-row share/unshare error, keyed by skill id. Cleared on the next
   *  refresh, same lifecycle as `deleteError`. */
  @state() private shareError: Record<string, string> = {};
  /** Ids with a share/unshare POST in flight — disables that row's
   *  toggle so a double-click can't fire two visibility flips. */
  @state() private shareInFlight: Set<string> = new Set();
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
    this.shareError = {};
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

  /** Flip a skill's visibility. `canManage` is the ONLY gate (spec §7.4 —
   *  no separate `*.share` capability); the toggle only renders where
   *  canManage is true, and the backend re-checks the ownership escape.
   *  The share endpoints return the FULL `Skill`, which lacks the
   *  `description` / `bound_coworker_count` fields the list `SkillSummary`
   *  carries — so we PATCH only `visibility` (and keep the row's
   *  `created_by_user_id`) onto the existing row rather than replacing it,
   *  which would blank those summary-only columns. (This is the one place
   *  PR5 cannot do a whole-row swap like PR4's Coworker share.) */
  private async toggleShare(row: SkillSummary): Promise<void> {
    if (this.shareInFlight.has(row.id)) return;
    this.shareInFlight = new Set(this.shareInFlight).add(row.id);
    this.shareError = { ...this.shareError, [row.id]: '' };
    try {
      const updated: Skill =
        row.visibility === 'shared'
          ? await this.api.unshareSkill(row.id)
          : await this.api.shareSkill(row.id);
      this.rows = this.rows.map((r) =>
        r.id === updated.id
          ? {
              ...r,
              visibility: updated.visibility,
              created_by_user_id: updated.created_by_user_id,
            }
          : r,
      );
    } catch (err) {
      this.shareError = {
        ...this.shareError,
        [row.id]: this.errMessage(err),
      };
    } finally {
      const next = new Set(this.shareInFlight);
      next.delete(row.id);
      this.shareInFlight = next;
    }
  }

  override render() {
    if (this.mode === 'detail' && this.skillId) {
      return html`<rm-skill-detail-page skill-id=${this.skillId}></rm-skill-detail-page>`;
    }
    return this.renderList();
  }

  private renderList() {
    const canCreate = hasCapability('skill.create');
    const visibleRows = this.rows.filter((r) => matchesChip(r, this.chip));
    return html`
      <div class="rm-spane">
        <div class="rm-ch">
          <h2>Skills</h2>
          ${canCreate
            ? html`<button
                type="button"
                class="rm-add"
                data-testid="skill-new"
                @click=${this.openCreateDialog}
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
                  stroke="currentColor" stroke-width="2" aria-hidden="true">
                  <path d="M12 5v14M5 12h14"/>
                </svg>
                New skill
              </button>`
            : nothing}
        </div>
        <p class="rm-sub">
          Tenant-wide catalog. Bind a skill to a coworker on the
          coworker detail page.
        </p>

        ${this.loading || this.listError || this.rows.length === 0
          ? nothing
          : this.renderChips()}

        ${this.loading
          ? html`<div class="rm-banner-loading">Loading…</div>`
          : this.listError
            ? html`<div class="rm-banner-err">${this.listError}</div>`
            : visibleRows.length === 0
              ? this.renderListEmpty()
              : this.renderRows(visibleRows)}

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

  private renderChips() {
    const chips: Array<[SkillChip, string]> = [
      ['all', 'All visible'],
      ['mine', 'Mine'],
      ['shared', 'Shared by others'],
    ];
    return html`
      <div class="rm-seg rm-chipbar" role="tablist" aria-label="Filter skills">
        ${chips.map(
          ([key, label]) => html`
            <button
              type="button"
              role="tab"
              class=${this.chip === key ? 'rm-seg--on' : ''}
              aria-selected=${this.chip === key ? 'true' : 'false'}
              data-testid="skill-chip-${key}"
              @click=${() => { this.chip = key; }}
            >${label}</button>
          `,
        )}
      </div>
    `;
  }

  private renderDeleteDialog() {
    const target = this.deleteTarget;
    const bindCount = target?.bound_coworker_count ?? 0;
    const blocked = bindCount > 0;
    return html`
      <rm-confirm-dialog
        title=${target ? `Delete skill "${target.name}"?` : 'Delete skill?'}
        ?open=${target !== null}
        tone="danger"
        confirm-label="Delete"
        busy-label="Deleting…"
        ?busy=${this.deleteInFlight}
        ?disable-confirm=${blocked}
        data-testid="confirm-delete-dialog"
        @cancel=${this.cancelDelete}
        @confirm=${() => void this.performDelete()}
      >
        ${blocked
          ? html`<p style="margin: 0;">
              This skill is bound to ${bindCount}
              coworker${bindCount === 1 ? '' : 's'}. Unbind it from
              ${bindCount === 1 ? 'that coworker' : 'each one'} before
              deleting.
            </p>`
          : html`<p style="margin: 0;">This cannot be undone.</p>`}
      </rm-confirm-dialog>
    `;
  }

  /** Empty-state copy varies by the active chip AND by whether the user
   *  can manage skills tenant-wide vs is a plain member (spec §5.2,
   *  mirroring §5.1). Gated on a CAPABILITY (`skill.manage`), never a role
   *  name — a member's next action is "ask an admin to share / create your
   *  own", a manager's is "create the first one". */
  private renderListEmpty() {
    const isManager = hasCapability('skill.manage');
    let title: string;
    let body: unknown;
    if (this.chip === 'mine') {
      title = 'Nothing here yet';
      body = html`You haven’t created any skills yet.`;
    } else if (this.chip === 'shared') {
      title = 'Nothing shared';
      body = isManager
        ? html`No skills shared by others.`
        : html`No skills have been shared with you yet.`;
    } else {
      // 'all'
      title = 'No skills yet';
      body = isManager
        ? html`No skills in this tenant yet. Click
            <b>+ New skill</b> above to create the first one.`
        : html`No skills yet. Create one with
            <b>+ New skill</b>, or ask your admin to share theirs.`;
    }
    return html`
      <div class="rm-empty" data-testid="skill-empty">
        <span class="rm-empty-title">${title}</span>
        ${body}
      </div>
    `;
  }

  /** Ownership cue (spec §5.2, mirroring §5.1). There is NO owner
   *  display-name on the wire — only `created_by_user_id` — so own rows
   *  read "Created by you" in accent; everything else is a neutral label
   *  (never an invented person name). */
  private renderOwnTag(r: SkillSummary) {
    if (isOwnResource(r)) {
      return html`<span
        class="rm-own-tag rm-own-tag--mine"
        data-testid="skill-own-tag"
      >Created by you</span>`;
    }
    return html`<span
      class="rm-own-tag rm-own-tag--other"
      data-testid="skill-own-tag"
    >Shared by another member</span>`;
  }

  /** Visibility pill — green "Shared" / gray "Private" from the wire
   *  `visibility` (spec §7.4). */
  private renderVisibilityPill(r: SkillSummary) {
    const shared = r.visibility === 'shared';
    return html`<span
      class=${shared ? 'rm-pill rm-pill-on' : 'rm-pill rm-pill-off'}
      data-testid="skill-visibility"
    >${shared ? 'Shared' : 'Private'}</span>`;
  }

  private renderRowActions(r: SkillSummary) {
    // Single gate for ALL management affordances — the ownership escape
    // (manage capability OR own the row), mirroring the backend. Members
    // see a "view only" hint instead of dead Edit/Delete/Share buttons.
    if (!canManage(r, 'skill.manage')) {
      return html`<span class="rm-viewonly" data-testid="skill-viewonly"
        >View only</span
      >`;
    }
    const shared = r.visibility === 'shared';
    const busy = this.shareInFlight.has(r.id);
    return html`
      <span class="rm-row-acts">
        <button
          type="button"
          class=${shared ? 'rm-iconbtn rm-iconbtn--on' : 'rm-iconbtn'}
          title=${shared
            ? 'Make private'
            : `Share with everyone in ${r.tenant_id}`}
          data-testid="skill-share"
          ?disabled=${busy}
          aria-pressed=${shared ? 'true' : 'false'}
          @click=${(e: Event) => {
            e.stopPropagation();
            void this.toggleShare(r);
          }}
        >${iconUsers(15)}</button>
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
    `;
  }

  private renderRows(rows: SkillSummary[]) {
    return html`
      ${rows.map((r) => {
        const delErr = this.deleteError[r.id] || '';
        const shareErr = this.shareError[r.id] || '';
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
              <span>
                ${r.description ?? '—'} · ${this.renderOwnTag(r)}
              </span>
            </span>
            <span class="rm-meta"
              >${r.bound_coworker_count} coworker${r.bound_coworker_count === 1 ? '' : 's'}</span>
            ${this.renderVisibilityPill(r)}
            ${r.enabled
              ? nothing
              : html`<span class="rm-pill rm-pill-off">disabled</span>`}
            ${this.renderRowActions(r)}
            ${delErr
              ? html`<div class="rm-row-error">${delErr}</div>`
              : nothing}
            ${shareErr
              ? html`<div class="rm-row-error">${shareErr}</div>`
              : nothing}
          </div>
        `;
      })}
    `;
  }

}
