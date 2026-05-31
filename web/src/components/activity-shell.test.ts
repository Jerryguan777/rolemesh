// @vitest-environment happy-dom
// <rm-activity-shell> — pins the two-route layout:
//   * `#/activity`                  → index (one card)
//   * `#/activity/safety-decisions` → <rm-safety-decisions-page>
//
// We pin observable behaviour: which custom element gets slotted, which
// hash a tab click writes, and whether the X button returns to `#/`.
// We do NOT mock the slotted pages — their internal fetches fail
// silently in the unit-test env, and that's fine because we're not
// asserting on the inner page's render output, just on shell routing.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api/client.js', async () => {
  const actual = await vi.importActual<typeof import('../api/client.js')>(
    '../api/client.js',
  );
  const single = vi.fn().mockResolvedValue(null);
  return {
    ...actual,
    getApiClient: () => ({
      getMe: single,
    }),
  };
});

vi.mock('../services/safety-admin-client.ts', () => ({
  getSafetyAdminClient: () => ({
    listRules: vi.fn().mockResolvedValue([]),
    listDecisions: vi.fn().mockResolvedValue([]),
  }),
  getTenantId: vi.fn().mockResolvedValue('tnt-1'),
  listCoworkers: vi.fn().mockResolvedValue([]),
  downloadDecisionsCsv: vi.fn(),
}));

// The class is imported solely as a type, so a TypeScript stripping
// pass would erase the side-effect that registers the custom element.
// Keep an explicit module-side import so the decorator always runs.
import './activity-shell.js';
import type { RmActivityShell } from './activity-shell.js';

interface LocationStub {
  hashAssignments: string[];
  hashGetter: string;
  restore: () => void;
}

function stubHash(initial: string): LocationStub {
  const stub: LocationStub = {
    hashAssignments: [],
    hashGetter: initial,
    restore: () => {},
  };
  const desc = Object.getOwnPropertyDescriptor(location, 'hash');
  Object.defineProperty(location, 'hash', {
    configurable: true,
    get: () => stub.hashGetter,
    set: (v: string) => {
      stub.hashAssignments.push(v);
      stub.hashGetter = v;
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    },
  });
  stub.restore = () => {
    if (desc) Object.defineProperty(location, 'hash', desc);
  };
  return stub;
}

async function settle(el: RmActivityShell): Promise<void> {
  for (let i = 0; i < 20; i += 1) {
    await Promise.resolve();
    await el.updateComplete;
  }
}

async function mount(): Promise<RmActivityShell> {
  const el = document.createElement('rm-activity-shell') as RmActivityShell;
  document.body.appendChild(el);
  await settle(el);
  return el;
}

describe('<rm-activity-shell>', () => {
  let loc: LocationStub;

  afterEach(() => {
    document.querySelectorAll('rm-activity-shell').forEach((el) => el.remove());
    loc?.restore();
    vi.clearAllMocks();
  });

  it('renders the index (card, no inner page) at #/activity', async () => {
    loc = stubHash('#/activity');
    const el = await mount();
    const body = el.querySelector('[data-testid="activity-body"]');
    expect(body?.getAttribute('data-tab')).toBe('index');
    expect(
      el.querySelector('[data-testid="activity-card-safety-decisions"]'),
    ).not.toBeNull();
    // Index renders no child page.
    expect(el.querySelector('rm-safety-decisions-page')).toBeNull();
  });

  it('renders <rm-safety-decisions-page> at #/activity/safety-decisions', async () => {
    loc = stubHash('#/activity/safety-decisions');
    const el = await mount();
    expect(el.querySelector('rm-safety-decisions-page')).not.toBeNull();
  });

  it('clicking the Safety decisions index card navigates to #/activity/safety-decisions', async () => {
    loc = stubHash('#/activity');
    const el = await mount();
    const card = el.querySelector<HTMLButtonElement>(
      '[data-testid="activity-card-safety-decisions"]',
    );
    card!.click();
    await settle(el);
    expect(loc.hashAssignments).toContain('#/activity/safety-decisions');
    expect(el.querySelector('rm-safety-decisions-page')).not.toBeNull();
  });

  it('tab bar switches via hash (URL is source of truth)', async () => {
    loc = stubHash('#/activity');
    const el = await mount();
    const safetyTab = el.querySelector<HTMLButtonElement>(
      '[data-testid="activity-tab-safety-decisions"]',
    );
    safetyTab!.click();
    await settle(el);
    expect(loc.hashAssignments).toContain('#/activity/safety-decisions');
    expect(
      el.querySelector('[data-testid="activity-body"]')?.getAttribute('data-tab'),
    ).toBe('safety-decisions');
  });

  it('X button navigates back to chat (#/)', async () => {
    loc = stubHash('#/activity/safety-decisions');
    const el = await mount();
    const back = el.querySelector<HTMLButtonElement>(
      '[data-testid="activity-back"]',
    );
    back!.click();
    expect(loc.hashAssignments).toContain('#/');
  });

  it('reacts to external hashchange (route survives deep link / refresh)', async () => {
    loc = stubHash('#/activity');
    const el = await mount();
    expect(
      el.querySelector('[data-testid="activity-body"]')?.getAttribute('data-tab'),
    ).toBe('index');
    // Simulate the user pasting a URL or hitting Back. The shell
    // must follow the URL, not its previous state.
    location.hash = '#/activity/safety-decisions';
    await settle(el);
    expect(el.querySelector('rm-safety-decisions-page')).not.toBeNull();
  });
});
