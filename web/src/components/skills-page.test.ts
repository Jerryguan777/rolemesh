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


describe('SkillsPage new-skill flow (via dialog)', () => {
  // v2-C unified create + edit into <rm-skill-dialog>. The
  // route-based `#/skills/new` page is gone; the hash now opens
  // the dialog over the list.
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

  it('opens the skill dialog in create mode when hash is #/skills/new', () => {
    const dialog = page.querySelector('rm-skill-dialog');
    expect(dialog).toBeTruthy();
    expect((dialog as { editing?: unknown }).editing).toBeNull();
    expect((dialog as { open?: boolean }).open).toBe(true);
  });

  it('clicking Create skill in the dialog calls createSkill with SKILL.md', async () => {
    const nameInput = page.querySelector(
      '[data-testid="skill-dialog-name"]',
    ) as HTMLInputElement;
    expect(nameInput).toBeTruthy();
    nameInput.value = 'demo';
    nameInput.dispatchEvent(new Event('input'));
    await settle(page);

    createSkillSpy.mockResolvedValue({
      id: 'new-id', tenant_id: 't', name: 'demo', enabled: true,
      frontmatter_common: {}, frontmatter_backend: {}, files: {},
      created_at: '', updated_at: '',
    });
    const saveBtn = page.querySelector(
      '[data-testid="skill-dialog-save"]',
    ) as HTMLButtonElement;
    expect(saveBtn).toBeTruthy();
    saveBtn.click();
    await settle(page);

    expect(createSkillSpy).toHaveBeenCalledTimes(1);
    const arg = createSkillSpy.mock.calls[0][0];
    expect(arg.name).toBe('demo');
    expect(arg.enabled).toBe(true);
    expect(Object.keys(arg.files)).toContain('SKILL.md');
    // Body still carries the YAML frontmatter the dialog injects.
    expect(arg.files['SKILL.md']).toContain('name: demo');
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
    // v2-C reskin: rows are `.rm-card` divs (clickable), not <a>
    // anchors. The skill metadata (name + bound count) still lives
    // in the row; both pieces of text show in the visible content.
    const card = page.querySelector('.rm-card[data-skill-id]');
    expect(card).toBeTruthy();
    expect(card?.textContent).toContain('alpha');
    expect(card?.textContent).toContain('2 coworker');
  });
});
