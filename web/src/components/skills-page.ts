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

import { SKILL_MANIFEST_NAME } from '../api/skill_constants.js';
import { ApiError, getApiClient } from '../api/client.js';
import type { SkillCreate, SkillSummary } from '../api/client.js';

import './skill-detail-page.js';
import { iconPencil, iconTrash } from './icons.js';

type Mode = 'list' | 'new' | 'detail';

interface ParsedHash {
  mode: Mode;
  skillId: string | null;
}

function parseHash(hash: string): ParsedHash {
  if (hash === '#/skills' || hash === '#/skills/') {
    return { mode: 'list', skillId: null };
  }
  if (hash === '#/skills/new') return { mode: 'new', skillId: null };
  const match = hash.match(/^#\/skills\/([^/]+)$/);
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
  @state() private busy = false;

  @state() private form = this.emptyForm();
  @state() private formError: string | null = null;
  /** Per-row delete error. Cleared on refresh. */
  @state() private deleteError: Record<string, string> = {};

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

  private emptyForm(): { name: string; skillMd: string } {
    return {
      name: '',
      skillMd:
        '---\n' +
        'name: \n' +
        'description: \n' +
        '---\n' +
        '# Workflow\n',
    };
  }

  private syncFromHash(): void {
    const { mode, skillId } = parseHash(location.hash);
    const switched =
      !this.synced || this.mode !== mode || this.skillId !== skillId;
    this.synced = true;
    this.mode = mode;
    this.skillId = skillId;
    if (!switched) return;
    this.listError = null;
    this.formError = null;
    if (mode === 'list') {
      void this.refreshList();
    } else if (mode === 'new') {
      this.form = this.emptyForm();
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
    // Editing piggybacks on the existing detail page (#/skills/:id),
    // which already has the SKILL.md / files editor wired up — no
    // need for a separate edit dialog at the list level.
    location.hash = `#/skills/${encodeURIComponent(row.id)}`;
  }

  private async deleteSkill(row: SkillSummary): Promise<void> {
    const ok = window.confirm(
      `Delete skill "${row.name}"?\n\n` +
        (row.bound_coworker_count > 0
          ? `${row.bound_coworker_count} coworker(s) currently bind this skill ` +
            'and will lose access. '
          : '') +
        'Cannot be undone.',
    );
    if (!ok) return;
    this.deleteError = { ...this.deleteError, [row.id]: '' };
    try {
      await this.api.deleteSkill(row.id);
      await this.refreshList();
    } catch (err) {
      this.deleteError = {
        ...this.deleteError,
        [row.id]: this.errMessage(err),
      };
    }
  }

  private async submitNew(): Promise<void> {
    const { name, skillMd } = this.form;
    if (!name.trim()) {
      this.formError = 'Name is required.';
      return;
    }
    this.busy = true;
    this.formError = null;
    const body: SkillCreate = {
      name: name.trim(),
      // openapi-typescript marks fields with a `default` as required.
      enabled: true,
      files: { [SKILL_MANIFEST_NAME]: skillMd },
    };
    try {
      const created = await this.api.createSkill(body);
      location.hash = `#/skills/${created.id}`;
    } catch (err) {
      this.formError = this.errMessage(err);
    } finally {
      this.busy = false;
    }
  }

  override render() {
    if (this.mode === 'detail' && this.skillId) {
      return html`<rm-skill-detail-page skill-id=${this.skillId}></rm-skill-detail-page>`;
    }
    if (this.mode === 'new') return this.renderNew();
    return this.renderList();
  }

  private renderList() {
    return html`
      <div class="rm-spane">
        <div class="rm-ch">
          <h2>Skills</h2>
          <a href="#/skills/new" class="rm-add" style="text-decoration: none;">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
              stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="M12 5v14M5 12h14"/>
            </svg>
            New skill
          </a>
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
      </div>
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
            <span class="rm-meta">${r.bound_coworker_count} coworker(s)</span>
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
                  void this.deleteSkill(r);
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

  private renderNew() {
    return html`
      <div class="h-full w-full overflow-y-auto px-6 py-6">
        <div class="max-w-2xl mx-auto">
          <div class="flex items-baseline justify-between mb-4">
            <h1 class="text-[20px] font-semibold text-ink-0 dark:text-d-ink-0">
              New skill
            </h1>
            <a
              href="#/skills"
              class="text-[12px] text-ink-3 dark:text-d-ink-3 hover:underline"
            >Cancel</a>
          </div>

          <label class="block text-[12px] text-ink-2 dark:text-d-ink-2 mb-3">
            <span class="block mb-1">Name</span>
            <input
              type="text"
              class="w-full text-[13px] px-3 py-1.5 rounded-md border border-surface-3 dark:border-d-surface-3
                bg-surface-1 dark:bg-d-surface-1"
              placeholder="e.g. code-review"
              .value=${this.form.name}
              @input=${(e: Event) =>
                (this.form = {
                  ...this.form,
                  name: (e.target as HTMLInputElement).value,
                })}
            />
            <span class="text-[11px] text-ink-3 dark:text-d-ink-3 block mt-1">
              Letters / digits / underscore / hyphen; must start with a
              letter; up to 64 characters.
            </span>
          </label>

          <label class="block text-[12px] text-ink-2 dark:text-d-ink-2 mb-3">
            <span class="block mb-1">${SKILL_MANIFEST_NAME}</span>
            <textarea
              rows="18"
              spellcheck="false"
              class="w-full text-[12.5px] px-3 py-2 rounded-md border border-surface-3 dark:border-d-surface-3
                bg-surface-1 dark:bg-d-surface-1 font-mono leading-relaxed"
              .value=${this.form.skillMd}
              @input=${(e: Event) =>
                (this.form = {
                  ...this.form,
                  skillMd: (e.target as HTMLTextAreaElement).value,
                })}
            ></textarea>
            <span class="text-[11px] text-ink-3 dark:text-d-ink-3 block mt-1">
              YAML frontmatter required. <strong>description</strong>
              must be at least 16 characters. Quote any value that
              looks like a YAML boolean (e.g.
              <code>name: "on"</code>).
            </span>
          </label>

          ${this.formError
            ? html`<div class="text-[12px] text-red-600 dark:text-red-300 mb-2">${this.formError}</div>`
            : nothing}

          <div class="flex items-center justify-end gap-2">
            <a
              href="#/skills"
              class="text-[12px] px-3 py-1.5 rounded-md border border-surface-3 dark:border-d-surface-3
                text-ink-2 dark:text-d-ink-2"
            >Cancel</a>
            <button
              type="button"
              class="text-[12px] px-3 py-1.5 rounded-md bg-brand text-white hover:bg-brand-dark
                disabled:opacity-60 disabled:cursor-not-allowed"
              ?disabled=${this.busy}
              @click=${() => void this.submitNew()}
            >Create</button>
          </div>
        </div>
      </div>
    `;
  }
}
