// @vitest-environment happy-dom
//
// Skills page list + new-form behaviour. Detail-mode behaviour is
// pinned in `skill-detail-page.test.ts` (separate component).

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const listSkillsSpy = vi.fn();
const createSkillSpy = vi.fn();
const getSkillSpy = vi.fn(); // exists for the delegated detail page
const shareSkillSpy = vi.fn();
const unshareSkillSpy = vi.fn();
const deleteSkillSpy = vi.fn();

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
      shareSkill: shareSkillSpy,
      unshareSkill: unshareSkillSpy,
      deleteSkill: deleteSkillSpy,
    }),
  };
});

import { SkillsPage } from './skills-page.js';
import { setMe } from '../auth/capabilities.js';
import type { Me, SkillSummary } from '../api/client.js';

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
    [
      listSkillsSpy,
      createSkillSpy,
      getSkillSpy,
      shareSkillSpy,
      unshareSkillSpy,
      deleteSkillSpy,
    ].forEach((s) => s.mockReset());
    listSkillsSpy.mockResolvedValue([]);
    setHash('#/skills/new');
    page = new SkillsPage();
    document.body.appendChild(page);
    await settle(page);
  });

  afterEach(() => {
    page.remove();
    setMe(null);
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
    // PR20: description is now required + live-validated; without it
    // the Save button stays disabled and the click does nothing.
    const descInput = page.querySelector(
      '[data-testid="skill-dialog-description"]',
    ) as HTMLInputElement;
    descInput.value = 'demo description';
    descInput.dispatchEvent(new Event('input'));
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
    [
      listSkillsSpy,
      createSkillSpy,
      getSkillSpy,
      shareSkillSpy,
      unshareSkillSpy,
      deleteSkillSpy,
    ].forEach((s) => s.mockReset());
    listSkillsSpy.mockResolvedValue([
      {
        id: 's-1', tenant_id: 't', name: 'alpha',
        description: 'desc', enabled: true, bound_coworker_count: 2,
        visibility: 'private',
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
    setMe(null);
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

// --------------------------------------------------------------------
// Role-aware affordances (RBAC UI PR5, spec §5.2 / §7.3 / §7.4).
//
// Mirrors coworkers-page.test.ts. What is pinned here:
//   * Chips re-classify the ALREADY-server-filtered list (UX only):
//     "Mine" -> own rows; "Shared by others" -> others' shared rows.
//   * Per-row management affordances (Edit / Delete / Share) gated by
//     `canManage(skill, 'skill.manage')` — the ownership escape. With the
//     manage capability every row is editable; for a plain member only
//     own rows are, and a null-owner row is NEVER manageable.
//   * The Share toggle calls shareSkill / unshareSkill.
//
// We mock ONLY the api client (the external boundary). The capability
// logic uses the REAL capabilities.ts (setMe + canManage/isOwnResource):
// mocking it would hide the very bugs these tests exist to catch.
//
// We deliberately do NOT assert "a member can't see X's private row" —
// that is backend visibility filtering (`user_can_see_resource`), enforced
// server-side and tested there. Re-asserting it here would be a forbidden
// duplicate-source / mirror test (spec §7.3).
// --------------------------------------------------------------------

const MY_UID = 'u-me';

// Capability sets transcribed from the backend matrix — TEST INPUTS ONLY.
// The production code keeps no role->cap map; it reads me.capabilities.
const MEMBER_CAPS = ['skill.create', 'skill.use'];
const MANAGER_CAPS = ['skill.create', 'skill.manage', 'skill.use'];

function makeMe(capabilities: string[]): Me {
  return {
    user_id: MY_UID,
    tenant_id: 't-1',
    name: 'Me',
    email: 'me@example.com',
    role: 'member',
    plane: 'tenant',
    capabilities,
  };
}

function mkSkill(over: Partial<SkillSummary>): SkillSummary {
  return {
    id: 's-x',
    tenant_id: 't-1',
    name: 'sk',
    description: 'desc',
    enabled: true,
    bound_coworker_count: 0,
    created_by_user_id: null,
    visibility: 'private',
    created_at: '',
    updated_at: '',
    ...over,
  };
}

// The canonical MIXED fixture: own-private, own-shared, others'-shared,
// and a null-owner (legacy/platform-default) row.
const OWN_PRIVATE = mkSkill({
  id: 'own-priv',
  name: 'Own Private',
  created_by_user_id: MY_UID,
  visibility: 'private',
});
const OWN_SHARED = mkSkill({
  id: 'own-shared',
  name: 'Own Shared',
  created_by_user_id: MY_UID,
  visibility: 'shared',
});
const OTHERS_SHARED = mkSkill({
  id: 'others-shared',
  name: 'Others Shared',
  created_by_user_id: 'u-other',
  visibility: 'shared',
});
const NULL_OWNER_SHARED = mkSkill({
  id: 'null-owner',
  name: 'Legacy Shared',
  created_by_user_id: null,
  visibility: 'shared',
});

const MIXED: SkillSummary[] = [
  OWN_PRIVATE,
  OWN_SHARED,
  OTHERS_SHARED,
  NULL_OWNER_SHARED,
];

/** Ids of the skill cards currently rendered, in DOM order. */
function visibleIds(page: SkillsPage): string[] {
  return Array.from(page.querySelectorAll('[data-skill-id]')).map(
    (el) => el.getAttribute('data-skill-id') ?? '',
  );
}

/** The card element for one skill id (so we can scope per-row queries). */
function card(page: SkillsPage, id: string): Element | null {
  return page.querySelector(`[data-skill-id="${id}"]`);
}

function clickChip(page: SkillsPage, chip: string): void {
  page
    .querySelector<HTMLButtonElement>(`[data-testid="skill-chip-${chip}"]`)!
    .click();
}

describe('SkillsPage — role-aware affordances', () => {
  beforeEach(() => {
    [
      listSkillsSpy,
      createSkillSpy,
      getSkillSpy,
      shareSkillSpy,
      unshareSkillSpy,
      deleteSkillSpy,
    ].forEach((s) => s.mockReset());
    listSkillsSpy.mockResolvedValue(MIXED);
    setHash('#/skills');
  });

  afterEach(() => {
    document.querySelectorAll('rm-skills-page').forEach((el) => el.remove());
    setMe(null);
    setHash('#/');
    vi.clearAllMocks();
  });

  async function mount(): Promise<SkillsPage> {
    const page = new SkillsPage();
    document.body.appendChild(page);
    await settle(page);
    return page;
  }

  describe('filter chips (UX re-classification)', () => {
    it('default "All visible" shows every returned row, untouched', async () => {
      setMe(makeMe(MEMBER_CAPS));
      const page = await mount();
      // No security filtering on the frontend — all 4 server-returned rows.
      expect(visibleIds(page)).toEqual([
        'own-priv',
        'own-shared',
        'others-shared',
        'null-owner',
      ]);
    });

    it('chip "Mine" narrows to own rows only (both visibilities)', async () => {
      setMe(makeMe(MEMBER_CAPS));
      const page = await mount();
      clickChip(page, 'mine');
      await settle(page);
      const ids = visibleIds(page);
      expect(ids).toEqual(['own-priv', 'own-shared']);
      // The null-owner row must NOT count as "Mine" (three-value safety).
      expect(ids).not.toContain('null-owner');
      expect(ids).not.toContain('others-shared');
    });

    it('chip "Shared by others" narrows to others\' shared rows only', async () => {
      setMe(makeMe(MEMBER_CAPS));
      const page = await mount();
      clickChip(page, 'shared');
      await settle(page);
      const ids = visibleIds(page);
      // own-shared is mine, so it is excluded; null-owner has no owner ->
      // not "mine" -> it IS "shared by others" (shared + not own).
      expect(ids).toContain('others-shared');
      expect(ids).toContain('null-owner');
      expect(ids).not.toContain('own-shared');
      expect(ids).not.toContain('own-priv');
    });
  });

  describe('per-row management gating (ownership escape)', () => {
    it('with skill.manage, Edit shows on EVERY row (incl. null-owner)', async () => {
      setMe(makeMe(MANAGER_CAPS));
      const page = await mount();
      for (const id of [
        'own-priv',
        'own-shared',
        'others-shared',
        'null-owner',
      ]) {
        expect(
          card(page, id)?.querySelector('[data-testid="skill-edit"]'),
          `manager should see Edit on ${id}`,
        ).not.toBeNull();
        expect(
          card(page, id)?.querySelector('[data-testid="skill-viewonly"]'),
          `manager should NOT see a view-only hint on ${id}`,
        ).toBeNull();
      }
    });

    it('as a plain member, Edit shows only on own rows', async () => {
      setMe(makeMe(MEMBER_CAPS));
      const page = await mount();
      // Own rows: editable.
      for (const id of ['own-priv', 'own-shared']) {
        expect(
          card(page, id)?.querySelector('[data-testid="skill-edit"]'),
          `member should see Edit on own ${id}`,
        ).not.toBeNull();
      }
      // Others' shared: NOT editable, shows view-only hint instead.
      expect(
        card(page, 'others-shared')?.querySelector(
          '[data-testid="skill-edit"]',
        ),
      ).toBeNull();
      expect(
        card(page, 'others-shared')?.querySelector(
          '[data-testid="skill-viewonly"]',
        ),
      ).not.toBeNull();
    });

    it('a null-owner row is NOT manageable by a member (three-value safety)', async () => {
      setMe(makeMe(MEMBER_CAPS));
      const page = await mount();
      // The legacy/platform-default row (created_by_user_id === null) must
      // never qualify for the ownership escape — no Edit/Delete/Share.
      const row = card(page, 'null-owner');
      expect(row?.querySelector('[data-testid="skill-edit"]')).toBeNull();
      expect(row?.querySelector('[data-testid="skill-delete"]')).toBeNull();
      expect(row?.querySelector('[data-testid="skill-share"]')).toBeNull();
      expect(
        row?.querySelector('[data-testid="skill-viewonly"]'),
      ).not.toBeNull();
    });
  });

  describe('share toggle (canManage-gated)', () => {
    it('Share toggle is present exactly where canManage is true (member)', async () => {
      setMe(makeMe(MEMBER_CAPS));
      const page = await mount();
      // Own rows -> share present.
      expect(
        card(page, 'own-priv')?.querySelector('[data-testid="skill-share"]'),
      ).not.toBeNull();
      expect(
        card(page, 'own-shared')?.querySelector('[data-testid="skill-share"]'),
      ).not.toBeNull();
      // Non-own rows -> share absent.
      expect(
        card(page, 'others-shared')?.querySelector(
          '[data-testid="skill-share"]',
        ),
      ).toBeNull();
      expect(
        card(page, 'null-owner')?.querySelector('[data-testid="skill-share"]'),
      ).toBeNull();
    });

    it('clicking Share on a PRIVATE own row calls shareSkill(id)', async () => {
      setMe(makeMe(MEMBER_CAPS));
      shareSkillSpy.mockResolvedValue({
        ...OWN_PRIVATE,
        visibility: 'shared',
      });
      const page = await mount();
      const btn = card(page, 'own-priv')!.querySelector<HTMLButtonElement>(
        '[data-testid="skill-share"]',
      )!;
      btn.click();
      await settle(page);
      expect(shareSkillSpy).toHaveBeenCalledTimes(1);
      expect(shareSkillSpy).toHaveBeenCalledWith('own-priv');
      expect(unshareSkillSpy).not.toHaveBeenCalled();
    });

    it('clicking Share on a SHARED own row calls unshareSkill(id)', async () => {
      setMe(makeMe(MEMBER_CAPS));
      unshareSkillSpy.mockResolvedValue({
        ...OWN_SHARED,
        visibility: 'private',
      });
      const page = await mount();
      const btn = card(page, 'own-shared')!.querySelector<HTMLButtonElement>(
        '[data-testid="skill-share"]',
      )!;
      btn.click();
      await settle(page);
      expect(unshareSkillSpy).toHaveBeenCalledTimes(1);
      expect(unshareSkillSpy).toHaveBeenCalledWith('own-shared');
      expect(shareSkillSpy).not.toHaveBeenCalled();
    });

    it('patches only visibility on the row from the returned full Skill (keeps summary fields)', async () => {
      // The share endpoint returns a full Skill, which has NO description /
      // bound_coworker_count. A naive whole-row swap (PR4's Coworker path)
      // would blank those summary-only columns — this pins the patch-only
      // behavior so the row keeps its description + bound count.
      setMe(makeMe(MEMBER_CAPS));
      shareSkillSpy.mockResolvedValue({
        id: 'own-priv',
        tenant_id: 't-1',
        name: 'Own Private',
        enabled: true,
        created_at: '',
        updated_at: '',
        created_by_user_id: MY_UID,
        visibility: 'shared',
        // NOTE: no description, no bound_coworker_count (full Skill shape).
      });
      const page = await mount();
      const btn = card(page, 'own-priv')!.querySelector<HTMLButtonElement>(
        '[data-testid="skill-share"]',
      )!;
      btn.click();
      await settle(page);
      const row = card(page, 'own-priv')!;
      // Visibility flipped to Shared...
      expect(
        row
          .querySelector('[data-testid="skill-visibility"]')
          ?.textContent?.trim(),
      ).toBe('Shared');
      // ...but the description survived (would be '—' if the row were swapped).
      expect(row.textContent).toContain('desc');
    });

    it('a manager can share OTHERS\' rows too (manage capability, not ownership)', async () => {
      setMe(makeMe(MANAGER_CAPS));
      unshareSkillSpy.mockResolvedValue({
        ...NULL_OWNER_SHARED,
        visibility: 'private',
      });
      const page = await mount();
      // The null-owner row's toggle must exist for a manager and, since it
      // is already shared, click -> unshare.
      const btn = card(page, 'null-owner')!.querySelector<HTMLButtonElement>(
        '[data-testid="skill-share"]',
      );
      expect(btn).not.toBeNull();
      btn!.click();
      await settle(page);
      expect(unshareSkillSpy).toHaveBeenCalledWith('null-owner');
    });
  });

  describe('visibility pill + new-skill gating', () => {
    it('renders a green "Shared" / gray "Private" pill from wire visibility', async () => {
      setMe(makeMe(MEMBER_CAPS));
      const page = await mount();
      const priv = card(page, 'own-priv')?.querySelector(
        '[data-testid="skill-visibility"]',
      );
      const shared = card(page, 'own-shared')?.querySelector(
        '[data-testid="skill-visibility"]',
      );
      expect(priv?.textContent?.trim()).toBe('Private');
      expect(priv?.className).toContain('rm-pill-off');
      expect(shared?.textContent?.trim()).toBe('Shared');
      expect(shared?.className).toContain('rm-pill-on');
    });

    it('hides the New-skill button when skill.create is absent', async () => {
      // The gate must be capability-driven, not assumed-on. Seed a Me with
      // no skill.create and assert the button vanishes.
      setMe(makeMe(['skill.use']));
      const page = await mount();
      expect(
        page.querySelector('[data-testid="skill-new"]'),
        'New button must be gated on skill.create',
      ).toBeNull();
    });

    it('shows the New-skill button when skill.create is present', async () => {
      setMe(makeMe(MEMBER_CAPS));
      const page = await mount();
      expect(page.querySelector('[data-testid="skill-new"]')).not.toBeNull();
    });
  });

  describe('capability-aware empty states', () => {
    it('member empty "All visible" copy points to create or ask admin', async () => {
      listSkillsSpy.mockResolvedValue([]);
      setMe(makeMe(MEMBER_CAPS));
      const page = await mount();
      const empty = page.querySelector('[data-testid="skill-empty"]');
      expect(empty?.textContent).toContain('ask your admin');
    });

    it('manager empty "All visible" copy says no skills in the tenant', async () => {
      listSkillsSpy.mockResolvedValue([]);
      setMe(makeMe(MANAGER_CAPS));
      const page = await mount();
      const empty = page.querySelector('[data-testid="skill-empty"]');
      expect(empty?.textContent).toContain('No skills in this tenant');
      expect(empty?.textContent).not.toContain('ask your admin');
    });
  });
});
