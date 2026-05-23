// @vitest-environment happy-dom
//
// Skills page list + new-form behaviour. Detail-mode behaviour is
// pinned in `skill-detail-page.test.ts` (separate component).

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const listSkillsSpy = vi.fn();
const createSkillSpy = vi.fn();
const getSkillSpy = vi.fn(); // exists for the delegated detail page

vi.mock('../api/client.js', async () => {
  const actual = await vi.importActual<typeof import('../api/client.js')>(
    '../api/client.js',
  );
  return {
    ...actual,
    getApiClient: () => ({
      listSkills: listSkillsSpy,
      createSkill: createSkillSpy,
      getSkill: getSkillSpy,
    }),
  };
});

import { SkillsPage } from './skills-page.js';

async function settle(el: SkillsPage): Promise<void> {
  for (let i = 0; i < 20; i++) {
    await Promise.resolve();
    await el.updateComplete;
  }
}

function setHash(hash: string): void {
  const previous = location.hash;
  if (previous === hash) return;
  history.replaceState(null, '', hash);
  window.dispatchEvent(new HashChangeEvent('hashchange'));
}


describe('SkillsPage new view', () => {
  let page: SkillsPage;

  beforeEach(async () => {
    [listSkillsSpy, createSkillSpy, getSkillSpy].forEach((s) => s.mockReset());
    listSkillsSpy.mockResolvedValue([]);
    setHash('#/skills/new');
    page = new SkillsPage();
    document.body.appendChild(page);
    await settle(page);
  });

  afterEach(() => {
    page.remove();
    setHash('#/');
  });

  it('posts createSkill with SKILL.md when Create is clicked', async () => {
    const nameInput = page.querySelector(
      'input[type="text"]',
    ) as HTMLInputElement;
    nameInput.value = 'demo';
    nameInput.dispatchEvent(new Event('input'));

    const createBtn = Array.from(page.querySelectorAll('button')).find(
      (b) => b.textContent?.trim() === 'Create',
    ) as HTMLButtonElement;
    expect(createBtn).toBeTruthy();
    createSkillSpy.mockResolvedValue({
      id: 'new-id', tenant_id: 't', name: 'demo', enabled: true,
      frontmatter_common: {}, frontmatter_backend: {}, files: {},
      created_at: '', updated_at: '',
    });
    createBtn.click();
    await settle(page);

    expect(createSkillSpy).toHaveBeenCalledTimes(1);
    const arg = createSkillSpy.mock.calls[0][0];
    expect(arg.name).toBe('demo');
    expect(arg.enabled).toBe(true);
    expect(Object.keys(arg.files)).toContain('SKILL.md');
  });
});


describe('SkillsPage list view', () => {
  let page: SkillsPage;

  beforeEach(async () => {
    [listSkillsSpy, createSkillSpy, getSkillSpy].forEach((s) => s.mockReset());
    listSkillsSpy.mockResolvedValue([
      {
        id: 's-1', tenant_id: 't', name: 'alpha',
        description: 'desc', enabled: true, bound_coworker_count: 2,
        created_at: '', updated_at: '',
      },
    ]);
    setHash('#/skills');
    page = new SkillsPage();
    document.body.appendChild(page);
    await settle(page);
  });

  afterEach(() => {
    page.remove();
    setHash('#/');
  });

  it('renders a row per skill with the bound coworker count', () => {
    // Use the list row link specifically — `+ New skill` also matches
    // ``a[href^="#/skills/"]`` (it points at #/skills/new).
    const link = page.querySelector('ul a[href^="#/skills/"]') as HTMLAnchorElement;
    expect(link).toBeTruthy();
    expect(link.textContent).toContain('alpha');
    expect(link.textContent).toContain('2 coworker');
  });
});
