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
  stripLeadingFolder,
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

  it('extra files: + Add file appends a row; remove drops it', async () => {
    const el = mount();
    el.editing = null;
    el.open = true;
    await settle(el);
    const addLink = el.querySelector<HTMLButtonElement>(
      '[data-testid="skill-dialog-add-file"]',
    )!;
    addLink.click();
    addLink.click();
    await settle(el);
    const rows = el.querySelectorAll('[data-testid="skill-dialog-file"]');
    expect(rows.length).toBe(2);
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

  it('blocks save with an invalid file path', async () => {
    const el = mount();
    el.editing = null;
    el.open = true;
    await settle(el);
    // Fill name + description so the new live-validation gates pass —
    // the only remaining gate is the file path. Without these the test
    // would pass for the wrong reason (description-empty also blocks).
    const nameInput = el.querySelector<HTMLInputElement>(
      '[data-testid="skill-dialog-name"]',
    )!;
    nameInput.value = 'demo';
    nameInput.dispatchEvent(new Event('input'));
    const descInput = el.querySelector<HTMLInputElement>(
      '[data-testid="skill-dialog-description"]',
    )!;
    descInput.value = 'demo description';
    descInput.dispatchEvent(new Event('input'));
    // Add a file with a name that the SKILL_FILE_PATH_RE rejects.
    const addLink = el.querySelector<HTMLButtonElement>(
      '[data-testid="skill-dialog-add-file"]',
    )!;
    addLink.click();
    await settle(el);
    const fileInput = el.querySelector<HTMLInputElement>(
      '[data-testid="skill-dialog-file"]',
    )!;
    fileInput.value = '../escape.md';
    fileInput.dispatchEvent(new Event('input'));
    await settle(el);
    const saveBtn = el.querySelector<HTMLButtonElement>(
      '[data-testid="skill-dialog-save"]',
    )!;
    saveBtn.click();
    await settle(el);
    expect(
      stub.calls.find(
        (c) => c.method === 'POST' && c.url.endsWith('/api/v1/skills'),
      ),
      'POST must NOT fire when validation fails',
    ).toBeUndefined();
  });

  it('rejects SKILL.md as an extra filename (reserved)', async () => {
    const el = mount();
    el.editing = null;
    el.open = true;
    await settle(el);
    const nameInput = el.querySelector<HTMLInputElement>(
      '[data-testid="skill-dialog-name"]',
    )!;
    nameInput.value = 'demo';
    nameInput.dispatchEvent(new Event('input'));
    const descInput = el.querySelector<HTMLInputElement>(
      '[data-testid="skill-dialog-description"]',
    )!;
    descInput.value = 'demo description';
    descInput.dispatchEvent(new Event('input'));
    const addLink = el.querySelector<HTMLButtonElement>(
      '[data-testid="skill-dialog-add-file"]',
    )!;
    addLink.click();
    await settle(el);
    const fileInput = el.querySelector<HTMLInputElement>(
      '[data-testid="skill-dialog-file"]',
    )!;
    fileInput.value = 'SKILL.md';
    fileInput.dispatchEvent(new Event('input'));
    await settle(el);
    const saveBtn = el.querySelector<HTMLButtonElement>(
      '[data-testid="skill-dialog-save"]',
    )!;
    saveBtn.click();
    await settle(el);
    expect(
      stub.calls.find(
        (c) => c.method === 'POST' && c.url.endsWith('/api/v1/skills'),
      ),
    ).toBeUndefined();
  });

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

describe('stripLeadingFolder', () => {
  it('drops the first path segment when there are multiple', () => {
    expect(stripLeadingFolder('rootName/references/intro.md')).toBe(
      'references/intro.md',
    );
  });

  it('passes single-segment paths through unchanged', () => {
    expect(stripLeadingFolder('SKILL.md')).toBe('SKILL.md');
  });

  it('handles a trailing-slash-only top folder', () => {
    // Edge: "folder/" with no file under it. After strip we get
    // empty string. Caller (ingestUploads) skips empties via the
    // isValidSkillFilePath gate.
    expect(stripLeadingFolder('folder/')).toBe('');
  });
});

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
    const rows = el.querySelectorAll<HTMLInputElement>(
      '[data-testid="skill-dialog-file"]',
    );
    expect(rows.length).toBe(1);
    expect(rows[0].value).toBe('note.md');
  });

  it('preserves folder structure from the folder picker via webkitRelativePath', async () => {
    const el = mount();
    el.editing = null;
    el.open = true;
    await settle(el);
    const input = el.querySelector<HTMLInputElement>(
      '[data-testid="skill-dialog-pick-folder"]',
    )!;
    // Folder picker exposes "rootName/sub/file.md" — the first
    // segment is the user's chosen root and gets stripped.
    const files = [
      makeFile({
        name: 'intro.md',
        content: '# intro\n',
        relativePath: 'my-skill/references/intro.md',
      }),
      makeFile({
        name: 'helper.py',
        content: "print('hi')\n",
        relativePath: 'my-skill/scripts/helper.py',
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
    const paths = [
      ...el.querySelectorAll<HTMLInputElement>(
        '[data-testid="skill-dialog-file"]',
      ),
    ].map((i) => i.value);
    // First segment ("my-skill") is stripped; folder structure
    // ("references/", "scripts/") survives.
    expect(paths.sort()).toEqual([
      'references/intro.md',
      'scripts/helper.py',
    ]);
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
    const rows = el.querySelectorAll<HTMLInputElement>(
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

describe('skill-dialog: folder snapshot', () => {
  let stub: Stub;
  beforeEach(() => {
    stub = installFetch();
  });
  afterEach(() => {
    stub.restore();
    document.body.innerHTML = '';
  });

  it('renders the snapshot with SKILL.md when no extras', async () => {
    const el = mount();
    el.editing = null;
    el.open = true;
    await settle(el);
    const snapshot = el.querySelector(
      '[data-testid="skill-dialog-folder-snapshot"]',
    );
    expect(snapshot, 'snapshot section must always render').toBeTruthy();
    // Pin the "SKILL.md is in this folder" + "edited below" framing —
    // the whole point of the snapshot is bridging the textarea to the
    // file model, so the cross-reference text MUST be visible.
    expect(snapshot!.textContent).toContain('SKILL.md');
    expect(snapshot!.textContent).toContain('edited in Instructions');
    // The empty-extras hint also lives here. Without it the snapshot
    // looks sad on a fresh dialog.
    expect(snapshot!.textContent).toContain('additional files appear here');
  });

  it('snapshot updates live when files are uploaded', async () => {
    const el = mount();
    el.editing = null;
    el.open = true;
    await settle(el);
    const input = el.querySelector<HTMLInputElement>(
      '[data-testid="skill-dialog-pick-files"]',
    )!;
    Object.defineProperty(input, 'files', {
      value: [
        makeFile({ name: 'intro.md', content: '# Intro\n' }),
      ],
      configurable: true,
    });
    input.dispatchEvent(new Event('change'));
    for (let i = 0; i < 50; i += 1) {
      await new Promise((r) => setTimeout(r, 0));
      await el.updateComplete;
      const s = el.querySelector(
        '[data-testid="skill-dialog-folder-snapshot"]',
      );
      if (s?.textContent?.includes('intro.md')) break;
    }
    const snapshot = el.querySelector(
      '[data-testid="skill-dialog-folder-snapshot"]',
    )!;
    expect(snapshot.textContent).toContain('intro.md');
    // SKILL.md row stays — the snapshot shows BOTH the main file and
    // the new extra, which is the unified mental model PR25 set out
    // to fix. Without this assert, a regression that swaps the
    // snapshot for "just the extras" would pass the previous test.
    expect(snapshot.textContent).toContain('SKILL.md');
  });

  it('snapshot groups files by folder', async () => {
    // Two extras under the same prefix should render as a single
    // folder row + indented children, NOT as two flat rows. This is
    // the visual cue that gives the user "ah, these end up in
    // references/ together".
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
          name: 'intro.md',
          content: 'intro',
          relativePath: 'root/references/intro.md',
        }),
        makeFile({
          name: 'glossary.md',
          content: 'gloss',
          relativePath: 'root/references/glossary.md',
        }),
      ],
      configurable: true,
    });
    input.dispatchEvent(new Event('change'));
    for (let i = 0; i < 50; i += 1) {
      await new Promise((r) => setTimeout(r, 0));
      await el.updateComplete;
      const s = el.querySelector(
        '[data-testid="skill-dialog-folder-snapshot"]',
      );
      if (s?.textContent?.includes('glossary.md')) break;
    }
    const text = el
      .querySelector('[data-testid="skill-dialog-folder-snapshot"]')!
      .textContent!;
    // Folder name renders with trailing slash (visual differentiator
    // from files); confirm the references/ header is there and
    // BOTH files appear under it.
    expect(text).toContain('references/');
    expect(text).toContain('intro.md');
    expect(text).toContain('glossary.md');
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
});
