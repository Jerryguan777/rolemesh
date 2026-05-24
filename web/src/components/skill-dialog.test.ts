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
  parseSkillMd,
  serializeSkillMd,
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
    await Promise.resolve();
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
    const nameInput = el.querySelector<HTMLInputElement>(
      '[data-testid="skill-dialog-name"]',
    )!;
    nameInput.value = 'demo';
    nameInput.dispatchEvent(new Event('input'));
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
});
