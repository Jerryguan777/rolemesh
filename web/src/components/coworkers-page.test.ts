// @vitest-environment happy-dom
//
// Role-aware Coworkers page (RBAC UI PR4, spec §5.1 / §7.3 / §7.4).
//
// What is pinned here:
//   * Chips re-classify the ALREADY-server-filtered list (UX only):
//     "Mine" -> own rows; "Shared by others" -> others' shared rows.
//   * Per-row management affordances (Edit / Delete / Share) are gated by
//     `canManage(co, 'coworker.manage')` — the ownership escape. With the
//     manage capability every row is editable; for a plain member only
//     own rows are, and a null-owner row is NEVER manageable.
//   * The Share toggle calls shareCoworker / unshareCoworker.
//
// We mock ONLY the api client (the external boundary). The capability
// logic uses the REAL capabilities.ts (setMe + canManage/isOwnResource):
// mocking it would hide the very bugs these tests exist to catch.
//
// We deliberately do NOT assert "a member can't see X's private row" —
// that is backend visibility filtering (`user_can_see_resource`), enforced
// server-side and tested there. Re-asserting it here would be a forbidden
// duplicate-source / mirror test (spec §7.3).

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const listCoworkersSpy = vi.fn();
const shareCoworkerSpy = vi.fn();
const unshareCoworkerSpy = vi.fn();
const deleteCoworkerSpy = vi.fn();

vi.mock('../api/client.js', async () => {
  const actual = await vi.importActual<typeof import('../api/client.js')>(
    '../api/client.js',
  );
  return {
    ...actual,
    getApiClient: () => ({
      listCoworkers: listCoworkersSpy,
      shareCoworker: shareCoworkerSpy,
      unshareCoworker: unshareCoworkerSpy,
      deleteCoworker: deleteCoworkerSpy,
    }),
  };
});

import { CoworkersPage } from './coworkers-page.js';
import { setMe } from '../auth/capabilities.js';
import type { Coworker, Me } from '../api/client.js';

const MY_UID = 'u-me';

// Capability sets transcribed from the backend matrix — TEST INPUTS ONLY.
// The production code keeps no role->cap map; it reads me.capabilities.
const MEMBER_CAPS = ['coworker.create', 'coworker.use'];
const MANAGER_CAPS = [
  'coworker.create',
  'coworker.manage',
  'coworker.use',
];

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

function mkCoworker(over: Partial<Coworker>): Coworker {
  return {
    id: 'c-x',
    tenant_id: 't-1',
    name: 'cw',
    folder: 'cw',
    agent_backend: 'claude',
    status: 'active',
    max_concurrent: 2,
    created_by_user_id: null,
    visibility: 'private',
    permissions: { agent_delegate: false, task_schedule: false, task_manage_others: false },
    is_frontdesk: false,
    created_at: '',
    ...over,
  };
}

// The canonical MIXED fixture: own-private, own-shared, others'-shared,
// and a null-owner (legacy/platform-default) row.
const OWN_PRIVATE = mkCoworker({
  id: 'own-priv',
  name: 'Own Private',
  created_by_user_id: MY_UID,
  visibility: 'private',
});
const OWN_SHARED = mkCoworker({
  id: 'own-shared',
  name: 'Own Shared',
  created_by_user_id: MY_UID,
  visibility: 'shared',
});
const OTHERS_SHARED = mkCoworker({
  id: 'others-shared',
  name: 'Others Shared',
  created_by_user_id: 'u-other',
  visibility: 'shared',
});
const NULL_OWNER_SHARED = mkCoworker({
  id: 'null-owner',
  name: 'Legacy Shared',
  created_by_user_id: null,
  visibility: 'shared',
});

const MIXED = [OWN_PRIVATE, OWN_SHARED, OTHERS_SHARED, NULL_OWNER_SHARED];

async function settle(el: CoworkersPage): Promise<void> {
  for (let i = 0; i < 20; i += 1) {
    await Promise.resolve();
    await el.updateComplete;
  }
}

async function mount(): Promise<CoworkersPage> {
  const page = new CoworkersPage();
  document.body.appendChild(page);
  await settle(page);
  return page;
}

/** Ids of the coworker cards currently rendered, in DOM order. */
function visibleIds(page: CoworkersPage): string[] {
  return Array.from(page.querySelectorAll('[data-coworker-id]')).map(
    (el) => el.getAttribute('data-coworker-id') ?? '',
  );
}

/** The card element for one coworker id (so we can scope per-row queries). */
function card(page: CoworkersPage, id: string): Element | null {
  return page.querySelector(`[data-coworker-id="${id}"]`);
}

function clickChip(page: CoworkersPage, chip: string): void {
  page
    .querySelector<HTMLButtonElement>(`[data-testid="coworker-chip-${chip}"]`)!
    .click();
}

beforeEach(() => {
  [
    listCoworkersSpy,
    shareCoworkerSpy,
    unshareCoworkerSpy,
    deleteCoworkerSpy,
  ].forEach((s) => s.mockReset());
  listCoworkersSpy.mockResolvedValue(MIXED);
});

afterEach(() => {
  document.querySelectorAll('rm-coworkers-page').forEach((el) => el.remove());
  setMe(null);
  vi.clearAllMocks();
});

describe('CoworkersPage — filter chips (UX re-classification)', () => {
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

describe('CoworkersPage — per-row management gating (ownership escape)', () => {
  it('with coworker.manage, Edit shows on EVERY row (incl. null-owner)', async () => {
    setMe(makeMe(MANAGER_CAPS));
    const page = await mount();
    for (const id of ['own-priv', 'own-shared', 'others-shared', 'null-owner']) {
      expect(
        card(page, id)?.querySelector('[data-testid="coworker-edit"]'),
        `manager should see Edit on ${id}`,
      ).not.toBeNull();
      expect(
        card(page, id)?.querySelector('[data-testid="coworker-viewonly"]'),
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
        card(page, id)?.querySelector('[data-testid="coworker-edit"]'),
        `member should see Edit on own ${id}`,
      ).not.toBeNull();
    }
    // Others' shared: NOT editable, shows view-only hint instead.
    expect(
      card(page, 'others-shared')?.querySelector(
        '[data-testid="coworker-edit"]',
      ),
    ).toBeNull();
    expect(
      card(page, 'others-shared')?.querySelector(
        '[data-testid="coworker-viewonly"]',
      ),
    ).not.toBeNull();
  });

  it('a null-owner row is NOT manageable by a member (three-value safety)', async () => {
    setMe(makeMe(MEMBER_CAPS));
    const page = await mount();
    // The legacy/platform-default row (created_by_user_id === null) must
    // never qualify for the ownership escape — no Edit/Delete/Share.
    const row = card(page, 'null-owner');
    expect(row?.querySelector('[data-testid="coworker-edit"]')).toBeNull();
    expect(row?.querySelector('[data-testid="coworker-delete"]')).toBeNull();
    expect(row?.querySelector('[data-testid="coworker-share"]')).toBeNull();
    expect(
      row?.querySelector('[data-testid="coworker-viewonly"]'),
    ).not.toBeNull();
  });
});

describe('CoworkersPage — share toggle (canManage-gated)', () => {
  it('Share toggle is present exactly where canManage is true (member)', async () => {
    setMe(makeMe(MEMBER_CAPS));
    const page = await mount();
    // Own rows -> share present.
    expect(
      card(page, 'own-priv')?.querySelector('[data-testid="coworker-share"]'),
    ).not.toBeNull();
    expect(
      card(page, 'own-shared')?.querySelector('[data-testid="coworker-share"]'),
    ).not.toBeNull();
    // Non-own rows -> share absent.
    expect(
      card(page, 'others-shared')?.querySelector(
        '[data-testid="coworker-share"]',
      ),
    ).toBeNull();
    expect(
      card(page, 'null-owner')?.querySelector('[data-testid="coworker-share"]'),
    ).toBeNull();
  });

  it('clicking Share on a PRIVATE own row calls shareCoworker(id)', async () => {
    setMe(makeMe(MEMBER_CAPS));
    shareCoworkerSpy.mockResolvedValue({ ...OWN_PRIVATE, visibility: 'shared' });
    const page = await mount();
    const btn = card(page, 'own-priv')!.querySelector<HTMLButtonElement>(
      '[data-testid="coworker-share"]',
    )!;
    btn.click();
    await settle(page);
    expect(shareCoworkerSpy).toHaveBeenCalledTimes(1);
    expect(shareCoworkerSpy).toHaveBeenCalledWith('own-priv');
    expect(unshareCoworkerSpy).not.toHaveBeenCalled();
  });

  it('clicking Share on a SHARED own row calls unshareCoworker(id)', async () => {
    setMe(makeMe(MEMBER_CAPS));
    unshareCoworkerSpy.mockResolvedValue({
      ...OWN_SHARED,
      visibility: 'private',
    });
    const page = await mount();
    const btn = card(page, 'own-shared')!.querySelector<HTMLButtonElement>(
      '[data-testid="coworker-share"]',
    )!;
    btn.click();
    await settle(page);
    expect(unshareCoworkerSpy).toHaveBeenCalledTimes(1);
    expect(unshareCoworkerSpy).toHaveBeenCalledWith('own-shared');
    expect(shareCoworkerSpy).not.toHaveBeenCalled();
  });

  it('a manager can share OTHERS\' rows too (manage capability, not ownership)', async () => {
    setMe(makeMe(MANAGER_CAPS));
    shareCoworkerSpy.mockResolvedValue({
      ...NULL_OWNER_SHARED,
      visibility: 'shared',
    });
    const page = await mount();
    // The null-owner row's toggle must exist for a manager and, since it
    // is already shared, click -> unshare.
    unshareCoworkerSpy.mockResolvedValue({
      ...NULL_OWNER_SHARED,
      visibility: 'private',
    });
    const btn = card(page, 'null-owner')!.querySelector<HTMLButtonElement>(
      '[data-testid="coworker-share"]',
    );
    expect(btn).not.toBeNull();
    btn!.click();
    await settle(page);
    expect(unshareCoworkerSpy).toHaveBeenCalledWith('null-owner');
  });
});

describe('CoworkersPage — visibility pill + new-coworker gating', () => {
  it('renders a green "Shared" / gray "Private" pill from wire visibility', async () => {
    setMe(makeMe(MEMBER_CAPS));
    const page = await mount();
    const priv = card(page, 'own-priv')?.querySelector(
      '[data-testid="coworker-visibility"]',
    );
    const shared = card(page, 'own-shared')?.querySelector(
      '[data-testid="coworker-visibility"]',
    );
    expect(priv?.textContent?.trim()).toBe('Private');
    expect(priv?.className).toContain('rm-pill-off');
    expect(shared?.textContent?.trim()).toBe('Shared');
    expect(shared?.className).toContain('rm-pill-on');
  });

  it('hides the New-coworker button when coworker.create is absent', async () => {
    // A capability the matrix never strips from a tenant role today, but
    // the gate must be capability-driven, not assumed-on. Seed a Me with
    // no coworker.create and assert the button vanishes.
    setMe(makeMe(['coworker.use']));
    const page = await mount();
    expect(
      page.querySelector('[data-testid="coworker-new"]'),
      'New button must be gated on coworker.create',
    ).toBeNull();
  });

  it('shows the New-coworker button when coworker.create is present', async () => {
    setMe(makeMe(MEMBER_CAPS));
    const page = await mount();
    expect(
      page.querySelector('[data-testid="coworker-new"]'),
    ).not.toBeNull();
  });
});

describe('CoworkersPage — capability-aware empty states', () => {
  it('member empty "All visible" copy points to create or ask admin', async () => {
    listCoworkersSpy.mockResolvedValue([]);
    setMe(makeMe(MEMBER_CAPS));
    const page = await mount();
    const empty = page.querySelector('[data-testid="coworker-empty"]');
    expect(empty?.textContent).toContain('ask your admin');
  });

  it('manager empty "All visible" copy says no coworkers in the tenant', async () => {
    listCoworkersSpy.mockResolvedValue([]);
    setMe(makeMe(MANAGER_CAPS));
    const page = await mount();
    const empty = page.querySelector('[data-testid="coworker-empty"]');
    expect(empty?.textContent).toContain('No coworkers in this tenant');
    expect(empty?.textContent).not.toContain('ask your admin');
  });
});
