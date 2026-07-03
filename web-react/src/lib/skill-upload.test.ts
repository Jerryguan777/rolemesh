import { describe, expect, it } from 'vitest';
import {
  composeTally,
  contentBytes,
  groupByFolder,
  ingestUploads,
  isLikelyBinary,
  validateSkillName,
  type ExtraFile,
  type IncomingFile,
  MAX_UPLOAD_BYTES_PER_FILE,
} from './skill-upload';

const file = (path: string, content: string): IncomingFile => ({
  path,
  content,
  bytes: contentBytes(content),
});

describe('validateSkillName', () => {
  it('accepts a lowercase slug, null on empty', () => {
    expect(validateSkillName('')).toBeNull();
    expect(validateSkillName('pdf-toolkit')).toBeNull();
    expect(validateSkillName('a1')).toBeNull();
  });
  it('rejects uppercase, spaces, leading hyphen, overlong', () => {
    expect(validateSkillName('PDF')).toContain('Lowercase');
    expect(validateSkillName('has space')).toContain('Lowercase');
    expect(validateSkillName('-lead')).toContain('Lowercase');
    expect(validateSkillName('a'.repeat(65))).toContain('Lowercase');
  });
  it('rejects reserved runtime names', () => {
    expect(validateSkillName('claude')).toContain('reserved');
    expect(validateSkillName('anthropic')).toContain('reserved');
  });
});

describe('isLikelyBinary', () => {
  it('flags a NUL byte in the first 4KB', () => {
    expect(isLikelyBinary('hello\0world')).toBe(true);
    expect(isLikelyBinary('plain text')).toBe(false);
    expect(isLikelyBinary('x'.repeat(5000) + '\0')).toBe(false); // beyond window
  });
});

describe('ingestUploads gates', () => {
  it('rejects SKILL.md path with the manifest flag, no tally', () => {
    const r = ingestUploads([], [file('SKILL.md', 'shadow')]);
    expect(r.manifestRejected).toBe(true);
    expect(r.files).toHaveLength(0);
    expect(r.tally.added).toBe(0);
  });

  it('skips invalid paths without aborting the batch', () => {
    const r = ingestUploads([], [file('../evil', 'x'), file('good.md', 'ok')]);
    expect(r.files.map((f) => f.path)).toEqual(['good.md']);
    expect(r.tally.added).toBe(1);
  });

  it('skips oversize files (per-file cap) and tallies them', () => {
    const big = { path: 'big.md', content: 'x', bytes: MAX_UPLOAD_BYTES_PER_FILE + 1 };
    const r = ingestUploads([], [big, file('ok.md', 'y')]);
    expect(r.tally.oversize).toBe(1);
    expect(r.files.map((f) => f.path)).toEqual(['ok.md']);
  });

  it('skips binary files and tallies them', () => {
    const r = ingestUploads([], [file('bin', 'a\0b'), file('ok.md', 'y')]);
    expect(r.tally.binary).toBe(1);
    expect(r.files.map((f) => f.path)).toEqual(['ok.md']);
  });

  it('enforces the cumulative budget across the session (replace subtracts old size)', () => {
    // 3 MiB already accepted; a 3 MiB add would exceed 5 MiB total → skip.
    const big = 'x'.repeat(3 * 1024 * 1024);
    const existing: ExtraFile[] = [{ path: 'a.md', content: big }];
    const add = { path: 'b.md', content: 'y', bytes: 3 * 1024 * 1024 };
    const r = ingestUploads(existing, [add]);
    expect(r.tally.oversize).toBe(1);
    expect(r.files.map((f) => f.path)).toEqual(['a.md']);
  });

  it('replaces same-path collisions (tallied as replaced, order preserved)', () => {
    const existing: ExtraFile[] = [{ path: 'a.md', content: 'old' }];
    const r = ingestUploads(existing, [file('a.md', 'new')]);
    expect(r.tally.replaced).toBe(1);
    expect(r.tally.added).toBe(0);
    expect(r.files).toEqual([{ path: 'a.md', content: 'new' }]);
  });

  it('keeps existing order and appends new paths sorted; discloses on change', () => {
    const existing: ExtraFile[] = [{ path: 'z.md', content: '1' }];
    const r = ingestUploads(existing, [file('b.md', '2'), file('a.md', '3')]);
    expect(r.files.map((f) => f.path)).toEqual(['z.md', 'a.md', 'b.md']);
    expect(r.disclose).toBe(true);
  });
});

describe('composeTally', () => {
  it('joins present tallies, empty when nothing happened', () => {
    expect(composeTally({ added: 2, replaced: 1, binary: 3, oversize: 1 })).toBe(
      '2 added · 1 replaced · 3 skipped (binary) · 1 skipped (over size cap)',
    );
    expect(composeTally({ added: 0, replaced: 0, binary: 0, oversize: 0 })).toBe('');
  });
});

describe('groupByFolder', () => {
  it('roots first, then folders alphabetical; entries keep their index', () => {
    const files: ExtraFile[] = [
      { path: 'readme.md', content: '' },
      { path: 'refs/b.md', content: '' },
      { path: 'refs/a.md', content: '' },
    ];
    const groups = groupByFolder(files);
    expect(groups.map((g) => g.folder)).toEqual(['', 'refs']);
    expect(groups[0].entries[0].index).toBe(0);
    expect(groups[1].entries.map((e) => e.file.path)).toEqual(['refs/b.md', 'refs/a.md']);
  });
});
