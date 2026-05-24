// <rm-skill-dialog> — unified create + edit dialog for skills.
//
// Matches the v2 prototype (lines 680-695):
//
//   ┌─ New skill ──────────────────────────────────────×┐
//   │  A skill is a package of instructions and files. │
//   │  Name        [ competitor-analysis           ]   │
//   │  Description [ What this skill does, one line]   │
//   │  SKILL.md    [ ---                            ]  │
//   │  (main file) [ name: …                        ]  │
//   │              [ description: …                 ]  │
//   │              [ ---                            ]  │
//   │              [ # When to use…                 ]  │
//   │  Additional [ 📄 reference.md         ×      ]   │
//   │  files      [ + Add file                      ]  │
//   │                                                  │
//   │                       [ Cancel ] [ Save skill ]  │
//   └──────────────────────────────────────────────────┘
//
// Why prototype's "Name + Description + SKILL.md textarea" trio (the
// inputs overlap with the textarea's frontmatter):
//
// * Name maps to `skills.name` (DB column, not frontmatter).
// * Description maps to the `description:` line in SKILL.md
//   frontmatter — kept as a separate input for fast scanning;
//   backend computes `SkillSummary.description` from the same line.
// * The SKILL.md textarea here is just the BODY (post-frontmatter).
//   On save we re-assemble: `---\nname: X\ndescription: Y\n---\n{body}`.
//   On load we split the existing raw SKILL.md the same way.
//
// Additional files: prototype only collects filenames (no per-file
// content editing in the dialog). We follow that for now — created
// files seed with empty content; full multi-file editing stays on
// the legacy <rm-skill-detail-page> via bookmark URL. If a user
// needs richer multi-file workflows the v3 skill editor surfaces it.

import { LitElement, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';

import './dialog.js';
import { ApiError, getApiClient } from '../api/client.js';
import type {
  Skill,
  SkillCreate,
  SkillSummary,
  SkillUpdate,
} from '../api/client.js';
import {
  SKILL_MANIFEST_NAME,
  isValidSkillFilePath,
} from '../api/skill_constants.js';
import { iconTrash } from './icons.js';

/** Parse a SKILL.md blob into its description (from frontmatter)
 *  and the body that follows. Liberal grammar: leading whitespace
 *  tolerated, `---` delimiters optional. Anything we can't parse
 *  shows up as the body with an empty description. */
export function parseSkillMd(raw: string): {
  description: string;
  body: string;
} {
  const trimmed = raw.trimStart();
  if (!trimmed.startsWith('---')) {
    return { description: '', body: raw };
  }
  // Find the closing `---` on its own line.
  const afterOpen = trimmed.slice(3); // skip leading ---
  const closeIdx = afterOpen.search(/(^|\n)---\s*(\n|$)/);
  if (closeIdx === -1) {
    return { description: '', body: raw };
  }
  const fm = afterOpen.slice(0, closeIdx);
  // Strip the trailing closing fence + any leading newline of the body.
  const restStart = afterOpen.indexOf('---', closeIdx) + 3;
  const body = afterOpen.slice(restStart).replace(/^\n/, '');
  // Pull description out of the YAML-ish frontmatter. A single line
  // `description: …` is the only field we care about; multi-line
  // values are uncommon and left to the SKILL.md body experience.
  const descMatch = fm.match(/(^|\n)description:\s*(.*)/);
  const description = descMatch ? descMatch[2].trim() : '';
  return { description, body };
}

/** Re-assemble a SKILL.md blob from the dialog's 3 inputs. */
export function serializeSkillMd(
  name: string,
  description: string,
  body: string,
): string {
  // Escape characters that would break a single-line YAML value. We
  // only need to guard the colon-and-newline case; everything else
  // is benign at this freeform level.
  const safeDescription = description.replace(/\n/g, ' ').trim();
  // Strip any existing leading frontmatter from `body` to avoid
  // double-wrapping if the user pasted raw markdown back into the
  // textarea.
  const cleanBody = body.replace(/^---[\s\S]*?\n---\n?/, '').replace(/^\n+/, '');
  return (
    `---\nname: ${name}\ndescription: ${safeDescription}\n---\n${cleanBody}`
  );
}

interface ExtraFile {
  /** path relative to skill root, e.g. "reference.md". */
  path: string;
  /** content read from the server on edit-mode open; new files
   *  default to empty. */
  content: string;
}

@customElement('rm-skill-dialog')
export class SkillDialog extends LitElement {
  @property({ type: Boolean }) open = false;
  /** When set, the dialog runs in edit mode. Caller passes the
   *  summary row from the list (id + name + bound_coworker_count);
   *  the dialog itself fetches the full `Skill` (with files) when
   *  the open transition fires. */
  @property({ attribute: false }) editing: SkillSummary | null = null;

  @state() private name = '';
  @state() private description = '';
  @state() private body = '';
  @state() private extraFiles: ExtraFile[] = [];
  @state() private busy = false;
  @state() private err: string | null = null;
  @state() private fileErr: string | null = null;
  @state() private loadingDetail = false;

  private readonly api = getApiClient();

  protected override createRenderRoot() {
    return this;
  }

  override willUpdate(changed: Map<string, unknown>) {
    if (changed.has('open') && this.open) {
      this.err = null;
      this.fileErr = null;
      this.busy = false;
      if (this.editing) {
        this.name = this.editing.name;
        this.description = this.editing.description ?? '';
        this.body = '';
        this.extraFiles = [];
        void this.loadDetail(this.editing.id);
      } else {
        // Create-mode defaults: empty inputs, no files. Same shape
        // the renderNew form used.
        this.name = '';
        this.description = '';
        this.body = '# Workflow\n\nDescribe when the coworker should use this skill.\n';
        this.extraFiles = [];
      }
    }
  }

  private async loadDetail(id: string): Promise<void> {
    this.loadingDetail = true;
    try {
      const detail: Skill = await this.api.getSkill(id);
      const files = detail.files ?? {};
      const manifest = files[SKILL_MANIFEST_NAME];
      if (manifest) {
        const parsed = parseSkillMd(manifest.content);
        // Prefer the live frontmatter description over the SkillSummary
        // value (the latter can lag if server-side cache hasn't caught up).
        if (parsed.description) this.description = parsed.description;
        this.body = parsed.body;
      }
      // Collect non-manifest files as filename rows.
      this.extraFiles = Object.entries(files)
        .filter(([path]) => path !== SKILL_MANIFEST_NAME)
        .map(([path, file]) => ({ path, content: file.content }));
    } catch (err) {
      this.err = this.errMessage(err);
    } finally {
      this.loadingDetail = false;
    }
  }

  private errMessage(err: unknown): string {
    if (err instanceof ApiError) return err.body?.message ?? `${err.status}`;
    return (err as Error).message ?? 'unknown error';
  }

  private addFile = () => {
    // Pre-fill with a unique placeholder so the user can rename
    // immediately. Backend rejects empty paths.
    let i = this.extraFiles.length + 1;
    let candidate = `file-${i}.md`;
    const taken = new Set(this.extraFiles.map((f) => f.path));
    while (taken.has(candidate)) {
      i += 1;
      candidate = `file-${i}.md`;
    }
    this.extraFiles = [
      ...this.extraFiles,
      { path: candidate, content: '' },
    ];
  };

  private renameFile(idx: number, newPath: string): void {
    this.extraFiles = this.extraFiles.map((f, i) =>
      i === idx ? { ...f, path: newPath } : f,
    );
  }

  private removeFile(idx: number): void {
    this.extraFiles = this.extraFiles.filter((_, i) => i !== idx);
  }

  private close = () => {
    this.open = false;
    this.dispatchEvent(
      new CustomEvent('close', { bubbles: true, composed: true }),
    );
  };

  private async save(): Promise<void> {
    if (!this.name.trim()) {
      this.err = 'Name is required.';
      return;
    }
    // Path validation: every extra file must use a legal path AND
    // not collide with the manifest filename.
    for (const f of this.extraFiles) {
      if (!isValidSkillFilePath(f.path)) {
        this.fileErr = `Invalid filename "${f.path}". Use a-z, 0-9, _, -, .`;
        return;
      }
      if (f.path === SKILL_MANIFEST_NAME) {
        this.fileErr = `"${SKILL_MANIFEST_NAME}" is reserved.`;
        return;
      }
    }
    this.busy = true;
    this.err = null;
    this.fileErr = null;
    const manifestBlob = serializeSkillMd(
      this.name.trim(),
      this.description.trim(),
      this.body,
    );
    const files: Record<string, string> = {
      [SKILL_MANIFEST_NAME]: manifestBlob,
    };
    for (const f of this.extraFiles) {
      files[f.path] = f.content;
    }
    try {
      let saved: Skill;
      if (this.editing) {
        const body: SkillUpdate = {
          name: this.name.trim(),
          enabled: true,
          files,
        };
        saved = await this.api.updateSkill(this.editing.id, body);
      } else {
        const body: SkillCreate = {
          name: this.name.trim(),
          enabled: true,
          files,
        };
        saved = await this.api.createSkill(body);
      }
      this.dispatchEvent(
        new CustomEvent<{ skill: Skill }>(
          this.editing ? 'skill-updated' : 'skill-created',
          {
            detail: { skill: saved },
            bubbles: true,
            composed: true,
          },
        ),
      );
      this.open = false;
      this.dispatchEvent(
        new CustomEvent('close', { bubbles: true, composed: true }),
      );
    } catch (err) {
      this.err = this.errMessage(err);
    } finally {
      this.busy = false;
    }
  }

  override render() {
    const title = this.editing
      ? `Edit skill: ${this.editing.name}`
      : 'New skill';
    return html`
      <rm-dialog
        title=${title}
        ?open=${this.open}
        ?close-on-backdrop=${!this.busy}
        ?close-on-esc=${!this.busy}
        width="640px"
        @close=${this.close}
      >
        <p class="text-[13px] text-ink-3 dark:text-d-ink-3 mb-4">
          A skill is a package of instructions and files coworkers
          can use.
        </p>

        ${this.loadingDetail
          ? html`<div class="rm-banner-loading">Loading…</div>`
          : nothing}

        <div class="mb-3">
          <label class="block text-[12.5px] font-medium mb-1">Name</label>
          <input
            type="text"
            class="w-full text-[13.5px] px-3 py-2 rounded-md border border-surface-3
              dark:border-d-surface-3 bg-surface-1 dark:bg-d-surface-1
              text-ink-0 dark:text-d-ink-0 focus:outline-none focus:ring-2 focus:ring-brand"
            placeholder="e.g. competitor-analysis"
            .value=${this.name}
            ?disabled=${this.busy}
            @input=${(e: Event) => {
              this.name = (e.target as HTMLInputElement).value;
            }}
            data-testid="skill-dialog-name"
          />
        </div>

        <div class="mb-3">
          <label class="block text-[12.5px] font-medium mb-1">Description</label>
          <input
            type="text"
            class="w-full text-[13.5px] px-3 py-2 rounded-md border border-surface-3
              dark:border-d-surface-3 bg-surface-1 dark:bg-d-surface-1
              text-ink-0 dark:text-d-ink-0 focus:outline-none focus:ring-2 focus:ring-brand"
            placeholder="What this skill does, in one line"
            .value=${this.description}
            ?disabled=${this.busy}
            @input=${(e: Event) => {
              this.description = (e.target as HTMLInputElement).value;
            }}
            data-testid="skill-dialog-description"
          />
        </div>

        <div class="mb-3">
          <label class="block text-[12.5px] font-medium mb-1">
            SKILL.md
            <span class="font-normal text-ink-3 dark:text-d-ink-3">— main file body</span>
          </label>
          <textarea
            rows="8"
            class="w-full text-[13px] px-3 py-2 rounded-md border border-surface-3
              dark:border-d-surface-3 bg-surface-1 dark:bg-d-surface-1
              text-ink-0 dark:text-d-ink-0 focus:outline-none focus:ring-2 focus:ring-brand
              font-mono resize-y leading-relaxed"
            placeholder="# When to use this skill&#10;Describe when and how the coworker should use it…"
            .value=${this.body}
            ?disabled=${this.busy}
            @input=${(e: Event) => {
              this.body = (e.target as HTMLTextAreaElement).value;
            }}
            data-testid="skill-dialog-body"
          ></textarea>
        </div>

        <div class="mb-3">
          <label class="block text-[12.5px] font-medium mb-1">Additional files</label>
          ${this.extraFiles.length === 0
            ? html`<div class="text-[12px] text-ink-3 dark:text-d-ink-3 mb-2">
                No extra files. SKILL.md is enough for most skills.
              </div>`
            : html`<div class="flex flex-col gap-1.5 mb-2">
                ${this.extraFiles.map((f, idx) => html`
                  <div class="flex items-center gap-2 border border-surface-3 dark:border-d-surface-3 rounded-md px-2.5 py-1.5">
                    <svg width="15" height="15" viewBox="0 0 24 24" fill="none"
                      stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"
                      class="text-ink-3 dark:text-d-ink-3 shrink-0" aria-hidden="true">
                      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                      <path d="M14 2v6h6"/>
                    </svg>
                    <input
                      type="text"
                      class="flex-1 text-[13px] bg-transparent outline-none font-mono"
                      .value=${f.path}
                      ?disabled=${this.busy}
                      @input=${(e: Event) =>
                        this.renameFile(idx, (e.target as HTMLInputElement).value)}
                      data-testid="skill-dialog-file"
                    />
                    <button
                      type="button"
                      class="rm-iconbtn rm-iconbtn--danger"
                      title="Remove file"
                      ?disabled=${this.busy}
                      @click=${() => this.removeFile(idx)}
                    >${iconTrash(14)}</button>
                  </div>
                `)}
              </div>`}
          <button
            type="button"
            class="text-[12.5px] text-brand hover:underline cursor-pointer"
            ?disabled=${this.busy}
            @click=${this.addFile}
            data-testid="skill-dialog-add-file"
          >+ Add file</button>
          ${this.fileErr
            ? html`<div class="text-[12px] text-red-600 dark:text-red-300 mt-1">${this.fileErr}</div>`
            : nothing}
        </div>

        ${this.err
          ? html`<div
              class="text-[12.5px] text-red-600 dark:text-red-300 mt-2"
              role="alert"
            >${this.err}</div>`
          : nothing}

        <div slot="footer" class="flex items-center gap-2">
          <button
            type="button"
            class="text-[12.5px] px-3 py-1.5 rounded-md border border-surface-3 dark:border-d-surface-3
              text-ink-2 dark:text-d-ink-2 hover:bg-surface-2 dark:hover:bg-d-surface-2 cursor-pointer
              disabled:opacity-60"
            ?disabled=${this.busy}
            @click=${this.close}
          >Cancel</button>
          <button
            type="button"
            class="text-[12.5px] px-3 py-1.5 rounded-md bg-brand text-white
              hover:bg-brand-dark transition-colors cursor-pointer
              disabled:opacity-60"
            ?disabled=${this.busy}
            @click=${() => void this.save()}
            data-testid="skill-dialog-save"
          >${this.busy
            ? 'Saving…'
            : this.editing
              ? 'Save changes'
              : 'Create skill'}</button>
        </div>
      </rm-dialog>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'rm-skill-dialog': SkillDialog;
  }
}
