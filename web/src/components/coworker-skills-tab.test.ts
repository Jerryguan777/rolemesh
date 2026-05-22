// @vitest-environment happy-dom
//
// Coworker skills subtab: pin the toggle round-trip behavior. Two
// scenarios: enable an unbound skill calls POST; disable a bound
// skill calls DELETE and removes the in-state flag (so the next
// click flips back to enable, not disable).

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const listSkillsSpy = vi.fn();
const listCoworkerSkillsSpy = vi.fn();
const enableSpy = vi.fn();
const disableSpy = vi.fn();

vi.mock('../api/client.js', async () => {
  const actual = await vi.importActual<typeof import('../api/client.js')>(
    '../api/client.js',
  );
  return {
    ...actual,
    getApiClient: () => ({
      listSkills: listSkillsSpy,
      listCoworkerSkills: listCoworkerSkillsSpy,
      enableCoworkerSkill: enableSpy,
      disableCoworkerSkill: disableSpy,
    }),
  };
});

import { CoworkerSkillsTab } from './coworker-skills-tab.js';

async function settle(el: CoworkerSkillsTab): Promise<void> {
  for (let i = 0; i < 20; i++) {
    await Promise.resolve();
    await el.updateComplete;
  }
}

const _SKILL_A = {
  id: 'a-1', tenant_id: 't1', name: 'alpha', description: '', enabled: true,
  bound_coworker_count: 0, created_at: '', updated_at: '',
};
const _SKILL_B = {
  id: 'b-2', tenant_id: 't1', name: 'beta', description: '', enabled: true,
  bound_coworker_count: 1, created_at: '', updated_at: '',
};


describe('CoworkerSkillsTab toggling', () => {
  let tab: CoworkerSkillsTab;

  beforeEach(async () => {
    [
      listSkillsSpy, listCoworkerSkillsSpy, enableSpy, disableSpy,
    ].forEach((s) => s.mockReset());
    listSkillsSpy.mockResolvedValue([_SKILL_A, _SKILL_B]);
    // Coworker has skill B bound and enabled.
    listCoworkerSkillsSpy.mockResolvedValue([
      { coworker_id: 'cw-1', skill_id: _SKILL_B.id, enabled: true },
    ]);
    tab = new CoworkerSkillsTab();
    tab.coworkerId = 'cw-1';
    document.body.appendChild(tab);
    await settle(tab);
  });

  afterEach(() => {
    tab.remove();
  });

  it('renders an unchecked checkbox for unbound skills', () => {
    const checkboxes = Array.from(
      tab.querySelectorAll('input[type="checkbox"]'),
    ) as HTMLInputElement[];
    expect(checkboxes.length).toBe(2);
    // Find by skill name nearby — order matches listSkills.
    const [alphaBox, betaBox] = checkboxes;
    expect(alphaBox.checked).toBe(false);
    expect(betaBox.checked).toBe(true);
  });

  it('checking an unbound skill calls enableCoworkerSkill', async () => {
    enableSpy.mockResolvedValue({
      coworker_id: 'cw-1', skill_id: _SKILL_A.id, enabled: true,
    });
    const [alphaBox] = Array.from(
      tab.querySelectorAll('input[type="checkbox"]'),
    ) as HTMLInputElement[];
    alphaBox.checked = true;
    alphaBox.dispatchEvent(new Event('change'));
    await settle(tab);
    expect(enableSpy).toHaveBeenCalledWith('cw-1', _SKILL_A.id);
    expect(disableSpy).not.toHaveBeenCalled();
  });

  it('unchecking a bound skill calls disableCoworkerSkill', async () => {
    disableSpy.mockResolvedValue(undefined);
    const [, betaBox] = Array.from(
      tab.querySelectorAll('input[type="checkbox"]'),
    ) as HTMLInputElement[];
    betaBox.checked = false;
    betaBox.dispatchEvent(new Event('change'));
    await settle(tab);
    expect(disableSpy).toHaveBeenCalledWith('cw-1', _SKILL_B.id);
    expect(enableSpy).not.toHaveBeenCalled();
  });
});
