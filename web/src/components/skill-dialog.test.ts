// @vitest-environment happy-dom
// Pinned tests for <rm-skill-dialog>. The dialog handles create AND
// edit; the same submit button (#skill-dialog-save) routes to POST
// or PATCH based on `editing`. The non-obvious bits are:
//
//   * parseSkillMd extracts description + body from a raw SKILL.md
//   * serializeSkillMd round-trips frontmatter exactly
//   * edit mode fetches the full Skill on open and seeds inputs from
//     the parsed frontmatter (preferring it over the SkillSummary's
//     server-computed description, which may be a cache snapshot)

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import './skill-dialog.js';
import {
  MAX_UPLOAD_BYTES_PER_FILE,
  isLikelyBinary,
  parseSkillMd,
  serializeSkillMd,
  validateSkillName,
  type SkillDialog,
} from './skill-dialog.js';
import type { Skill, SkillSummary } from '../api/client.js';

// ---------------------------------------------------------------------
// pure helpers
// ---------------------------------------------------------------------

describe('parseSkillMd', () => {
  it('extracts description from frontmatter; body is everything after the closing ---', () => {
    const raw = '---\nname: demo\ndescription: hello world\n---\n# body\nthings\n';
    const { description, body } = parseSkillMd(raw);
    expect(description).toBe('hello world');
    expect(body).toBe('# body\nthings\n');
  });

  it('returns empty description + raw body for content without frontmatter', () => {
    const raw = '# just a body\n';
    const { description, body } = parseSkillMd(raw);
    expect(description).toBe('');
    expect(body).toBe(raw);
  });

  it('returns empty description when frontmatter has no description key', () => {
    const raw = '---\nname: demo\n---\n# body\n';
    expect(parseSkillMd(raw).description).toBe('');
  });

  it('returns empty description when the closing --- is missing (malformed)', () => {
    const raw = '---\nname: demo\n# body that never closes frontmatter\n';
    const { description, body } = parseSkillMd(raw);
    expect(description).toBe('');
    expect(body).toBe(raw);
  });
});

describe('serializeSkillMd', () => {
  it('round-trips a parsed SKILL.md', () => {
    const raw = '---\nname: demo\ndescription: hello world\n---\n# body\nx\n';
    const { description, body } = parseSkillMd(raw);
    const out = serializeSkillMd('demo', description, body);
    // The header may use a slightly different layout (no trailing space,
    // canonical key order), so we assert on the data rather than the
    // exact string.
    expect(parseSkillMd(out).description).toBe('hello world');
    expect(parseSkillMd(out).body).toBe('# body\nx\n');
  });

  it('strips existing frontmatter from the body to avoid double-wrapping', () => {
    // User pasted a full SKILL.md into the body textarea by accident.
    const out = serializeSkillMd(
      'demo',
      'fresh',
      '---\nname: stale\ndescription: stale\n---\n# real body\n',
    );
    // Result has exactly ONE frontmatter block — `name: demo` not
    // `name: stale`, and the inner `---` from the stale block is gone.
    expect(out.match(/^---/gm)?.length).toBeLessThanOrEqual(2);
    expect(out).toContain('name: demo');
    expect(out).toContain('description: fresh');
    expect(out).toContain('# real body');
    expect(out).not.toContain('stale');
  });

  it('flattens newlines in description (single-line YAML value safety)', () => {
    const out = serializeSkillMd(
      'demo',
      'line1\nline2',
      '# body',
    );
    expect(out).toContain('description: line1 line2');
    expect(out).not.toContain('description: line1\nline2');
  });
});

// ---------------------------------------------------------------------
// DOM integration tests
// ---------------------------------------------------------------------

interface StubCall {
  url: string;
  method: string;
  body: Record<string, unknown> | null;
}

interface Stub {
  restore: () => void;
  calls: StubCall[];
  shouldFail: { status: number; message: string } | null;
  /** Pre-canned response for GET /api/v1/skills/{id} (edit-mode load). */
  skillDetail: Skill | null;
}

function installFetch(): Stub {
  const original = globalThis.fetch;
  const stub: Stub = {
    restore: () => {},
    calls: [],
    shouldFail: null,
    skillDetail: null,
  };
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = init?.method ?? 'GET';
    const body =
      init?.body != null ? JSON.parse(init.body as string) : null;
    stub.calls.push({ url, method, body });
    // Edit-mode GET — return the canned detail.
    if (/\/api\/v1\/skills\/[\w-]+$/.test(url) && method === 'GET') {
      if (!stub.skillDetail) {
        return Promise.resolve(new Response('not found', { status: 404 }));
      }
      return Promise.resolve(
        new Response(JSON.stringify(stub.skillDetail), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      );
    }
    // POST or PATCH — succeed unless shouldFail set.
    if (stub.shouldFail) {
      return Promise.resolve(
        new Response(
          JSON.stringify({ code: 'X', message: stub.shouldFail.message }),
          {
            status: stub.shouldFail.status,
            headers: { 'Content-Type': 'application/json' },
          },
        ),
      );
    }
    return Promise.resolve(
      new Response(
        JSON.stringify({
          id: 's-new',
          tenant_id: 't',
          name: body?.name ?? '',
          enabled: true,
          frontmatter_common: {},
          frontmatter_backend: {},
          files: body?.files ?? {},
          created_at: '',
          updated_at: '',
        }),
        {
          status: method === 'POST' ? 201 : 200,
          headers: { 'Content-Type': 'application/json' },
        },
      ),
    );
  }) as unknown as typeof fetch;
  stub.restore = () => {
    globalThis.fetch = original;
  };
  return stub;
}

async function settle(el: SkillDialog): Promise<void> {
  for (let i = 0; i < 25; i += 1) {
    await new Promise((r) => setTimeout(r, 0));
    await el.updateComplete;
  }
}

function mount(): SkillDialog {
  const el = document.createElement('rm-skill-dialog') as SkillDialog;
  document.body.appendChild(el);
  return el;
}

describe('<rm-skill-dialog>', () => {
  let stub: Stub;

  beforeEach(() => {
    stub = installFetch();
  });

  afterEach(() => {
    document
      .querySelectorAll('rm-skill-dialog')
      .forEach((el) => el.remove());
    stub.restore();
  });

  it('create mode: opens with empty inputs + default body, submit POSTs', async () => {
    const el = mount();
    el.editing = null;
    el.open = true;
    await settle(el);
    const nameInput = el.querySelector<HTMLInputElement>(
      '[data-testid="skill-dialog-name"]',
    )!;
    nameInput.value = 'fresh-skill';
    nameInput.dispatchEvent(new Event('input'));
    const descInput = el.querySelector<HTMLInputElement>(
      '[data-testid="skill-dialog-description"]',
    )!;
    descInput.value = 'a short description';
    descInput.dispatchEvent(new Event('input'));
    await settle(el);
    const saveBtn = el.querySelector<HTMLButtonElement>(
      '[data-testid="skill-dialog-save"]',
    )!;
    saveBtn.click();
    await settle(el);
    const post = stub.calls.find(
      (c) => c.method === 'POST' && c.url.endsWith('/api/v1/skills'),
    );
    expect(post, 'POST /api/v1/skills must fire').toBeTruthy();
    expect(post!.body!.name).toBe('fresh-skill');
    expect(
      (post!.body!.files as Record<string, string>)['SKILL.md'],
    ).toContain('description: a short description');
  });

  it('edit mode: fetches detail, seeds inputs from frontmatter, submit PATCHes', async () => {
    const existing: SkillSummary = {
      id: 's-9',
      tenant_id: 't',
      name: 'legacy-skill',
      description: 'stale desc',
      enabled: true,
      bound_coworker_count: 0,
      created_at: '',
      updated_at: '',
    } as SkillSummary;
    stub.skillDetail = {
      id: 's-9',
      tenant_id: 't',
      name: 'legacy-skill',
      enabled: true,
      frontmatter_common: {},
      frontmatter_backend: {},
      files: {
        'SKILL.md': {
          content:
            '---\nname: legacy-skill\ndescription: fresh from file\n---\n# Body\n',
          mime_type: 'text/markdown',
          updated_at: '',
        },
      },
      created_at: '',
      updated_at: '',
    } as Skill;
    const el = mount();
    el.editing = existing;
    el.open = true;
    await settle(el);
    // Description prefers the FILE-side frontmatter ("fresh from file")
    // over the SkillSummary ("stale desc"), because the summary value
    // can be a server cache snapshot.
    const descInput = el.querySelector<HTMLInputElement>(
      '[data-testid="skill-dialog-description"]',
    )!;
    expect(descInput.value).toBe('fresh from file');
    // Body strips the frontmatter and shows only the markdown.
    const bodyTextarea = el.querySelector<HTMLTextAreaElement>(
      '[data-testid="skill-dialog-body"]',
    )!;
    expect(bodyTextarea.value).toBe('# Body\n');

    // Submit PATCHes, not POSTs.
    const saveBtn = el.querySelector<HTMLButtonElement>(
      '[data-testid="skill-dialog-save"]',
    )!;
    saveBtn.click();
    await settle(el);
    const patch = stub.calls.find((c) => c.method === 'PATCH');
    expect(patch, 'PATCH /api/v1/skills/s-9 must fire').toBeTruthy();
    expect(patch!.url).toContain('/api/v1/skills/s-9');
    expect(
      stub.calls.find(
        (c) => c.method === 'POST' && c.url.endsWith('/api/v1/skills'),
      ),
      'POST must NOT fire in edit mode',
    ).toBeUndefined();
  });

  it('upload then remove drops the file from the tree', async () => {
    // PR26 removed the standalone "+ Add empty file" button; the only
    // way into extraFiles is now via upload. Pin the upload → remove
    // round-trip so a regression that breaks the remove icon (which
    // moved into the new inline tree) is caught.
    const el = mount();
    el.editing = null;
    el.open = true;
    await settle(el);
    const input = el.querySelector<HTMLInputElement>(
      '[data-testid="skill-dialog-pick-files"]',
    )!;
    Object.defineProperty(input, 'files', {
      value: [
        makeFile({ name: 'a.md', content: 'a' }),
        makeFile({ name: 'b.md', content: 'b' }),
      ],
      configurable: true,
    });
    input.dispatchEvent(new Event('change'));
    for (let i = 0; i < 50; i += 1) {
      await new Promise((r) => setTimeout(r, 0));
      await el.updateComplete;
      if (el.querySelectorAll('[data-testid="skill-dialog-file"]').length === 2) break;
    }
    expect(
      el.querySelectorAll('[data-testid="skill-dialog-file"]').length,
    ).toBe(2);
    // Click the first row's remove button.
    const removeBtn = el.querySelectorAll<HTMLButtonElement>(
      '.rm-iconbtn--danger',
    )[0];
    removeBtn.click();
    await settle(el);
    expect(
      el.querySelectorAll('[data-testid="skill-dialog-file"]').length,
    ).toBe(1);
  });

  // PR27 removed the per-file rename input — paths are now display-only
  // text. The previous "blocks save with an invalid file path" and
  // "rejects SKILL.md as an extra filename" tests exercised that
  // input-driven path; with the input gone, the UI can no longer
  // reach those code paths. The underlying defenses live elsewhere:
  //   * ingestUploads gates incoming paths via isValidSkillFilePath
  //     and a SKILL.md-shadow check (covered by the "rejects an upload
  //     at the reserved SKILL.md path" + binary / size cap tests).
  //   * save() still has belt-and-braces checks for both cases — they
  //     fire if something mutates extraFiles bypassing the gates, but
  //     that's no longer reachable via the UI.

  // ---------------------------------------------------------------------
  // PR20: live-validation gating + backend error routing
  // ---------------------------------------------------------------------

  it('disables Save when the name fails the kebab regex (live)', async () => {
    const el = mount();
    el.editing = null;
    el.open = true;
    await settle(el);
    const nameInput = el.querySelector<HTMLInputElement>(
      '[data-testid="skill-dialog-name"]',
    )!;
    nameInput.value = 'Has Upper';
    nameInput.dispatchEvent(new Event('input'));
    const descInput = el.querySelector<HTMLInputElement>(
      '[data-testid="skill-dialog-description"]',
    )!;
    descInput.value = 'fine';
    descInput.dispatchEvent(new Event('input'));
    await settle(el);
    const saveBtn = el.querySelector<HTMLButtonElement>(
      '[data-testid="skill-dialog-save"]',
    )!;
    expect(saveBtn.disabled).toBe(true);
    // The inline error appears in place of the helper hint.
    const err = el.querySelector('[data-testid="skill-dialog-name-error"]');
    expect(err).toBeTruthy();
    expect(err!.textContent).toContain('Lowercase');
    // And clicking it doesn't trigger a POST as a final safety check
    // (keyboard-Enter could route around the disabled-button gate).
    saveBtn.click();
    await settle(el);
    expect(
      stub.calls.find((c) => c.method === 'POST'),
      'POST must not fire when name validation fails',
    ).toBeUndefined();
  });

  it('disables Save when the name matches a reserved word', async () => {
    const el = mount();
    el.editing = null;
    el.open = true;
    await settle(el);
    const nameInput = el.querySelector<HTMLInputElement>(
      '[data-testid="skill-dialog-name"]',
    )!;
    // Reserved by the Claude runtime; frontend rejects before backend
    // sees the request so the user gets a fast, specific error rather
    // than a 422 with no field-level mapping.
    nameInput.value = 'anthropic';
    nameInput.dispatchEvent(new Event('input'));
    const descInput = el.querySelector<HTMLInputElement>(
      '[data-testid="skill-dialog-description"]',
    )!;
    descInput.value = 'fine';
    descInput.dispatchEvent(new Event('input'));
    await settle(el);
    const saveBtn = el.querySelector<HTMLButtonElement>(
      '[data-testid="skill-dialog-save"]',
    )!;
    expect(saveBtn.disabled).toBe(true);
    const err = el.querySelector('[data-testid="skill-dialog-name-error"]');
    expect(err?.textContent).toContain('reserved');
  });

  it('disables Save when description is empty even with valid name', async () => {
    const el = mount();
    el.editing = null;
    el.open = true;
    await settle(el);
    const nameInput = el.querySelector<HTMLInputElement>(
      '[data-testid="skill-dialog-name"]',
    )!;
    nameInput.value = 'good-name';
    nameInput.dispatchEvent(new Event('input'));
    await settle(el);
    const saveBtn = el.querySelector<HTMLButtonElement>(
      '[data-testid="skill-dialog-save"]',
    )!;
    expect(saveBtn.disabled).toBe(true);
  });

  it('disables Save when description exceeds the 1024-char cap', async () => {
    const el = mount();
    el.editing = null;
    el.open = true;
    await settle(el);
    const nameInput = el.querySelector<HTMLInputElement>(
      '[data-testid="skill-dialog-name"]',
    )!;
    nameInput.value = 'good-name';
    nameInput.dispatchEvent(new Event('input'));
    const descInput = el.querySelector<HTMLInputElement>(
      '[data-testid="skill-dialog-description"]',
    )!;
    descInput.value = 'x'.repeat(1025);
    descInput.dispatchEvent(new Event('input'));
    await settle(el);
    const saveBtn = el.querySelector<HTMLButtonElement>(
      '[data-testid="skill-dialog-save"]',
    )!;
    expect(saveBtn.disabled).toBe(true);
    // Counter shows the overflow value (1025 / 1024) so the user can see
    // why they're blocked.
    const counter = el.querySelector(
      '[data-testid="skill-dialog-desc-counter"]',
    );
    expect(counter?.textContent).toContain('1025');
  });

  it('routes a backend INVALID_NAME error to the name input', async () => {
    const el = mount();
    el.editing = null;
    el.open = true;
    await settle(el);
    // Make the POST return a code-tagged 422. The dialog should paint
    // the error next to the name input, not as a generic banner.
    stub.shouldFail = { status: 422, message: 'bad name' };
    // Override the response shape so the body includes the code field
    // we route on.
    const original = globalThis.fetch;
    globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = init?.method ?? 'GET';
      stub.calls.push({
        url,
        method,
        body: init?.body != null ? JSON.parse(init.body as string) : null,
      });
      return Promise.resolve(
        new Response(
          JSON.stringify({ code: 'INVALID_NAME', message: 'bad name' }),
          { status: 422, headers: { 'Content-Type': 'application/json' } },
        ),
      );
    }) as unknown as typeof fetch;
    const nameInput = el.querySelector<HTMLInputElement>(
      '[data-testid="skill-dialog-name"]',
    )!;
    nameInput.value = 'valid-name';
    nameInput.dispatchEvent(new Event('input'));
    const descInput = el.querySelector<HTMLInputElement>(
      '[data-testid="skill-dialog-description"]',
    )!;
    descInput.value = 'fine';
    descInput.dispatchEvent(new Event('input'));
    await settle(el);
    const saveBtn = el.querySelector<HTMLButtonElement>(
      '[data-testid="skill-dialog-save"]',
    )!;
    saveBtn.click();
    await settle(el);
    globalThis.fetch = original;
    const nameErr = el.querySelector(
      '[data-testid="skill-dialog-name-error"]',
    );
    expect(nameErr?.textContent).toContain('bad name');
  });

  it('hides Additional files behind a collapsed disclosure on create', async () => {
    const el = mount();
    el.editing = null;
    el.open = true;
    await settle(el);
    const details = el.querySelector('details');
    expect(details).toBeTruthy();
    // Create-mode default is collapsed — the easy-first path doesn't
    // bother the user with multi-file scaffolding.
    expect((details as HTMLDetailsElement).open).toBe(false);
  });

  it('opens the disclosure automatically on edit when the skill has extra files', async () => {
    const existing: SkillSummary = {
      id: 's-multi',
      tenant_id: 't',
      name: 'multi-file',
      description: 'has refs',
      enabled: true,
      bound_coworker_count: 0,
      created_at: '',
      updated_at: '',
    } as SkillSummary;
    stub.skillDetail = {
      id: 's-multi',
      tenant_id: 't',
      name: 'multi-file',
      enabled: true,
      frontmatter_common: {},
      frontmatter_backend: {},
      files: {
        'SKILL.md': {
          path: 'SKILL.md',
          content: '---\nname: multi-file\ndescription: has refs\n---\nbody',
          mime_type: 'text/markdown',
          updated_at: '',
        },
        'reference.md': {
          path: 'reference.md',
          content: 'data',
          mime_type: 'text/markdown',
          updated_at: '',
        },
      } as unknown as Skill['files'],
      created_at: '',
      updated_at: '',
    } as Skill;
    const el = mount();
    el.editing = existing;
    el.open = true;
    await settle(el);
    const details = el.querySelector('details');
    expect((details as HTMLDetailsElement).open).toBe(true);
  });
});

// ---------------------------------------------------------------------
// PR20: validateSkillName unit boundary table
// ---------------------------------------------------------------------

describe('validateSkillName', () => {
  // Accept table
  it.each([
    ['code-review', 'canonical kebab'],
    ['a', 'single char'],
    ['1st-skill', 'leading digit allowed'],
    ['a'.repeat(64), 'exact upper bound'],
  ])('accepts %s (%s)', (name) => {
    expect(validateSkillName(name)).toBeNull();
  });

  // Reject table — each row pairs the input with the substring the
  // returned error must contain. Pinning the substring catches a
  // future refactor that merges all errors into a generic "invalid"
  // (which would lose the per-case actionability).
  it.each([
    ['Has Upper', 'Lowercase'],
    ['has_underscore', 'Lowercase'],
    ['-leading-dash', 'leading hyphen'],
    ['a'.repeat(65), 'Lowercase'],
    ['name.with.dot', 'Lowercase'],
    ['anthropic', 'reserved'],
    ['claude', 'reserved'],
  ])('rejects %s with hint containing %s', (name, hint) => {
    const msg = validateSkillName(name);
    expect(msg, `validateSkillName(${name}) returned null`).not.toBeNull();
    expect(msg!.toLowerCase()).toContain(hint.toLowerCase());
  });

  it('treats the empty string as "not yet typed" (no error)', () => {
    // The dialog suppresses live errors before first input — pinning
    // this contract so a future "stricter validation" doesn't flash
    // red the moment the dialog opens.
    expect(validateSkillName('')).toBeNull();
  });
});

// ---------------------------------------------------------------------
// PR21: pure helpers — isLikelyBinary, stripLeadingFolder
// ---------------------------------------------------------------------

describe('isLikelyBinary', () => {
  it('returns false for pure text', () => {
    expect(isLikelyBinary('hello world\nmore lines\n')).toBe(false);
  });

  it('returns true when a NUL byte appears in the first 4KB', () => {
    expect(isLikelyBinary('abc\0def')).toBe(true);
  });

  it('returns true even when the NUL is at the boundary', () => {
    // Exactly at index 0 — catches the off-by-one in any future
    // "skip first byte" optimization.
    expect(isLikelyBinary('\0rest')).toBe(true);
  });

  it('does not scan past the 4KB window', () => {
    // Pin the perf contract: a 10MB string with a NUL at the very
    // end must NOT be flagged binary (we only look at first 4KB).
    // The test exists to catch a future refactor that drops the
    // window optimization and starts O(n) scanning huge texts.
    const big = 'a'.repeat(5_000_000) + '\0' + 'b'.repeat(5_000_000);
    expect(isLikelyBinary(big)).toBe(false);
  });
});

// PR27 follow-up: stripLeadingFolder removed entirely. The folder
// picker (PR21) used to strip the user-picked root folder name on the
// assumption that pick = "the entire skill folder", but user feedback
// showed the assumption was wrong — users typically pick individual
// subfolders (`references/`, `scripts/`) and expect those names to
// survive. Now both drag-drop and folder picker preserve every level
// of the picked structure.

// ---------------------------------------------------------------------
// PR21: upload UX (drag-drop, pickers, size + binary gates, conflict
// silent-replace + toast)
// ---------------------------------------------------------------------

interface FakeFileOptions {
  name: string;
  content: string;
  /** Mimics the folder picker's path metadata (`folder/sub/file.md`). */
  relativePath?: string;
  /** Override the size separately from the content length (for
   *  oversize tests where the synthetic content is short but we
   *  want to trip the cap). */
  size?: number;
}

function makeFile(opts: FakeFileOptions): File {
  // happy-dom's File constructor honors `name` and exposes `size`
  // from the blob parts. We monkey-patch webkitRelativePath after
  // construction because the constructor doesn't accept it.
  const f = new File([opts.content], opts.name, {
    type: 'text/plain',
  });
  if (opts.size !== undefined) {
    Object.defineProperty(f, 'size', { value: opts.size, configurable: true });
  }
  if (opts.relativePath !== undefined) {
    Object.defineProperty(f, 'webkitRelativePath', {
      value: opts.relativePath,
      configurable: true,
    });
  }
  return f;
}

/** Build a FileList-like wrapper that the pickers' onChange accepts.
 *  happy-dom doesn't expose a FileList constructor, but the dialog
 *  reads input.files via Array.from(list) which only needs a
 *  Symbol.iterator + numeric indices. A plain array already has
 *  both; we just cast through unknown to satisfy TS. */
function asFileList(files: File[]): FileList {
  return files.slice() as unknown as FileList;
}

describe('skill-dialog: upload via file picker', () => {
  let stub: Stub;
  beforeEach(() => {
    stub = installFetch();
  });
  afterEach(() => {
    stub.restore();
    document.body.innerHTML = '';
  });

  it('reads file content, normalizes path, and shows the row in the tree', async () => {
    const el = mount();
    el.editing = null;
    el.open = true;
    await settle(el);
    const input = el.querySelector<HTMLInputElement>(
      '[data-testid="skill-dialog-pick-files"]',
    )!;
    const file = makeFile({ name: 'note.md', content: '# hello\n' });
    Object.defineProperty(input, 'files', {
      value: asFileList([file]),
      configurable: true,
    });
    input.dispatchEvent(new Event('change'));
    // FileReader uses a macrotask in happy-dom; flush via setTimeout
    // because awaiting `Promise.resolve()` only drains microtasks.
    for (let i = 0; i < 50; i += 1) {
      await new Promise((r) => setTimeout(r, 0));
      await el.updateComplete;
      if (el.querySelector('[data-testid="skill-dialog-file"]')) break;
    }
    // PR27: per-file path is now a <span> (read-only), not an input.
    const rows = el.querySelectorAll<HTMLElement>(
      '[data-testid="skill-dialog-file"]',
    );
    expect(rows.length).toBe(1);
    expect(rows[0].textContent?.trim()).toBe('note.md');
  });

  it('preserves the picked folder name from the folder picker', async () => {
    // PR27 follow-up: user picks `references/` containing files —
    // expects the `references/` name to survive. Before this fix
    // the code stripped the top-level pick on the assumption that
    // the user was picking "the entire skill folder"; user feedback
    // showed that assumption was wrong for the common case.
    const el = mount();
    el.editing = null;
    el.open = true;
    await settle(el);
    const input = el.querySelector<HTMLInputElement>(
      '[data-testid="skill-dialog-pick-folder"]',
    )!;
    // Folder picker exposes "<pickedFolder>/file.md" in
    // webkitRelativePath. Path stored = the full relative path,
    // unchanged.
    const files = [
      makeFile({
        name: 'intro.md',
        content: '# intro\n',
        relativePath: 'references/intro.md',
      }),
      makeFile({
        name: 'glossary.md',
        content: 'gloss',
        relativePath: 'references/glossary.md',
      }),
    ];
    Object.defineProperty(input, 'files', {
      value: asFileList(files),
      configurable: true,
    });
    input.dispatchEvent(new Event('change'));
    for (let i = 0; i < 50; i += 1) {
      await new Promise((r) => setTimeout(r, 0));
      await el.updateComplete;
      if (
        el.querySelectorAll('[data-testid="skill-dialog-file"]').length >= 2
      ) break;
    }
    // Folder header must surface the picked folder name.
    const treeText = el
      .querySelector('[data-testid="skill-dialog-folder-tree"]')!
      .textContent ?? '';
    expect(treeText).toContain('references/');
    // File names render as basenames under the folder group.
    const fileTexts = [
      ...el.querySelectorAll<HTMLElement>(
        '[data-testid="skill-dialog-file"]',
      ),
    ].map((s) => s.textContent?.trim() ?? '');
    expect(fileTexts.sort()).toEqual(['glossary.md', 'intro.md']);
  });

  it('rejects a binary file (NUL byte) and emits a toast tally', async () => {
    const el = mount();
    el.editing = null;
    el.open = true;
    await settle(el);
    const input = el.querySelector<HTMLInputElement>(
      '[data-testid="skill-dialog-pick-files"]',
    )!;
    const file = makeFile({ name: 'image.bin', content: 'abc\0def' });
    Object.defineProperty(input, 'files', {
      value: asFileList([file]),
      configurable: true,
    });
    input.dispatchEvent(new Event('change'));
    for (let i = 0; i < 50; i += 1) {
      await new Promise((r) => setTimeout(r, 0));
      await el.updateComplete;
      if (
        el.querySelector('[data-testid="skill-dialog-upload-toast"]')
      ) break;
    }
    expect(
      el.querySelectorAll('[data-testid="skill-dialog-file"]').length,
      'binary file must not appear in the tree',
    ).toBe(0);
    const toast = el.querySelector('[data-testid="skill-dialog-upload-toast"]');
    expect(toast?.textContent).toContain('binary');
  });

  it('rejects a file over the per-file size cap and tallies in toast', async () => {
    const el = mount();
    el.editing = null;
    el.open = true;
    await settle(el);
    const input = el.querySelector<HTMLInputElement>(
      '[data-testid="skill-dialog-pick-files"]',
    )!;
    // Small synthetic content but size monkey-patched to exceed the
    // cap; reader will succeed but ingest gate rejects.
    const file = makeFile({
      name: 'huge.log',
      content: 'short',
      size: MAX_UPLOAD_BYTES_PER_FILE + 1,
    });
    Object.defineProperty(input, 'files', {
      value: asFileList([file]),
      configurable: true,
    });
    input.dispatchEvent(new Event('change'));
    for (let i = 0; i < 50; i += 1) {
      await new Promise((r) => setTimeout(r, 0));
      await el.updateComplete;
      if (
        el.querySelector('[data-testid="skill-dialog-upload-toast"]')
      ) break;
    }
    expect(
      el.querySelectorAll('[data-testid="skill-dialog-file"]').length,
    ).toBe(0);
    const toast = el.querySelector('[data-testid="skill-dialog-upload-toast"]');
    expect(toast?.textContent).toContain('size cap');
  });

  it('silently replaces an existing path on conflict and tallies "replaced" in toast', async () => {
    const el = mount();
    el.editing = null;
    el.open = true;
    await settle(el);
    // First upload — establishes the row.
    const input = el.querySelector<HTMLInputElement>(
      '[data-testid="skill-dialog-pick-files"]',
    )!;
    Object.defineProperty(input, 'files', {
      value: asFileList([
        makeFile({ name: 'note.md', content: 'v1\n' }),
      ]),
      configurable: true,
    });
    input.dispatchEvent(new Event('change'));
    for (let i = 0; i < 50; i += 1) {
      await new Promise((r) => setTimeout(r, 0));
      await el.updateComplete;
      if (el.querySelector('[data-testid="skill-dialog-file"]')) break;
    }
    // Second upload at the same path with new content.
    Object.defineProperty(input, 'files', {
      value: asFileList([
        makeFile({ name: 'note.md', content: 'v2 (replaced)\n' }),
      ]),
      configurable: true,
    });
    input.dispatchEvent(new Event('change'));
    for (let i = 0; i < 50; i += 1) {
      await new Promise((r) => setTimeout(r, 0));
      await el.updateComplete;
      const t = el.querySelector('[data-testid="skill-dialog-upload-toast"]');
      if (t?.textContent?.includes('replaced')) break;
    }
    // PR27: per-file path is a <span> now; count by element rather
    // than typing as HTMLInputElement.
    const rows = el.querySelectorAll<HTMLElement>(
      '[data-testid="skill-dialog-file"]',
    );
    // Still ONE row; content was replaced in place, not duplicated.
    expect(rows.length).toBe(1);
    const toast = el.querySelector('[data-testid="skill-dialog-upload-toast"]');
    expect(toast?.textContent).toContain('replaced');
  });

  it('rejects an upload at the reserved SKILL.md path with a clear toast', async () => {
    const el = mount();
    el.editing = null;
    el.open = true;
    await settle(el);
    const input = el.querySelector<HTMLInputElement>(
      '[data-testid="skill-dialog-pick-files"]',
    )!;
    Object.defineProperty(input, 'files', {
      value: asFileList([
        makeFile({ name: 'SKILL.md', content: '# Smuggled\n' }),
      ]),
      configurable: true,
    });
    input.dispatchEvent(new Event('change'));
    for (let i = 0; i < 50; i += 1) {
      await new Promise((r) => setTimeout(r, 0));
      await el.updateComplete;
      const t = el.querySelector('[data-testid="skill-dialog-upload-toast"]');
      if (t) break;
    }
    expect(
      el.querySelectorAll('[data-testid="skill-dialog-file"]').length,
      'SKILL.md must not be added as an extra file (would shadow main)',
    ).toBe(0);
    const toast = el.querySelector('[data-testid="skill-dialog-upload-toast"]');
    expect(toast?.textContent).toContain('SKILL.md');
  });
});

describe('skill-dialog: PATCH edit body shape', () => {
  let stub: Stub;
  beforeEach(() => {
    stub = installFetch();
  });
  afterEach(() => {
    stub.restore();
    document.body.innerHTML = '';
  });

  it('omits `name` from the PATCH body (backend treats name as immutable)', async () => {
    // Pre-PR21 the dialog sent {name, enabled, files} which would now
    // produce a no-op match against the existing name (legal but
    // verbose). The new contract is to omit name entirely so a future
    // backend tightening that rejects ANY name in PATCH still works.
    const existing: SkillSummary = {
      id: 's-noname',
      tenant_id: 't',
      name: 'pre-existing',
      description: 'd',
      enabled: true,
      bound_coworker_count: 0,
      created_at: '',
      updated_at: '',
    } as SkillSummary;
    stub.skillDetail = {
      id: 's-noname',
      tenant_id: 't',
      name: 'pre-existing',
      enabled: true,
      frontmatter_common: {},
      frontmatter_backend: {},
      files: {
        'SKILL.md': {
          path: 'SKILL.md',
          content: '---\nname: pre-existing\ndescription: d\n---\nbody\n',
          mime_type: 'text/markdown',
          updated_at: '',
        },
      } as unknown as Skill['files'],
      created_at: '',
      updated_at: '',
    } as Skill;
    const el = mount();
    el.editing = existing;
    el.open = true;
    await settle(el);
    const saveBtn = el.querySelector<HTMLButtonElement>(
      '[data-testid="skill-dialog-save"]',
    )!;
    saveBtn.click();
    await settle(el);
    const patch = stub.calls.find((c) => c.method === 'PATCH');
    expect(patch, 'PATCH must fire').toBeTruthy();
    expect(
      'name' in (patch!.body ?? {}),
      'name must NOT be in the PATCH body',
    ).toBe(false);
    expect(patch!.body!.files).toBeDefined();
  });
});

// ---------------------------------------------------------------------
// PR25: "Your skill folder" snapshot — read-only orientation
// ---------------------------------------------------------------------

describe('skill-dialog: inline folder tree (PR26)', () => {
  let stub: Stub;
  beforeEach(() => {
    stub = installFetch();
  });
  afterEach(() => {
    stub.restore();
    document.body.innerHTML = '';
  });

  it('renders SKILL.md row with annotation even before any upload', async () => {
    // The inline tree replaces the PR25 top card. SKILL.md is ALWAYS
    // present in the tree (read-only, muted) so the user knows where
    // their Instructions textarea content lands relative to uploads.
    const el = mount();
    el.editing = null;
    el.open = true;
    await settle(el);
    const tree = el.querySelector(
      '[data-testid="skill-dialog-folder-tree"]',
    );
    expect(tree, 'inline tree must always render').toBeTruthy();
    const skillRow = el.querySelector(
      '[data-testid="skill-dialog-tree-skill-md"]',
    );
    expect(skillRow, 'SKILL.md row must always be present').toBeTruthy();
    // The annotation explicitly cross-references the Instructions
    // textarea above; without that text the row would look like a
    // mysterious always-on file the user can't act on.
    expect(skillRow!.textContent).toContain('from Instructions');
  });

  it('top of dialog has no card-style folder snapshot (PR25 card removed)', async () => {
    // PR25 had a bordered card at the very top of the dialog with
    // testid 'skill-dialog-folder-snapshot'. PR26 removed it after
    // user feedback that the card looked too much like an input.
    // If a future PR reintroduces a top-positioned snapshot under
    // any obvious name, this test should fail and force a rethink.
    const el = mount();
    el.editing = null;
    el.open = true;
    await settle(el);
    expect(
      el.querySelector('[data-testid="skill-dialog-folder-snapshot"]'),
      'PR25 top card must stay deleted',
    ).toBeNull();
  });

  it('inline tree updates live with the SKILL.md row preserved on upload', async () => {
    const el = mount();
    el.editing = null;
    el.open = true;
    await settle(el);
    const input = el.querySelector<HTMLInputElement>(
      '[data-testid="skill-dialog-pick-files"]',
    )!;
    Object.defineProperty(input, 'files', {
      value: [makeFile({ name: 'intro.md', content: '# Intro\n' })],
      configurable: true,
    });
    input.dispatchEvent(new Event('change'));
    for (let i = 0; i < 50; i += 1) {
      await new Promise((r) => setTimeout(r, 0));
      await el.updateComplete;
      if (el.querySelector('[data-testid="skill-dialog-file"]')) break;
    }
    const tree = el.querySelector(
      '[data-testid="skill-dialog-folder-tree"]',
    )!;
    // SKILL.md is rendered as text inside the tree's skill-md row.
    expect(tree.textContent).toContain('SKILL.md');
    // PR27: per-file path is now a <span>; read textContent.
    const filenames = [
      ...tree.querySelectorAll<HTMLElement>(
        '[data-testid="skill-dialog-file"]',
      ),
    ].map((s) => s.textContent?.trim() ?? '');
    expect(filenames).toContain('intro.md');
  });

  it('inline tree groups uploads by folder', async () => {
    const el = mount();
    el.editing = null;
    el.open = true;
    await settle(el);
    const input = el.querySelector<HTMLInputElement>(
      '[data-testid="skill-dialog-pick-folder"]',
    )!;
    Object.defineProperty(input, 'files', {
      value: [
        makeFile({
          name: 'intro.md', content: 'intro',
          relativePath: 'references/intro.md',
        }),
        makeFile({
          name: 'glossary.md', content: 'gloss',
          relativePath: 'references/glossary.md',
        }),
      ],
      configurable: true,
    });
    input.dispatchEvent(new Event('change'));
    for (let i = 0; i < 50; i += 1) {
      await new Promise((r) => setTimeout(r, 0));
      await el.updateComplete;
      if (el.querySelectorAll('[data-testid="skill-dialog-file"]').length >= 2) break;
    }
    const tree = el.querySelector(
      '[data-testid="skill-dialog-folder-tree"]',
    )!;
    // Folder header renders as text (a <div>references/</div>); the
    // text content check works for it.
    expect(tree.textContent).toContain('references/');
    // PR27: per-file path is now a <span>; read textContent.
    const filenames = [
      ...tree.querySelectorAll<HTMLElement>(
        '[data-testid="skill-dialog-file"]',
      ),
    ].map((s) => s.textContent?.trim() ?? '');
    expect(filenames).toContain('intro.md');
    expect(filenames).toContain('glossary.md');
  });

  it('+ Add empty file button no longer exists (PR26 removed it)', async () => {
    // Pin the deletion: future PR cycles that reintroduce a way to
    // create empty placeholder files without uploading should fail
    // here and prompt a UX rediscussion.
    const el = mount();
    el.editing = null;
    el.open = true;
    await settle(el);
    expect(
      el.querySelector('[data-testid="skill-dialog-add-file"]'),
      '+ Add empty file button must stay removed',
    ).toBeNull();
  });

  it('per-file path renders as read-only text, not an input (PR27)', async () => {
    // PR27 removed the rename affordance — paths are display-only.
    // Upload one file, then assert the row's path-bearing element is
    // a <span>, not an <input>. A regression that re-introduces the
    // input would let users rename files in the dialog AND would
    // re-open the validation gap that the previous "blocks save"
    // tests covered.
    const el = mount();
    el.editing = null;
    el.open = true;
    await settle(el);
    const picker = el.querySelector<HTMLInputElement>(
      '[data-testid="skill-dialog-pick-files"]',
    )!;
    Object.defineProperty(picker, 'files', {
      value: [makeFile({ name: 'ok.md', content: 'fine' })],
      configurable: true,
    });
    picker.dispatchEvent(new Event('change'));
    for (let i = 0; i < 50; i += 1) {
      await new Promise((r) => setTimeout(r, 0));
      await el.updateComplete;
      if (el.querySelector('[data-testid="skill-dialog-file"]')) break;
    }
    const row = el.querySelector('[data-testid="skill-dialog-file"]')!;
    expect(
      row.tagName.toLowerCase(),
      'per-file path must be a <span>, not <input>',
    ).toBe('span');
  });
});

// ---------------------------------------------------------------------
// PR27: drag-drop folder structure preservation
// ---------------------------------------------------------------------

describe('skill-dialog: drag-drop folder structure (PR27)', () => {
  let stub: Stub;
  beforeEach(() => {
    stub = installFetch();
  });
  afterEach(() => {
    stub.restore();
    document.body.innerHTML = '';
  });

  // Build a fake FileSystemEntry tree for testing the drag-drop
  // walk(). happy-dom doesn't ship webkitGetAsEntry, so we wire up
  // the DataTransfer items by hand. Two helpers:
  //   makeFakeFileEntry(name, content)
  //   makeFakeDirEntry(name, children)
  // Each conforms to the structural FileSystemEntryLike interface
  // the dialog's readEntries expects.
  interface FakeEntry {
    isFile: boolean;
    isDirectory: boolean;
    name: string;
    file?: (cb: (f: File) => void, errcb?: (e: unknown) => void) => void;
    createReader?: () => {
      readEntries: (
        cb: (entries: FakeEntry[]) => void,
        errcb?: (e: unknown) => void,
      ) => void;
    };
  }

  function makeFakeFileEntry(name: string, content: string): FakeEntry {
    return {
      isFile: true,
      isDirectory: false,
      name,
      file: (cb) => {
        cb(new File([content], name, { type: 'text/plain' }));
      },
    };
  }

  function makeFakeDirEntry(name: string, children: FakeEntry[]): FakeEntry {
    return {
      isFile: false,
      isDirectory: true,
      name,
      createReader: () => {
        let delivered = false;
        return {
          readEntries: (cb) => {
            // Per spec, readEntries returns batches; eventually
            // empty. We deliver everything once, then empty.
            if (delivered) {
              cb([]);
              return;
            }
            delivered = true;
            cb(children);
          },
        };
      },
    };
  }

  function fakeDataTransferItems(entries: FakeEntry[]): DataTransferItemList {
    const items = entries.map((entry) => ({
      kind: 'file' as const,
      type: 'application/octet-stream',
      getAsFile: () => null,
      getAsString: () => undefined,
      webkitGetAsEntry: () => entry,
    }));
    return items as unknown as DataTransferItemList;
  }

  it('drag-drop of a folder with files at root preserves the folder name', async () => {
    // Bug PR27 fixed: walk() was calling stripLeadingFolder on the
    // result path, which flattened the top-level folder. So a drop
    // of `references/` containing `intro.md` ended up as `intro.md`
    // at the catalog root rather than `references/intro.md`.
    const el = mount();
    el.editing = null;
    el.open = true;
    await settle(el);

    const dropzone = el.querySelector('[data-testid="skill-dialog-dropzone"]')!;
    const referencesDir = makeFakeDirEntry('references', [
      makeFakeFileEntry('intro.md', '# intro\n'),
    ]);
    const ev = new DragEvent('drop', { bubbles: true, cancelable: true });
    Object.defineProperty(ev, 'dataTransfer', {
      value: {
        items: fakeDataTransferItems([referencesDir]),
      },
      configurable: true,
    });
    dropzone.dispatchEvent(ev);

    for (let i = 0; i < 80; i += 1) {
      await new Promise((r) => setTimeout(r, 0));
      await el.updateComplete;
      const t = el.querySelector(
        '[data-testid="skill-dialog-folder-tree"]',
      );
      if (t?.textContent?.includes('references/')) break;
    }
    const tree = el.querySelector(
      '[data-testid="skill-dialog-folder-tree"]',
    )!;
    // Folder header must appear — pre-PR27 it didn't because the
    // path was flat 'intro.md' so the grouping put it under the ''
    // (root) key with no header rendered.
    expect(tree.textContent).toContain('references/');
    // File still shows up under it as basename.
    const fileNames = [
      ...tree.querySelectorAll<HTMLElement>(
        '[data-testid="skill-dialog-file"]',
      ),
    ].map((s) => s.textContent?.trim());
    expect(fileNames).toContain('intro.md');
  });

  it('drag-drop of nested folders preserves the full path', async () => {
    // `parent/sub/deep.md` — multi-level structure.
    const el = mount();
    el.editing = null;
    el.open = true;
    await settle(el);
    const dropzone = el.querySelector('[data-testid="skill-dialog-dropzone"]')!;
    const parent = makeFakeDirEntry('parent', [
      makeFakeDirEntry('sub', [makeFakeFileEntry('deep.md', 'hi')]),
    ]);
    const ev = new DragEvent('drop', { bubbles: true, cancelable: true });
    Object.defineProperty(ev, 'dataTransfer', {
      value: { items: fakeDataTransferItems([parent]) },
      configurable: true,
    });
    dropzone.dispatchEvent(ev);
    for (let i = 0; i < 80; i += 1) {
      await new Promise((r) => setTimeout(r, 0));
      await el.updateComplete;
      const t = el.querySelector(
        '[data-testid="skill-dialog-folder-tree"]',
      );
      if (t?.textContent?.includes('parent')) break;
    }
    const tree = el.querySelector(
      '[data-testid="skill-dialog-folder-tree"]',
    )!;
    // The grouping is one level deep visually — files inside
    // `parent/sub/` cluster under the `parent/` header with their
    // relative-to-parent path as the basename ("sub/deep.md").
    expect(tree.textContent).toContain('parent/');
    const fileNames = [
      ...tree.querySelectorAll<HTMLElement>(
        '[data-testid="skill-dialog-file"]',
      ),
    ].map((s) => s.textContent?.trim());
    expect(fileNames).toContain('sub/deep.md');
  });
});

// ---------------------------------------------------------------------
// PR25: dialog primitive — sticky footer + scrollable body
// ---------------------------------------------------------------------

describe('rm-dialog: layout', () => {
  afterEach(() => {
    document.body.innerHTML = '';
  });

  it('renders header + body + footer as three flex children', async () => {
    // Pin the structural contract: a tall body must not push the
    // footer out of the dialog — the CSS layer guarantees this via
    // max-height + flex column + flex-shrink:0 on header/footer.
    // happy-dom doesn't run layout, so we can't measure heights,
    // but we CAN verify the structure that makes the CSS guarantee
    // possible: header (.hd), body (.body), footer (.foot) all
    // exist as direct children of <dialog>. A regression that
    // collapses footer into body — losing the sticky behavior —
    // fails this test.
    const el = document.createElement('rm-dialog');
    (el as unknown as { title: string }).title = 'Test';
    el.setAttribute('title', 'Test');
    document.body.appendChild(el);
    (el as unknown as { open: boolean }).open = true;
    await (el as unknown as { updateComplete: Promise<unknown> })
      .updateComplete;
    // shadowRoot host because dialog has its own scope.
    const root = (el as unknown as { shadowRoot: ShadowRoot }).shadowRoot;
    expect(root.querySelector('.hd')).toBeTruthy();
    expect(root.querySelector('.body')).toBeTruthy();
    expect(root.querySelector('.foot')).toBeTruthy();
  });

  it('scopes the layout display:flex to [open] (regression: dialog appeared on page mount)', async () => {
    // PR25 originally put `display: flex` on bare `dialog { ... }`.
    // The author-origin rule overrode the UA's
    // `dialog:not([open]) { display: none }` (cascade origin beats
    // specificity for the same property) and every dialog rendered
    // visible the moment its host page mounted — coworkers, MCP
    // servers, skills, and credentials pages all popped their
    // create dialog with no user action.
    //
    // The fix: layout properties (display, flex-direction, max-height)
    // live under `dialog[open] { ... }` so they only apply when the
    // dialog is actually meant to be visible. happy-dom doesn't
    // implement the UA stylesheet faithfully enough for a "is it
    // visible" assertion to catch this, so we instead read the
    // raw styles text and pin the structural contract: the bare
    // `dialog` rule MUST NOT carry display:flex.
    //
    // This is brittle by design — a future PR that "tidies up" by
    // moving display:flex back to bare `dialog` should fail this
    // test loudly.
    const { RmDialog } = await import('./dialog.js');
    const stylesText = String((RmDialog as { styles: unknown }).styles);
    // Find the section between `dialog {` and the next `}`. The
    // [^{}]* pattern catches the block contents; we then check
    // 'display:' isn't in there.
    const bareMatch = stylesText.match(
      /(?:^|\n)\s*dialog\s*\{([^{}]*)\}/m,
    );
    expect(
      bareMatch,
      'expected to find a bare `dialog { ... }` rule for non-layout props',
    ).toBeTruthy();
    const bareBlock = (bareMatch![1] ?? '').toLowerCase();
    expect(
      bareBlock.includes('display:'),
      'bare `dialog` rule must NOT set display — it overrides the UA ' +
        'stylesheet and makes closed dialogs visible. Put display rules ' +
        'inside `dialog[open] { ... }` instead.',
    ).toBe(false);
    // Positive check: layout DOES exist somewhere with [open] scope.
    expect(stylesText).toMatch(/dialog\[open\]\s*\{[^{}]*display:\s*flex/);
  });
});
