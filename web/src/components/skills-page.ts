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
      <div class="h-full w-full overflow-y-auto px-6 py-6">
        <div class="max-w-3xl mx-auto">
          <div class="flex items-baseline justify-between mb-4">
            <div>
              <h1 class="text-[20px] font-semibold text-ink-0 dark:text-d-ink-0">
                Skills
              </h1>
              <p class="text-[13px] text-ink-3 dark:text-d-ink-3 mt-0.5">
                Tenant-wide catalog. Bind a skill to a coworker on the
                coworker detail page.
              </p>
            </div>
            <a
              href="#/skills/new"
              class="text-[12px] px-3 py-1.5 rounded-md bg-brand text-white
                hover:bg-brand-dark transition-colors"
            >+ New skill</a>
          </div>

          ${this.loading
            ? html`<div class="text-[13px] text-ink-3 dark:text-d-ink-3">Loading…</div>`
            : this.listError
              ? html`<div
                  class="border border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-900/20
                    text-red-700 dark:text-red-300 text-[13px] px-3 py-2 rounded-lg"
                >${this.listError}</div>`
              : this.rows.length === 0
                ? this.renderListEmpty()
                : this.renderRows()}
        </div>
      </div>
    `;
  }

  private renderListEmpty() {
    return html`
      <div
        class="border border-dashed border-surface-3 dark:border-d-surface-3
          rounded-xl px-6 py-10 text-center text-[13px] text-ink-2 dark:text-d-ink-2"
      >
        <p class="mb-1.5 font-medium text-ink-1 dark:text-d-ink-1">
          No skills yet
        </p>
        <p class="leading-relaxed">
          Click <strong>+ New skill</strong> to create your first one.
        </p>
      </div>
    `;
  }

  private renderRows() {
    // Same hover-icon pattern as coworkers / mcp-servers pages. The
    // row body remains a clickable anchor that takes the user to
    // detail; the action icons sit next to it and stopPropagation so
    // clicking them doesn't also navigate.
    return html`
      <style>
        rm-skills-page .row-acts {
          opacity: 0;
          transition: opacity 0.13s;
        }
        rm-skills-page .skill-row:hover .row-acts,
        rm-skills-page .skill-row:focus-within .row-acts {
          opacity: 1;
        }
        rm-skills-page .icon-btn {
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
        rm-skills-page .icon-btn:hover {
          background: var(--rm-surface-3);
          color: var(--rm-ink);
        }
        rm-skills-page .icon-btn.danger:hover {
          background: var(--rm-bad-subtle);
          color: var(--rm-bad);
        }
      </style>
      <ul class="divide-y divide-surface-3 dark:divide-d-surface-3 border border-surface-3 dark:border-d-surface-3 rounded-xl overflow-hidden">
        ${this.rows.map((r) => {
          const delErr = this.deleteError[r.id] || '';
          return html`
            <li class="skill-row flex items-center gap-3 px-4 py-3 hover:bg-surface-2 dark:hover:bg-d-surface-2" data-skill-id=${r.id}>
              <a
                href=${`#/skills/${encodeURIComponent(r.id)}`}
                class="min-w-0 flex-1 block"
              >
                <div class="flex items-baseline gap-2">
                  <div class="text-[14px] font-medium text-ink-0 dark:text-d-ink-0 truncate">
                    ${r.name}
                  </div>
                  ${r.enabled
                    ? nothing
                    : html`<span class="text-[10.5px] uppercase tracking-wide
                        px-1.5 py-0.5 rounded bg-surface-3 dark:bg-d-surface-3
                        text-ink-3 dark:text-d-ink-3">disabled</span>`}
                  <span class="text-[11.5px] text-ink-3 dark:text-d-ink-3 ml-auto">
                    ${r.bound_coworker_count} coworker(s)
                  </span>
                </div>
                ${r.description
                  ? html`<div class="text-[12px] text-ink-2 dark:text-d-ink-2 mt-0.5 truncate">
                      ${r.description}
                    </div>`
                  : nothing}
                ${delErr
                  ? html`<div class="text-[11.5px] text-red-600 dark:text-red-300 mt-1">${delErr}</div>`
                  : nothing}
              </a>
              <div class="row-acts flex items-center gap-1 shrink-0">
                <button
                  type="button"
                  class="icon-btn"
                  title="Edit skill"
                  data-testid="skill-edit"
                  @click=${(e: Event) => { e.preventDefault(); this.editSkill(r); }}
                >${iconPencil(15)}</button>
                <button
                  type="button"
                  class="icon-btn danger"
                  title="Delete skill"
                  data-testid="skill-delete"
                  @click=${(e: Event) => { e.preventDefault(); void this.deleteSkill(r); }}
                >${iconTrash(15)}</button>
              </div>
            </li>
          `;
        })}
      </ul>
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
