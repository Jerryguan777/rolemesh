// @vitest-environment happy-dom
// <rm-activity-shell> — now a thin launcher. The safety log moved to
// Settings → Governance (spec §7), so the Activity overview's card links into
// `#/manage/safety-log` rather than hosting the page in-shell. We pin: the
// card exists, it navigates to the settings home, the shell never slots the
// decisions page, and the X button returns to chat.

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

  it('renders the index launcher card and never slots the decisions page', async () => {
    loc = stubHash('#/activity');
    const el = await mount();
    expect(
      el.querySelector('[data-testid="activity-card-safety-log"]'),
    ).not.toBeNull();
    expect(el.querySelector('rm-safety-decisions-page')).toBeNull();
  });

  it('clicking the Safety log card navigates to Settings → Safety log', async () => {
    loc = stubHash('#/activity');
    const el = await mount();
    const card = el.querySelector<HTMLButtonElement>(
      '[data-testid="activity-card-safety-log"]',
    );
    card!.click();
    await settle(el);
    expect(loc.hashAssignments).toContain('#/manage/safety-log');
    // The shell still never hosts the page itself.
    expect(el.querySelector('rm-safety-decisions-page')).toBeNull();
  });

  it('X button navigates back to chat (#/)', async () => {
    loc = stubHash('#/activity');
    const el = await mount();
    const back = el.querySelector<HTMLButtonElement>(
      '[data-testid="activity-back"]',
    );
    back!.click();
    expect(loc.hashAssignments).toContain('#/');
  });
});
