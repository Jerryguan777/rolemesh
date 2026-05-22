// @vitest-environment happy-dom
//
// Skill detail page: pin two behaviours that would silently regress
// if the component drifts:
//
// 1. SKILL.md row renders with a disabled delete button (the server
//    returns 409 SKILL_MANIFEST_PROTECTED on a delete attempt; the
//    UI should not even let the user try).
// 2. Adding a file with a traversal path is rejected at the form
//    level — the visible error must appear, and no API call goes
//    out (so we don't rely on the server's 422 to surface the bug).
//
// Anti-mirror: assertions read the rendered DOM, not the underlying
// path regex constant. If the regex changes the test runs the new
// regex through the component's own code path.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const getSkillSpy = vi.fn();
const putSkillFileSpy = vi.fn();
const updateSkillSpy = vi.fn();
const deleteSkillSpy = vi.fn();
const deleteSkillFileSpy = vi.fn();

vi.mock('../api/client.js', async () => {
  const actual = await vi.importActual<typeof import('../api/client.js')>(
    '../api/client.js',
  );
  return {
    ...actual,
    getApiClient: () => ({
      getSkill: getSkillSpy,
      putSkillFile: putSkillFileSpy,
      updateSkill: updateSkillSpy,
      deleteSkill: deleteSkillSpy,
      deleteSkillFile: deleteSkillFileSpy,
    }),
  };
});

import { SkillDetailPage } from './skill-detail-page.js';

async function settle(el: SkillDetailPage): Promise<void> {
  for (let i = 0; i < 20; i++) {
    await Promise.resolve();
    await el.updateComplete;
  }
}

const _baseSkill = {
  id: 'skill-123',
  tenant_id: 'tnt-1',
  name: 'code-review',
  enabled: true,
  frontmatter_common: {},
  frontmatter_backend: {},
  files: {
    'SKILL.md': {
      path: 'SKILL.md',
      content: '# body',
      mime_type: 'text/markdown',
      updated_at: '',
    },
    'reference.md': {
      path: 'reference.md',
      content: 'ref',
      mime_type: 'text/plain',
      updated_at: '',
    },
  },
  created_at: '',
  updated_at: '',
};


describe('SkillDetailPage', () => {
  let page: SkillDetailPage;

  beforeEach(async () => {
    [
      getSkillSpy, putSkillFileSpy, updateSkillSpy,
      deleteSkillSpy, deleteSkillFileSpy,
    ].forEach((s) => s.mockReset());
    getSkillSpy.mockResolvedValue(structuredClone(_baseSkill));
    page = new SkillDetailPage();
    page.skillId = 'skill-123';
    document.body.appendChild(page);
    await settle(page);
  });

  afterEach(() => {
    page.remove();
  });

  it('renders SKILL.md with a disabled delete button', () => {
    const items = Array.from(page.querySelectorAll('aside li'));
    const manifest = items.find((li) =>
      li.textContent?.includes('SKILL.md'),
    );
    expect(manifest, 'SKILL.md row in tree').toBeTruthy();
    const delBtn = manifest!.querySelector('button[title*="protected"]');
    expect(delBtn, 'manifest delete button present').toBeTruthy();
    expect((delBtn as HTMLButtonElement).disabled).toBe(true);
  });

  it('refuses to add a file with a traversal path locally', async () => {
    const input = page.querySelector(
      'aside input[type="text"]',
    ) as HTMLInputElement;
    expect(input).toBeTruthy();
    input.value = '../escape.md';
    input.dispatchEvent(new Event('input'));
    const addBtn = page.querySelector('aside button.bg-brand') as HTMLButtonElement;
    expect(addBtn).toBeTruthy();
    addBtn.click();
    await settle(page);
    const err = page.querySelector('aside .text-red-600, aside .dark\\:text-red-300');
    expect(err?.textContent ?? '').toContain('Invalid path');
    expect(putSkillFileSpy).not.toHaveBeenCalled();
  });
});
