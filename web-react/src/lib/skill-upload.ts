// Skill upload pure logic — extracted from the Lit skill-dialog.ts so
// the caps/binary/name gates and the three-gate ingest pipeline are
// React-agnostic and unit-testable (the Lit versions were tangled with
// component state + DOM). The dialog wires DOM events (drop / pickers)
// to `ingestUploads`; everything decision-shaped lives here.

import { isValidSkillFilePath } from './skill-constants';
import { SKILL_MANIFEST_NAME } from './skill-constants';

// Mirror the backend regex (src/rolemesh/core/skills.py::_SKILL_NAME_RE);
// there's no shared source between Python and TS, so the character class
// is repeated. Gives faster feedback than a 422 round-trip.
const SKILL_NAME_RE = /^[a-z0-9][a-z0-9-]{0,63}$/;
const SKILL_NAME_RESERVED: ReadonlySet<string> = new Set(['anthropic', 'claude']);
export const DESCRIPTION_MAX = 1024;

/** Per-file and aggregate caps for client-uploaded extras (ported). */
export const MAX_UPLOAD_BYTES_PER_FILE = 1 * 1024 * 1024;
export const MAX_UPLOAD_BYTES_TOTAL = 5 * 1024 * 1024;

/** Return null when the name is acceptable, else the user-facing
 *  message. `null` on empty = "not yet typed", not an error, so the
 *  dialog doesn't flash red on first focus. */
export function validateSkillName(name: string): string | null {
  if (name.length === 0) return null;
  if (!SKILL_NAME_RE.test(name)) {
    return 'Lowercase letters, digits, hyphens only — no spaces, uppercase, or leading hyphen.';
  }
  if (SKILL_NAME_RESERVED.has(name)) {
    return `"${name}" is reserved by the Claude runtime. Pick a different name.`;
  }
  return null;
}

/** Liberal text-vs-binary heuristic: a NUL byte in the first 4KB is a
 *  near-perfect binary signal (text files never contain raw NULs; the
 *  DB's TEXT column rejects them anyway). */
export function isLikelyBinary(content: string): boolean {
  const window = content.length > 4096 ? content.slice(0, 4096) : content;
  return window.indexOf('\0') !== -1;
}

/** Byte count for a UTF-8 string content (mirrors `new Blob([s]).size`
 *  without needing a DOM Blob — countable in node tests). */
export function contentBytes(content: string): number {
  // TextEncoder is available in node and the browser.
  return new TextEncoder().encode(content).length;
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export interface ExtraFile {
  /** path relative to skill root, e.g. "references/intro.md". */
  path: string;
  content: string;
}

export interface IncomingFile {
  path: string;
  content: string;
  bytes: number;
}

export interface IngestTally {
  added: number;
  replaced: number;
  binary: number;
  oversize: number;
}

export interface IngestResult {
  files: ExtraFile[];
  tally: IngestTally;
  /** True when a SKILL.md-path upload was rejected — the dialog shows
   *  the dedicated guidance toast instead of the tally line. */
  manifestRejected: boolean;
  /** True when anything was added/replaced — the dialog force-opens the
   *  disclosure so the user sees what landed. */
  disclose: boolean;
}

/** Merge `incoming` into `existing` in one pass with three gates and a
 *  single aggregated tally (ported from the Lit `ingestUploads`, made
 *  pure). Gate order matters:
 *   1. SKILL.md path → rejected with guidance (edit in Instructions).
 *   2. invalid path → skip (never abort the batch — one bad path in a
 *      50-file drop must not lose the other 49).
 *   3. per-file cap → skip; NUL-binary → skip; cumulative budget → skip.
 *  Same-path collisions replace (budget subtracts the old size first).
 *  Existing rows keep order; new paths append sorted. */
export function ingestUploads(
  existing: readonly ExtraFile[],
  incoming: readonly IncomingFile[],
): IngestResult {
  const tally: IngestTally = { added: 0, replaced: 0, binary: 0, oversize: 0 };
  let manifestRejected = false;

  const byPath = new Map(existing.map((f) => [f.path, f.content]));
  // Start the budget from bytes already accepted — defends against
  // death-by-a-thousand-cuts across repeated drops.
  let totalBytes = existing.reduce((acc, f) => acc + contentBytes(f.content), 0);

  for (const item of incoming) {
    if (item.path === SKILL_MANIFEST_NAME) {
      manifestRejected = true;
      continue;
    }
    if (!isValidSkillFilePath(item.path)) {
      continue; // skip silently — never abort the batch
    }
    if (item.bytes > MAX_UPLOAD_BYTES_PER_FILE) {
      tally.oversize += 1;
      continue;
    }
    if (isLikelyBinary(item.content)) {
      tally.binary += 1;
      continue;
    }
    if (totalBytes + item.bytes > MAX_UPLOAD_BYTES_TOTAL) {
      tally.oversize += 1;
      continue;
    }
    if (byPath.has(item.path)) {
      tally.replaced += 1;
      totalBytes -= contentBytes(byPath.get(item.path) ?? '');
    } else {
      tally.added += 1;
    }
    byPath.set(item.path, item.content);
    totalBytes += item.bytes;
  }

  // Preserve existing order; append new paths sorted.
  const seen = new Set<string>();
  const files: ExtraFile[] = [];
  for (const f of existing) {
    if (byPath.has(f.path)) {
      files.push({ path: f.path, content: byPath.get(f.path) ?? '' });
      seen.add(f.path);
    }
  }
  for (const p of [...byPath.keys()].filter((x) => !seen.has(x)).sort()) {
    files.push({ path: p, content: byPath.get(p) ?? '' });
  }

  return {
    files,
    tally,
    manifestRejected,
    disclose: tally.added > 0 || tally.replaced > 0,
  };
}

/** Compose the aggregated upload toast from a tally (empty string when
 *  nothing happened). */
export function composeTally(t: IngestTally): string {
  const parts: string[] = [];
  if (t.added > 0) parts.push(`${t.added} added`);
  if (t.replaced > 0) parts.push(`${t.replaced} replaced`);
  if (t.binary > 0) parts.push(`${t.binary} skipped (binary)`);
  if (t.oversize > 0) parts.push(`${t.oversize} skipped (over size cap)`);
  return parts.join(' · ');
}

export const MANIFEST_UPLOAD_REJECTED = `"${SKILL_MANIFEST_NAME}" is the main instructions file — edit it in the Instructions box above, not as an upload.`;

/** Group extra files by top-level folder for the tree render (root
 *  files first under key "", then folders alphabetical). */
export function groupByFolder(
  files: readonly ExtraFile[],
): { folder: string; entries: { file: ExtraFile; index: number }[] }[] {
  const groups = new Map<string, { file: ExtraFile; index: number }[]>();
  files.forEach((file, index) => {
    const slash = file.path.indexOf('/');
    const key = slash === -1 ? '' : file.path.slice(0, slash);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push({ file, index });
  });
  return [...groups.entries()]
    .sort(([a], [b]) => (a === '' ? -1 : b === '' ? 1 : a.localeCompare(b)))
    .map(([folder, entries]) => ({ folder, entries }));
}
