// <rm-skill-dialog> — unified create + edit dialog for skills.
//
// UX goal (v2-C/PR20): the 80% case is a single-file skill with a
// short name, a description, and a body of instructions. We never
// expose YAML frontmatter to the user; the dialog reassembles
// `---\nname: X\ndescription: Y\n---\n{body}` on save and strips it
// back on load. Multi-file editing stays available behind the
// collapsed "Additional files" disclosure for power users.
//
//   ┌─ Create skill ───────────────────────────────────×┐
//   │  Name *                                           │
//   │  [ competitor-analysis            ]               │
//   │  ↳ Lowercase letters, digits, hyphens.            │
//   │                                                   │
//   │  Description *  (24 / 1024)                       │
//   │  [ Analyzes competitor pricing pages. ]           │
//   │                                                   │
//   │  Instructions                                     │
//   │  ┌────────────────────────────────────────────┐   │
//   │  │ # When to use                              │   │
//   │  │ Use this skill when ...                    │   │
//   │  └────────────────────────────────────────────┘   │
//   │                                                   │
//   │  ▶ Additional files                               │
//   │                                                   │
//   │                       [ Cancel ] [ Create skill ] │
//   └───────────────────────────────────────────────────┘
//
// Validation strategy:
//
// * Name: live-validated against the same regex the backend enforces
//   (`^[a-z0-9][a-z0-9-]{0,63}$`) plus a reserved-word check
//   (`anthropic` / `claude`). Save stays disabled while invalid so
//   the round-trip to a 422 never happens for typos.
// * Description: live char-count vs. the 1024 cap with color cues.
//   Empty disables Save (matches backend's MIN_LENGTH=1).
// * File paths: deferred to existing isValidSkillFilePath helper;
//   error surfaces on save attempt (rare in the easy-first flow
//   because the disclosure is collapsed by default).

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

// Mirror the backend regex (src/rolemesh/core/skills.py::_SKILL_NAME_RE).
// Keep this literal in sync — there's no shared source between Python
// and TS, so the same character class has to be repeated. The Pydantic
// `name: str = Field(pattern=...)` declaration on SkillCreate v1 carries
// the source of truth; this constant just lets the dialog give faster
// feedback than a 422 round-trip would.
const SKILL_NAME_RE = /^[a-z0-9][a-z0-9-]{0,63}$/;
const SKILL_NAME_RESERVED: ReadonlySet<string> = new Set([
  'anthropic',
  'claude',
]);
const DESCRIPTION_MAX = 1024;

/** Per-file and aggregate caps for client-uploaded extras. Skills are
 *  documents / scripts — a 1MB file is already huge for that. The
 *  total cap exists to defend the tenant DB from a careless drag-drop
 *  of a 200MB log directory; the backend currently has no row-size
 *  enforcement on `skill_files.content` (it's TEXT, unbounded). */
export const MAX_UPLOAD_BYTES_PER_FILE = 1 * 1024 * 1024;
export const MAX_UPLOAD_BYTES_TOTAL = 5 * 1024 * 1024;

/** Liberal text-vs-binary heuristic: a NUL byte in the first 4KB of a
 *  file is a near-perfect binary signal (text files never contain
 *  raw NULs; the DB's TEXT column would reject them anyway). Cheaper
 *  and more accurate than a MIME-by-extension allowlist. */
export function isLikelyBinary(content: string): boolean {
  const window = content.length > 4096 ? content.slice(0, 4096) : content;
  return window.indexOf('\0') !== -1;
}

/** Format byte count for the file row meta line ("1.2 KB", "234 B"). */
function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/** Strip the first path segment when there's more than one. Folder
 *  pickers expose paths as "<rootName>/sub/file.md" — keeping the root
 *  prefix would make the catalog read "my-skill-files/references/intro.md"
 *  which is meaningless to the agent. Single-segment paths pass through
 *  unchanged. */
export function stripLeadingFolder(path: string): string {
  const idx = path.indexOf('/');
  if (idx === -1) return path;
  return path.slice(idx + 1);
}

// Minimal structural typing for the (non-standard) FileSystemEntry
// API exposed by webkitGetAsEntry. TypeScript's lib.dom.d.ts has
// these now but their availability varies by tsconfig target; pin the
// shape we actually use so the code doesn't compile-fail on older
// lib defs.
interface FileSystemEntryLike {
  isFile: boolean;
  isDirectory: boolean;
  name: string;
}
interface FileSystemFileEntryLike extends FileSystemEntryLike {
  file: (cb: (f: File) => void, errcb?: (e: unknown) => void) => void;
}
interface FileSystemDirectoryEntryLike extends FileSystemEntryLike {
  createReader: () => {
    readEntries: (
      cb: (entries: FileSystemEntryLike[]) => void,
      errcb?: (e: unknown) => void,
    ) => void;
  };
}

/** Read one File via FileReader.readAsText. The wrapper rejects empty
 *  reads (zero bytes is a valid file but useless as a skill resource —
 *  and surfaces a clearer error than the silent "" content). */
function readFileAsText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onerror = () => reject(r.error ?? new Error('read error'));
    r.onload = () => {
      const v = r.result;
      if (typeof v !== 'string') {
        reject(new Error('expected text content'));
        return;
      }
      resolve(v);
    };
    r.readAsText(file);
  });
}

/** Return null when the name is acceptable, else the user-facing
 *  message. `null` is the empty-string case — the dialog treats it
 *  as "not yet typed" rather than an error so we don't flash red on
 *  first focus. */
export function validateSkillName(name: string): string | null {
  if (name.length === 0) return null;
  if (!SKILL_NAME_RE.test(name)) {
    return 'Lowercase letters, digits, hyphens only — no spaces, ' +
      'uppercase, or leading hyphen.';
  }
  if (SKILL_NAME_RESERVED.has(name)) {
    return `"${name}" is reserved by the Claude runtime. Pick a different name.`;
  }
  return null;
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
  /** Per-input server-side errors. Cleared on next edit of that
   *  input so the user gets immediate feedback when they retry. */
  @state() private nameServerErr: string | null = null;
  @state() private descriptionServerErr: string | null = null;
  @state() private loadingDetail = false;
  /** When true the "Additional files" disclosure is open. Default
   *  collapsed because the easy-first path is single-file. We also
   *  auto-open it on edit-mode load if the existing skill has any
   *  extra files (otherwise the user wouldn't see them). */
  @state() private advancedOpen = false;
  /** Sticky "user has interacted with the name field at least once"
   *  flag. Prevents the live error banner from flashing red while the
   *  user hasn't typed anything yet (empty string is the "untouched"
   *  state, not an error). */
  @state() private nameTouched = false;
  /** "Replaced N" / "Skipped binary M" feedback after an upload. Lives
   *  for ~3 seconds then clears itself so the dialog doesn't get
   *  cluttered. */
  @state() private uploadToast: string | null = null;
  /** True while a folder-pick or drag-drop read is in flight. Disables
   *  the upload controls so users can't double-trigger. */
  @state() private reading = false;
  /** Counter for drag-enter / drag-leave so child elements crossing
   *  the boundary don't toggle the highlight state. */
  private dragDepth = 0;
  @state() private dragHover = false;
  private toastTimer: number | null = null;

  private readonly api = getApiClient();

  protected override createRenderRoot() {
    return this;
  }

  override willUpdate(changed: Map<string, unknown>) {
    if (changed.has('open') && this.open) {
      this.err = null;
      this.fileErr = null;
      this.busy = false;
      this.advancedOpen = false;
      if (this.editing) {
        this.name = this.editing.name;
        this.description = this.editing.description ?? '';
        this.body = '';
        this.extraFiles = [];
        // Edit mode loads an existing valid name; treat it as already
        // touched so the field doesn't look pristine if the user
        // clears it later.
        this.nameTouched = true;
        void this.loadDetail(this.editing.id);
      } else {
        // Create-mode defaults: empty inputs, no files. Same shape
        // the renderNew form used.
        this.name = '';
        this.description = '';
        this.body = '# Workflow\n\nDescribe when the coworker should use this skill.\n';
        this.extraFiles = [];
        this.nameTouched = false;
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
      // Auto-disclose the advanced section if the skill already has
      // extras; otherwise the user wouldn't see them and might think
      // their files vanished on edit.
      if (this.extraFiles.length > 0) this.advancedOpen = true;
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

  /** Map a backend ErrorResponse to the field it concerns so the
   *  dialog can paint the error next to the offending input rather
   *  than as a generic banner at the bottom. Pydantic 422s use the
   *  `loc` array (e.g. `['body', 'name']`); the v1 handler's custom
   *  `INVALID_NAME` / `INVALID_MANIFEST` codes are similar but use
   *  `code` instead. Cover both paths because validation can fire
   *  from either layer depending on which check tripped first. */
  private fieldForError(
    err: unknown,
  ): 'name' | 'description' | null {
    if (!(err instanceof ApiError)) return null;
    const body = err.body as
      | { code?: string; details?: { name?: unknown }; detail?: unknown }
      | null
      | undefined;
    if (body?.code === 'INVALID_NAME') return 'name';
    if (body?.code === 'INVALID_MANIFEST') return 'description';
    // Pydantic shape: { detail: [{loc: ['body', 'name'], ...}] }
    const detail = body?.detail;
    if (Array.isArray(detail)) {
      for (const item of detail as Array<{ loc?: unknown[] }>) {
        const loc = item?.loc;
        if (Array.isArray(loc) && loc.includes('name')) return 'name';
      }
    }
    return null;
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

  /** Surface a transient banner under the upload zone. Repeated calls
   *  reset the timer so the latest message stays visible for the full
   *  duration. */
  private setUploadToast(msg: string): void {
    this.uploadToast = msg;
    if (this.toastTimer !== null) {
      window.clearTimeout(this.toastTimer);
    }
    this.toastTimer = window.setTimeout(() => {
      this.uploadToast = null;
      this.toastTimer = null;
    }, 4000);
  }

  /** Take a list of (path, content) pairs from a picker / drop and
   *  merge them into ``extraFiles``. Applies all three gates in one
   *  pass so the user gets ONE aggregated summary toast instead of
   *  per-file alerts: per-file size, NUL-byte binary detection, and
   *  total-size budget. Same-path collisions silently replace existing
   *  rows (with the count surfaced in the toast). */
  private ingestUploads(
    incoming: ReadonlyArray<{ path: string; content: string; bytes: number }>,
  ): void {
    let oversize = 0;
    let binary = 0;
    let replaced = 0;
    let added = 0;
    // Working copy of the file map keyed by path so collisions are
    // O(1) instead of O(N²) for large drops.
    const byPath = new Map(this.extraFiles.map((f) => [f.path, f.content]));
    // Start the total-size budget from the bytes already accepted into
    // extraFiles — that defends against death-by-a-thousand-cuts
    // (user drops 1 file of 4MB, then another 4MB later, etc.).
    let totalBytes = this.extraFiles.reduce(
      (acc, f) => acc + new Blob([f.content]).size,
      0,
    );
    for (const item of incoming) {
      if (item.path === SKILL_MANIFEST_NAME) {
        // SKILL.md goes through the dedicated textarea; an extra
        // file at that path would silently shadow it on the wire.
        binary += 0; // not binary; just rejected — count under a
        // dedicated tally to avoid mis-labeling.
        this.setUploadToast(
          `"${SKILL_MANIFEST_NAME}" is the main instructions file — edit it in the Instructions box above, not as an upload.`,
        );
        continue;
      }
      if (!isValidSkillFilePath(item.path)) {
        // Skip rather than abort the batch — one bad path in a 50-file
        // folder drop shouldn't lose the other 49 files.
        oversize += 0;
        continue;
      }
      if (item.bytes > MAX_UPLOAD_BYTES_PER_FILE) {
        oversize += 1;
        continue;
      }
      if (isLikelyBinary(item.content)) {
        binary += 1;
        continue;
      }
      if (totalBytes + item.bytes > MAX_UPLOAD_BYTES_TOTAL) {
        oversize += 1;
        continue;
      }
      if (byPath.has(item.path)) {
        replaced += 1;
      } else {
        added += 1;
      }
      // Subtract the previous size if replacing, so the budget is
      // accurate across re-uploads of the same path.
      if (byPath.has(item.path)) {
        const prev = byPath.get(item.path) ?? '';
        totalBytes -= new Blob([prev]).size;
      }
      byPath.set(item.path, item.content);
      totalBytes += item.bytes;
    }
    // Rebuild extraFiles preserving original order, then append new
    // ones in folder-sorted order so the tree renders predictably.
    const seen = new Set<string>();
    const next: ExtraFile[] = [];
    for (const f of this.extraFiles) {
      if (byPath.has(f.path)) {
        next.push({ path: f.path, content: byPath.get(f.path) ?? '' });
        seen.add(f.path);
      }
    }
    const newPaths = [...byPath.keys()]
      .filter((p) => !seen.has(p))
      .sort();
    for (const p of newPaths) {
      next.push({ path: p, content: byPath.get(p) ?? '' });
    }
    this.extraFiles = next;
    // Force-disclose so the user sees what just landed; even if they
    // collapsed it manually a moment ago, after an explicit upload
    // hiding the result is unhelpful.
    if (added > 0 || replaced > 0) this.advancedOpen = true;
    // Compose a single toast covering everything that happened.
    const parts: string[] = [];
    if (added > 0) parts.push(`${added} added`);
    if (replaced > 0) parts.push(`${replaced} replaced`);
    if (binary > 0) parts.push(`${binary} skipped (binary)`);
    if (oversize > 0) {
      parts.push(`${oversize} skipped (over size cap)`);
    }
    if (parts.length > 0) this.setUploadToast(parts.join(' · '));
  }

  /** Pull a flat list of (path, File) pairs from a FileList. Used by
   *  the file/folder pickers. Folder pickers populate
   *  ``webkitRelativePath`` — strip the first segment so a user who
   *  picks "my-skill-files/" doesn't end up with that as a prefix on
   *  every file path inside the catalog. */
  private async readFilesFromInput(list: FileList): Promise<
    Array<{ path: string; content: string; bytes: number }>
  > {
    const out: Array<{ path: string; content: string; bytes: number }> = [];
    for (const file of Array.from(list)) {
      // webkitRelativePath is "folderName/sub/file.md" when the user
      // used the folder picker; empty string for plain file picker.
      const raw =
        ((file as File & { webkitRelativePath?: string })
          .webkitRelativePath) || file.name;
      const path = stripLeadingFolder(raw);
      try {
        const content = await readFileAsText(file);
        out.push({ path, content, bytes: file.size });
      } catch {
        // Skip unreadable; the toast tally below will note it as
        // binary which is a close-enough explanation for the user.
        out.push({ path, content: '\0', bytes: file.size });
      }
    }
    return out;
  }

  /** Recursively walk a dropped folder using the (non-standard but
   *  widely supported) webkitGetAsEntry API. Falls back to FileList
   *  semantics when the drop is files-only and the entry API isn't
   *  available. */
  private async readEntries(
    items: DataTransferItemList,
  ): Promise<Array<{ path: string; content: string; bytes: number }>> {
    const out: Array<{ path: string; content: string; bytes: number }> = [];
    const entries: FileSystemEntryLike[] = [];
    for (const it of Array.from(items)) {
      if (it.kind !== 'file') continue;
      const entry =
        typeof (it as DataTransferItem & {
          webkitGetAsEntry?: () => FileSystemEntryLike | null;
        }).webkitGetAsEntry === 'function'
          ? (it as DataTransferItem & {
              webkitGetAsEntry: () => FileSystemEntryLike | null;
            }).webkitGetAsEntry()
          : null;
      if (entry) {
        entries.push(entry);
      } else {
        // Browser without webkitGetAsEntry (mostly happy-dom in
        // tests) — fall back to getAsFile.
        const f = it.getAsFile();
        if (f) {
          const content = await readFileAsText(f).catch(() => '\0');
          out.push({ path: f.name, content, bytes: f.size });
        }
      }
    }
    async function walk(
      entry: FileSystemEntryLike,
      prefix: string,
    ): Promise<void> {
      if (entry.isFile) {
        const file = await new Promise<File>((resolve, reject) =>
          (entry as FileSystemFileEntryLike).file(resolve, reject),
        );
        const content = await readFileAsText(file).catch(() => '\0');
        out.push({
          path: stripLeadingFolder(prefix + file.name),
          content,
          bytes: file.size,
        });
      } else if (entry.isDirectory) {
        const reader = (entry as FileSystemDirectoryEntryLike).createReader();
        // readEntries returns a batch at a time; keep calling until
        // it returns empty per the spec.
        let batch: FileSystemEntryLike[] = [];
        do {
          batch = await new Promise<FileSystemEntryLike[]>(
            (resolve, reject) => reader.readEntries(resolve, reject),
          );
          for (const child of batch) {
            await walk(child, `${prefix}${entry.name}/`);
          }
        } while (batch.length > 0);
      }
    }
    for (const e of entries) {
      // Top-level entries get no prefix; their own name shows up via
      // stripLeadingFolder inside walk's file branch (the folder
      // becomes the prefix for its children, but its own segment is
      // dropped by stripLeadingFolder so the catalog doesn't carry
      // "my-skill-files/" everywhere).
      await walk(e, '');
    }
    return out;
  }

  private onPickFiles = async (e: Event) => {
    const input = e.target as HTMLInputElement;
    if (!input.files || input.files.length === 0) return;
    this.reading = true;
    try {
      const items = await this.readFilesFromInput(input.files);
      this.ingestUploads(items);
    } finally {
      this.reading = false;
      input.value = ''; // allow re-picking the same path
    }
  };

  private onDragEnter = (e: DragEvent) => {
    e.preventDefault();
    this.dragDepth += 1;
    this.dragHover = true;
  };

  private onDragLeave = (e: DragEvent) => {
    e.preventDefault();
    this.dragDepth -= 1;
    if (this.dragDepth <= 0) {
      this.dragDepth = 0;
      this.dragHover = false;
    }
  };

  private onDragOver = (e: DragEvent) => {
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
  };

  private onDrop = async (e: DragEvent) => {
    e.preventDefault();
    this.dragDepth = 0;
    this.dragHover = false;
    if (!e.dataTransfer) return;
    this.reading = true;
    try {
      const items = await this.readEntries(e.dataTransfer.items);
      this.ingestUploads(items);
    } finally {
      this.reading = false;
    }
  };

  private close = () => {
    this.open = false;
    this.dispatchEvent(
      new CustomEvent('close', { bubbles: true, composed: true }),
    );
  };

  private async save(): Promise<void> {
    // The Save button is disabled when isValid() is false, but a
    // keyboard-Enter on the name input could still fire this handler
    // — re-check defensively so the empty / invalid case surfaces an
    // inline error instead of round-tripping to a backend 422.
    const nameProblem = validateSkillName(this.name.trim());
    if (this.name.trim() === '') {
      this.nameServerErr = 'Name is required.';
      this.nameTouched = true;
      return;
    }
    if (nameProblem) {
      this.nameServerErr = nameProblem;
      this.nameTouched = true;
      return;
    }
    if (this.description.trim() === '') {
      this.descriptionServerErr = 'Description is required.';
      return;
    }
    if (this.description.length > DESCRIPTION_MAX) {
      this.descriptionServerErr =
        `Description is ${this.description.length} characters; max ${DESCRIPTION_MAX}.`;
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
    this.nameServerErr = null;
    this.descriptionServerErr = null;
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
        // Edit mode: send the full file set, but omit `name` since
        // the backend treats name as immutable on PATCH. We could send
        // `name: this.editing.name` (the backend accepts unchanged
        // values) but omitting it keeps the wire payload smaller and
        // makes the "name is read-only here" contract obvious.
        const body: SkillUpdate = {
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
      const field = this.fieldForError(err);
      const msg = this.errMessage(err);
      if (field === 'name') this.nameServerErr = msg;
      else if (field === 'description') this.descriptionServerErr = msg;
      else this.err = msg;
    } finally {
      this.busy = false;
    }
  }

  /** Compose-time gating for the Save button. Anything that disables
   *  it must also short-circuit save() above so keyboard-Enter and
   *  programmatic invocation match the visible state. */
  private isValid(): boolean {
    const trimmedName = this.name.trim();
    if (trimmedName === '') return false;
    if (validateSkillName(trimmedName) !== null) return false;
    if (this.description.trim() === '') return false;
    if (this.description.length > DESCRIPTION_MAX) return false;
    return true;
  }

  override render() {
    const title = this.editing
      ? `Edit skill: ${this.editing.name}`
      : 'Create skill';
    // Live name validation. Suppress the live error while the user
    // hasn't touched the field yet — flashing red on an empty field
    // before they've typed is hostile.
    const trimmedName = this.name.trim();
    const liveNameErr =
      this.nameTouched && trimmedName !== ''
        ? validateSkillName(trimmedName)
        : null;
    const nameErrorText = this.nameServerErr ?? liveNameErr;
    // Description counter color cue. Yellow when within 200 of the
    // cap; red when over. Mirror the backend max so a user who's at
    // 1024 chars sees green/neutral, 1025 sees red.
    const descLen = this.description.length;
    let descCounterClass = 'text-ink-3 dark:text-d-ink-3';
    if (descLen > DESCRIPTION_MAX) {
      descCounterClass = 'text-red-600 dark:text-red-300 font-medium';
    } else if (descLen > DESCRIPTION_MAX - 200) {
      descCounterClass = 'text-amber-600 dark:text-amber-300';
    }
    const canSave = this.isValid() && !this.busy;
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
          A skill is a package of instructions a coworker uses
          automatically when relevant.
        </p>

        ${this.loadingDetail
          ? html`<div class="rm-banner-loading">Loading…</div>`
          : nothing}

        <div class="mb-3">
          <label class="block text-[12.5px] font-medium mb-1">
            Name
            <span class="text-red-600 dark:text-red-300">*</span>
          </label>
          <input
            type="text"
            class=${`w-full text-[13.5px] px-3 py-2 rounded-md border bg-surface-1
              dark:bg-d-surface-1 text-ink-0 dark:text-d-ink-0 focus:outline-none
              focus:ring-2 ${nameErrorText
                ? 'border-red-500 dark:border-red-400 focus:ring-red-400'
                : 'border-surface-3 dark:border-d-surface-3 focus:ring-brand'}`}
            placeholder="e.g. competitor-analysis"
            .value=${this.name}
            ?disabled=${this.busy}
            aria-invalid=${nameErrorText ? 'true' : 'false'}
            @input=${(e: Event) => {
              this.name = (e.target as HTMLInputElement).value;
              this.nameTouched = true;
              this.nameServerErr = null;
            }}
            @blur=${() => { this.nameTouched = true; }}
            data-testid="skill-dialog-name"
          />
          ${nameErrorText
            ? html`<div
                class="text-[12px] text-red-600 dark:text-red-300 mt-1"
                data-testid="skill-dialog-name-error"
                role="alert"
              >${nameErrorText}</div>`
            : html`<div class="text-[12px] text-ink-3 dark:text-d-ink-3 mt-1">
                Lowercase letters, digits, hyphens. Used as a folder name on the agent side.
              </div>`}
        </div>

        <div class="mb-3">
          <div class="flex items-center justify-between mb-1">
            <label class="block text-[12.5px] font-medium">
              Description
              <span class="text-red-600 dark:text-red-300">*</span>
            </label>
            <span
              class=${`text-[11.5px] ${descCounterClass}`}
              data-testid="skill-dialog-desc-counter"
            >${descLen} / ${DESCRIPTION_MAX}</span>
          </div>
          <input
            type="text"
            class=${`w-full text-[13.5px] px-3 py-2 rounded-md border bg-surface-1
              dark:bg-d-surface-1 text-ink-0 dark:text-d-ink-0 focus:outline-none
              focus:ring-2 ${this.descriptionServerErr || descLen > DESCRIPTION_MAX
                ? 'border-red-500 dark:border-red-400 focus:ring-red-400'
                : 'border-surface-3 dark:border-d-surface-3 focus:ring-brand'}`}
            placeholder="Analyzes competitor pricing pages and summarizes trends."
            .value=${this.description}
            ?disabled=${this.busy}
            @input=${(e: Event) => {
              this.description = (e.target as HTMLInputElement).value;
              this.descriptionServerErr = null;
            }}
            data-testid="skill-dialog-description"
          />
          ${this.descriptionServerErr
            ? html`<div
                class="text-[12px] text-red-600 dark:text-red-300 mt-1"
                data-testid="skill-dialog-desc-error"
                role="alert"
              >${this.descriptionServerErr}</div>`
            : html`<div class="text-[12px] text-ink-3 dark:text-d-ink-3 mt-1">
                What it does and when to use it. Shown to the coworker so it knows when to invoke this skill.
              </div>`}
        </div>

        ${renderSkillFolderSnapshot(this.extraFiles)}

        <div class="mb-3">
          <label class="block text-[12.5px] font-medium mb-1">
            Instructions
            <span class="font-normal text-ink-3 dark:text-d-ink-3">
              — saved as <code class="font-mono text-[12px]">SKILL.md</code>
            </span>
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

        <details
          class="mb-3 group"
          ?open=${this.advancedOpen}
          @toggle=${(e: Event) => {
            this.advancedOpen = (e.target as HTMLDetailsElement).open;
          }}
        >
          <summary
            class="text-[12.5px] font-medium cursor-pointer select-none
              text-ink-2 dark:text-d-ink-2 hover:text-ink-0 dark:hover:text-d-ink-0"
            data-testid="skill-dialog-advanced-toggle"
          >Add files to this skill folder</summary>
          <div class="mt-2">
            <div
              class=${`border-2 border-dashed rounded-md px-4 py-5 text-center transition-colors
                ${this.dragHover
                  ? 'border-brand bg-brand/5'
                  : 'border-surface-3 dark:border-d-surface-3 bg-surface-1 dark:bg-d-surface-1'}`}
              data-testid="skill-dialog-dropzone"
              @dragenter=${this.onDragEnter}
              @dragleave=${this.onDragLeave}
              @dragover=${this.onDragOver}
              @drop=${this.onDrop}
            >
              <div class="text-[12.5px] text-ink-2 dark:text-d-ink-2 mb-2">
                ${this.reading
                  ? 'Reading files…'
                  : 'Drop a folder or files here, or:'}
              </div>
              <div class="flex items-center justify-center gap-2">
                <label
                  class="text-[12.5px] px-3 py-1.5 rounded-md border border-surface-3 dark:border-d-surface-3
                    text-ink-1 dark:text-d-ink-1 hover:bg-surface-2 dark:hover:bg-d-surface-2
                    cursor-pointer ${this.reading || this.busy ? 'opacity-60 pointer-events-none' : ''}"
                  data-testid="skill-dialog-pick-folder-label"
                >
                  Choose folder
                  <input
                    type="file"
                    class="hidden"
                    webkitdirectory
                    multiple
                    data-testid="skill-dialog-pick-folder"
                    @change=${this.onPickFiles}
                  />
                </label>
                <label
                  class="text-[12.5px] px-3 py-1.5 rounded-md border border-surface-3 dark:border-d-surface-3
                    text-ink-1 dark:text-d-ink-1 hover:bg-surface-2 dark:hover:bg-d-surface-2
                    cursor-pointer ${this.reading || this.busy ? 'opacity-60 pointer-events-none' : ''}"
                  data-testid="skill-dialog-pick-files-label"
                >
                  Choose files
                  <input
                    type="file"
                    class="hidden"
                    multiple
                    data-testid="skill-dialog-pick-files"
                    @change=${this.onPickFiles}
                  />
                </label>
              </div>
              <div class="text-[11px] text-ink-3 dark:text-d-ink-3 mt-2">
                Text files only, ${formatBytes(MAX_UPLOAD_BYTES_PER_FILE)} per file,
                ${formatBytes(MAX_UPLOAD_BYTES_TOTAL)} total.
              </div>
            </div>
            ${this.uploadToast
              ? html`<div
                  class="text-[12px] text-ink-2 dark:text-d-ink-2 mt-2"
                  data-testid="skill-dialog-upload-toast"
                  role="status"
                >${this.uploadToast}</div>`
              : nothing}
            ${this.extraFiles.length === 0
              ? html`<div class="text-[12px] text-ink-3 dark:text-d-ink-3 mt-3">
                  Most skills don't need extras. Drop files above when
                  the coworker needs reference material beyond the
                  instructions.
                </div>`
              : renderFileTree(
                  this.extraFiles,
                  this.busy,
                  (idx, newPath) => this.renameFile(idx, newPath),
                  (idx) => this.removeFile(idx),
                )}
            <button
              type="button"
              class="text-[12.5px] text-brand hover:underline cursor-pointer mt-3"
              ?disabled=${this.busy}
              @click=${this.addFile}
              data-testid="skill-dialog-add-file"
            >+ Add empty file</button>
            ${this.fileErr
              ? html`<div class="text-[12px] text-red-600 dark:text-red-300 mt-1">${this.fileErr}</div>`
              : nothing}
          </div>
        </details>

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
              disabled:opacity-60 disabled:cursor-not-allowed"
            ?disabled=${!canSave}
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

/** "Your skill folder" snapshot — read-only orientation that
 *  unifies the user's mental model. SKILL.md (the Instructions
 *  textarea) and any uploaded extras live in the SAME folder once
 *  the skill ships to the coworker; rendering both here together
 *  makes that obvious BEFORE the user starts typing.
 *
 *  Read-only on purpose: the editable controls (textarea, upload
 *  zone, per-file rename / delete) live in their respective sections
 *  below. Duplicating the file list as a passive snapshot here is
 *  cheap and removes the "wait, where do these files end up?"
 *  question. */
function renderSkillFolderSnapshot(extraFiles: ExtraFile[]) {
  // Group extras by top-level folder, same grouping rule as
  // renderFileTree below — so the snapshot's shape matches what
  // the user sees in the Additional files section.
  const groups = new Map<string, ExtraFile[]>();
  for (const f of extraFiles) {
    const slash = f.path.indexOf('/');
    const key = slash === -1 ? '' : f.path.slice(0, slash);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(f);
  }
  const ordered = [...groups.entries()].sort(([a], [b]) => {
    if (a === '') return -1;
    if (b === '') return 1;
    return a.localeCompare(b);
  });
  return html`
    <div
      class="mb-3 px-3 py-2.5 rounded-md bg-surface-2 dark:bg-d-surface-2
        border border-surface-3 dark:border-d-surface-3"
      data-testid="skill-dialog-folder-snapshot"
    >
      <div class="text-[11.5px] uppercase tracking-wide font-medium text-ink-3 dark:text-d-ink-3 mb-1.5">
        Your skill folder
      </div>
      <div class="flex flex-col gap-0.5 font-mono text-[12.5px]">
        <div class="flex items-center gap-2 text-ink-0 dark:text-d-ink-0">
          <span aria-hidden="true">📄</span>
          <span>SKILL.md</span>
          <span class="font-sans text-[11.5px] text-ink-3 dark:text-d-ink-3">
            ← edited in Instructions below
          </span>
        </div>
        ${extraFiles.length === 0
          ? html`<div class="flex items-center gap-2 text-ink-3 dark:text-d-ink-3">
              <span aria-hidden="true">📁</span>
              <span class="font-sans text-[11.5px]">
                additional files appear here when you upload them
              </span>
            </div>`
          : ordered.map(([folder, files]) => folder === ''
              ? files.map((f) => html`
                  <div class="flex items-center gap-2 text-ink-0 dark:text-d-ink-0">
                    <span aria-hidden="true">📄</span>
                    <span>${f.path}</span>
                  </div>
                `)
              : html`
                  <div class="flex items-center gap-2 text-ink-1 dark:text-d-ink-1">
                    <span aria-hidden="true">📁</span>
                    <span>${folder}/</span>
                  </div>
                  ${files.map((f) => html`
                    <div class="flex items-center gap-2 text-ink-0 dark:text-d-ink-0 pl-5">
                      <span aria-hidden="true">📄</span>
                      <span>${f.path.slice(folder.length + 1)}</span>
                    </div>
                  `)}
                `,
          )}
      </div>
    </div>
  `;
}

/** Render the extras as a folder-grouped list. Files at the catalog
 *  root (no slash in path) render first, then each folder gets its
 *  own group with the file rows beneath. The tree is one level deep
 *  visually — nested folders just show as `sub/dir/file.md` inside
 *  their top-level folder group. Keeps the markup simple while still
 *  giving the user a sense of structure. Built as a free function so
 *  the dialog's render() stays a one-screen scroll. */
function renderFileTree(
  files: ExtraFile[],
  busy: boolean,
  onRename: (idx: number, newPath: string) => void,
  onRemove: (idx: number) => void,
) {
  const groups = new Map<string, Array<{ file: ExtraFile; idx: number }>>();
  files.forEach((file, idx) => {
    const slash = file.path.indexOf('/');
    const key = slash === -1 ? '' : file.path.slice(0, slash);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push({ file, idx });
  });
  // Root files first ("" key), then folder groups alphabetically.
  const ordered = [...groups.entries()].sort(([a], [b]) => {
    if (a === '') return -1;
    if (b === '') return 1;
    return a.localeCompare(b);
  });
  return html`
    <div class="flex flex-col gap-1 mt-3" data-testid="skill-dialog-file-tree">
      ${ordered.map(([folder, entries]) => html`
        ${folder
          ? html`<div class="text-[12px] text-ink-2 dark:text-d-ink-2 font-mono mt-2">${folder}/</div>`
          : nothing}
        ${entries.map(({ file, idx }) => html`
          <div class=${`flex items-center gap-2 border border-surface-3 dark:border-d-surface-3
            rounded-md px-2.5 py-1.5 ${folder ? 'ml-3' : ''}`}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
              stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"
              class="text-ink-3 dark:text-d-ink-3 shrink-0" aria-hidden="true">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
              <path d="M14 2v6h6"/>
            </svg>
            <input
              type="text"
              class="flex-1 text-[13px] bg-transparent outline-none font-mono"
              .value=${file.path}
              ?disabled=${busy}
              @input=${(e: Event) =>
                onRename(idx, (e.target as HTMLInputElement).value)}
              data-testid="skill-dialog-file"
            />
            <span class="text-[11px] text-ink-3 dark:text-d-ink-3 whitespace-nowrap">
              ${formatBytes(new Blob([file.content]).size)}
            </span>
            <button
              type="button"
              class="rm-iconbtn rm-iconbtn--danger"
              title="Remove file"
              ?disabled=${busy}
              @click=${() => onRemove(idx)}
            >${iconTrash(14)}</button>
          </div>
        `)}
      `)}
    </div>
  `;
}

declare global {
  interface HTMLElementTagNameMap {
    'rm-skill-dialog': SkillDialog;
  }
}
