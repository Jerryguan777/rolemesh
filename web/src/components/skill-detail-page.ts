// Per-skill detail editor (#/skills/:id).
//
// Two-column layout (design §6.3 G): file tree on the left, textarea
// editor on the right, plus a header with enabled-toggle / Save /
// Delete. Lives in its own component (spec calls for a separate
// `<rm-skill-detail-page>`) so the list page above stays small and
// the detail page can be reused if a future host wants to embed it
// modally.
//
// Editor choice: textarea, per 03b prompt Open Q 1 (don't pull in
// monaco/codemirror for a v1.1 polish — defer the dep).

import { LitElement, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';

import {
  SKILL_FILE_PATH_RE,
  SKILL_MANIFEST_NAME,
  isValidSkillFilePath,
} from '../api/skill_constants.js';
import { ApiError, getApiClient } from '../api/client.js';
import type { Skill } from '../api/client.js';
import './confirm-dialog.js';

@customElement('rm-skill-detail-page')
export class SkillDetailPage extends LitElement {
  @property({ type: String, attribute: 'skill-id' })
  skillId = '';

  @state() private detail: Skill | null = null;
  @state() private loading = false;
  @state() private detailError: string | null = null;
  @state() private busy = false;

  @state() private activeFile: string | null = null;
  @state() private fileEdits: Record<string, string> = {};
  @state() private metaEnabled = true;
  @state() private newFilePath = '';
  @state() private newFileError: string | null = null;
  @state() private saveError: string | null = null;
  @state() private deleteError: string | null = null;
  /** When true, the skill-level "Delete skill" confirmation is up. */
  @state() private deleteSkillOpen = false;
  /** Per-file delete confirm. Holds the path of the file the user
   *  asked to delete; null = no file-delete dialog open. */
  @state() private deleteFilePath: string | null = null;
  @state() private deleteFileBusy = false;

  private readonly api = getApiClient();

  protected override createRenderRoot() {
    return this;
  }

  override connectedCallback() {
    super.connectedCallback();
    if (this.skillId) void this.load();
  }

  override updated(changed: Map<string, unknown>) {
    if (changed.has('skillId') && this.skillId) {
      void this.load();
    }
  }

  private errMessage(err: unknown): string {
    if (err instanceof ApiError) {
      if (err.status === 409 && err.body?.details) {
        const ids = (err.body.details as Record<string, unknown>).coworker_ids;
        if (Array.isArray(ids)) {
          return `Skill is in use by ${ids.length} coworker(s); unbind them before deleting.`;
        }
      }
      return err.body?.message ?? `${err.status}`;
    }
    return (err as Error).message;
  }

  private async load(): Promise<void> {
    this.loading = true;
    this.detailError = null;
    this.fileEdits = {};
    this.activeFile = null;
    try {
      this.detail = await this.api.getSkill(this.skillId);
      this.metaEnabled = this.detail.enabled;
      const files = this.detail.files ?? {};
      if (SKILL_MANIFEST_NAME in files) {
        this.activeFile = SKILL_MANIFEST_NAME;
      } else {
        const first = Object.keys(files).sort()[0];
        this.activeFile = first ?? null;
      }
    } catch (err) {
      this.detail = null;
      this.detailError = this.errMessage(err);
    } finally {
      this.loading = false;
    }
  }

  private contentForFile(path: string): string {
    if (path in this.fileEdits) return this.fileEdits[path];
    return this.detail?.files?.[path]?.content ?? '';
  }

  private onFileEdit(path: string, value: string): void {
    this.fileEdits = { ...this.fileEdits, [path]: value };
  }

  private hasUnsavedEdits(): boolean {
    if (Object.keys(this.fileEdits).length > 0) return true;
    return this.detail?.enabled !== this.metaEnabled;
  }

  private async saveAll(): Promise<void> {
    if (!this.detail) return;
    this.busy = true;
    this.saveError = null;
    try {
      for (const [path, content] of Object.entries(this.fileEdits)) {
        await this.api.putSkillFile(this.detail.id, path, {
          content,
          mime_type: this.detail.files?.[path]?.mime_type ?? 'text/plain',
        });
      }
      if (this.metaEnabled !== this.detail.enabled) {
        await this.api.updateSkill(this.detail.id, {
          enabled: this.metaEnabled,
        });
      }
      await this.load();
    } catch (err) {
      this.saveError = this.errMessage(err);
    } finally {
      this.busy = false;
    }
  }

  private askDeleteSkill = (): void => {
    if (!this.detail) return;
    this.deleteSkillOpen = true;
  };

  private cancelDeleteSkill = (): void => {
    if (this.busy) return;
    this.deleteSkillOpen = false;
  };

  private async performDeleteSkill(): Promise<void> {
    if (!this.detail || this.busy) return;
    this.busy = true;
    this.deleteError = null;
    try {
      await this.api.deleteSkill(this.detail.id);
      this.deleteSkillOpen = false;
      location.hash = '#/manage/skills';
    } catch (err) {
      this.deleteError = this.errMessage(err);
      this.deleteSkillOpen = false;
    } finally {
      this.busy = false;
    }
  }

  private addNewFile(): void {
    if (!this.detail) return;
    this.newFileError = null;
    const path = this.newFilePath.trim();
    if (!path) {
      this.newFileError = 'Enter a file path.';
      return;
    }
    if (!isValidSkillFilePath(path)) {
      this.newFileError =
        'Invalid path. Only [A-Za-z0-9_.-] segments; no traversal.';
      return;
    }
    const serverFiles = this.detail.files ?? {};
    if (path in serverFiles || path in this.fileEdits) {
      this.newFileError = 'File already exists.';
      return;
    }
    if (path === SKILL_MANIFEST_NAME) {
      this.newFileError = 'SKILL.md already exists.';
      return;
    }
    this.fileEdits = { ...this.fileEdits, [path]: '' };
    this.activeFile = path;
    this.newFilePath = '';
  }

  private askDeleteFile(path: string): void {
    if (!this.detail) return;
    if (path === SKILL_MANIFEST_NAME) return;
    this.deleteFilePath = path;
  }

  private cancelDeleteFile = (): void => {
    if (this.deleteFileBusy) return;
    this.deleteFilePath = null;
  };

  private async performDeleteFile(): Promise<void> {
    const path = this.deleteFilePath;
    if (!path) return;
    this.deleteFileBusy = true;
    try {
      await this.deleteFile(path);
    } finally {
      this.deleteFileBusy = false;
      this.deleteFilePath = null;
    }
  }

  private async deleteFile(path: string): Promise<void> {
    if (!this.detail) return;
    if (path === SKILL_MANIFEST_NAME) return;
    const serverFiles = this.detail.files ?? {};
    if (!(path in serverFiles)) {
      const next = { ...this.fileEdits };
      delete next[path];
      this.fileEdits = next;
      if (this.activeFile === path) {
        this.activeFile = SKILL_MANIFEST_NAME in serverFiles
          ? SKILL_MANIFEST_NAME : null;
      }
      return;
    }
    try {
      await this.api.deleteSkillFile(this.detail.id, path);
      await this.load();
    } catch (err) {
      this.saveError = this.errMessage(err);
    }
  }

  override render() {
    if (this.loading && !this.detail) {
      return html`<div class="p-6 text-[13px] text-ink-3 dark:text-d-ink-3">Loading…</div>`;
    }
    if (this.detailError) {
      return html`
        <div class="p-6 text-[13px]">
          <a href="#/skills" class="text-brand hover:underline">← Back to skills</a>
          <div class="mt-3 border border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-900/20
            text-red-700 dark:text-red-300 px-3 py-2 rounded-lg">
            ${this.detailError}
          </div>
        </div>
      `;
    }
    if (!this.detail) return html`<div></div>`;
    const s = this.detail;
    const allPaths = Array.from(
      new Set([...Object.keys(s.files ?? {}), ...Object.keys(this.fileEdits)]),
    ).sort();
    return html`
      <div class="h-full w-full overflow-hidden flex flex-col">
        ${this.renderHeader(s)}
        <div class="flex-1 min-h-0 grid grid-cols-[260px_1fr] gap-0 border-t border-surface-3 dark:border-d-surface-3">
          ${this.renderFileTree(allPaths)}
          ${this.renderEditor()}
        </div>
        ${this.renderConfirmDialogs(s)}
      </div>
    `;
  }

  private renderConfirmDialogs(s: Skill) {
    return html`
      <rm-confirm-dialog
        title="Delete skill?"
        ?open=${this.deleteSkillOpen}
        tone="danger"
        confirm-label="Delete"
        busy-label="Deleting…"
        ?busy=${this.busy}
        data-testid="confirm-delete-skill-dialog"
        @cancel=${this.cancelDeleteSkill}
        @confirm=${() => void this.performDeleteSkill()}
      >
        <p style="margin: 0 0 12px;">
          Delete skill <strong>${s.name}</strong>?
        </p>
        <p style="margin: 0; color: var(--rm-ink-2); font-size: var(--rm-text-sm);">
          This cannot be undone.
        </p>
      </rm-confirm-dialog>
      <rm-confirm-dialog
        title="Delete file?"
        ?open=${this.deleteFilePath !== null}
        tone="danger"
        confirm-label="Delete"
        busy-label="Deleting…"
        ?busy=${this.deleteFileBusy}
        data-testid="confirm-delete-file-dialog"
        @cancel=${this.cancelDeleteFile}
        @confirm=${() => void this.performDeleteFile()}
      >
        ${this.deleteFilePath
          ? html`
              <p style="margin: 0;">
                Delete file
                <strong>${this.deleteFilePath}</strong>?
              </p>
            `
          : nothing}
      </rm-confirm-dialog>
    `;
  }

  private renderHeader(s: Skill) {
    return html`
      <div class="px-6 py-4 flex items-start gap-4">
        <div class="min-w-0 flex-1">
          <a href="#/skills" class="text-[12px] text-ink-3 dark:text-d-ink-3 hover:underline">
            ← Skills
          </a>
          <h1 class="text-[20px] font-semibold text-ink-0 dark:text-d-ink-0 mt-1 truncate">
            ${s.name}
          </h1>
        </div>
        <label class="flex items-center gap-2 text-[12px] text-ink-2 dark:text-d-ink-2">
          <input
            type="checkbox"
            .checked=${this.metaEnabled}
            @change=${(e: Event) =>
              (this.metaEnabled = (e.target as HTMLInputElement).checked)}
          />
          Enabled (catalog)
        </label>
        <button
          type="button"
          class="rm-btn rm-btn--primary"
          ?disabled=${this.busy || !this.hasUnsavedEdits()}
          @click=${() => void this.saveAll()}
        >Save</button>
        <button
          type="button"
          class="rm-btn rm-btn--danger"
          ?disabled=${this.busy}
          @click=${this.askDeleteSkill}
        >Delete</button>
      </div>
      ${this.saveError
        ? html`<div class="mx-6 mb-2 text-[12px] text-red-600 dark:text-red-300">${this.saveError}</div>`
        : nothing}
      ${this.deleteError
        ? html`<div class="mx-6 mb-2 text-[12px] text-red-600 dark:text-red-300">${this.deleteError}</div>`
        : nothing}
    `;
  }

  private renderFileTree(allPaths: string[]) {
    return html`
      <aside class="border-r border-surface-3 dark:border-d-surface-3 overflow-y-auto">
        <ul class="text-[12.5px]">
          ${allPaths.map((p) => {
            const isManifest = p === SKILL_MANIFEST_NAME;
            const unsaved = p in this.fileEdits;
            const active = this.activeFile === p;
            return html`
              <li class="flex items-center group">
                <button
                  type="button"
                  class=${`flex-1 text-left px-3 py-1.5 truncate
                    ${active ? 'bg-surface-2 dark:bg-d-surface-2 text-ink-0 dark:text-d-ink-0'
                      : 'text-ink-1 dark:text-d-ink-1 hover:bg-surface-2 dark:hover:bg-d-surface-2'}`}
                  @click=${() => (this.activeFile = p)}
                >
                  ${unsaved ? html`<span class="text-amber-600 mr-1">●</span>` : nothing}${p}
                </button>
                <button
                  type="button"
                  class=${`px-2 py-1 text-[11px]
                    ${isManifest ? 'text-ink-4 dark:text-d-ink-4 cursor-not-allowed'
                      : 'text-ink-3 hover:text-red-600 dark:text-d-ink-3 dark:hover:text-red-300 cursor-pointer'}`}
                  title=${isManifest ? `${SKILL_MANIFEST_NAME} is protected (server returns 409)` : 'Delete file'}
                  ?disabled=${isManifest}
                  @click=${() => this.askDeleteFile(p)}
                >×</button>
              </li>
            `;
          })}
        </ul>
        <div class="border-t border-surface-3 dark:border-d-surface-3 px-3 py-2">
          <label class="text-[11px] text-ink-3 dark:text-d-ink-3 block mb-1">New file</label>
          <div class="flex gap-1">
            <input
              type="text"
              class="flex-1 min-w-0 text-[11.5px] px-2 py-1 rounded border border-surface-3 dark:border-d-surface-3
                bg-surface-1 dark:bg-d-surface-1"
              placeholder="notes.md"
              .value=${this.newFilePath}
              pattern=${SKILL_FILE_PATH_RE.source}
              @input=${(e: Event) =>
                (this.newFilePath = (e.target as HTMLInputElement).value)}
              @keydown=${(e: KeyboardEvent) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  this.addNewFile();
                }
              }}
            />
            <button
              type="button"
              class="text-[11.5px] px-2 py-1 rounded bg-brand text-white hover:bg-brand-dark"
              @click=${() => this.addNewFile()}
            >Add</button>
          </div>
          ${this.newFileError
            ? html`<div class="text-[11px] text-red-600 dark:text-red-300 mt-1">${this.newFileError}</div>`
            : nothing}
        </div>
      </aside>
    `;
  }

  private renderEditor() {
    if (this.activeFile === null) {
      return html`<div class="p-6 text-[13px] text-ink-3 dark:text-d-ink-3">
        No file selected.
      </div>`;
    }
    const path = this.activeFile;
    const content = this.contentForFile(path);
    return html`
      <div class="flex flex-col min-h-0">
        <div class="px-4 py-2 border-b border-surface-3 dark:border-d-surface-3
          text-[12px] text-ink-3 dark:text-d-ink-3 flex items-center gap-2">
          <span class="font-mono">${path}</span>
          ${path === SKILL_MANIFEST_NAME
            ? html`<span class="text-[10.5px] uppercase tracking-wide px-1.5 py-0.5 rounded
                bg-surface-3 dark:bg-d-surface-3 text-ink-3 dark:text-d-ink-3">
                manifest (protected)
              </span>`
            : nothing}
        </div>
        <textarea
          class="flex-1 min-h-0 w-full px-4 py-3 text-[12.5px] font-mono leading-relaxed
            bg-surface-1 dark:bg-d-surface-1 outline-none resize-none"
          spellcheck="false"
          .value=${content}
          @input=${(e: Event) =>
            this.onFileEdit(path, (e.target as HTMLTextAreaElement).value)}
        ></textarea>
      </div>
    `;
  }
}
