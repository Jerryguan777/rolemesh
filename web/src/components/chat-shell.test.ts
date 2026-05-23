// @vitest-environment happy-dom
// <rm-chat-shell> — pins the contract that the shell exposes to
// users:
//   * 3 topbar icons each route to the correct hash / popover
//   * coworker switcher popover lists every coworker and triggers
//     a `location.href` navigation with the new ?agent_id
//   * conversation row click navigates with a new ?chat_id
//   * user pill at the bottom opens a Settings + Log out menu
//   * groupConversations buckets correctly into Today/Yesterday/Earlier
//
// Anti-mirror: we drive real DOM clicks and assert the externally
// observable effect (location string mutated, popover element
// appears). Internal `openMenu` state is never inspected directly.
//
// Location stubbing: happy-dom's `location.href` setter calls
// `browserFrame.goto` asynchronously, which we cannot easily wait
// on inside a unit test. We replace the setter with a spy via
// Object.defineProperty so the test can read back what *would*
// have been navigated to.

import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest';

const listCoworkersSpy = vi.fn();
const getMeSpy = vi.fn();
const listConvsSpy = vi.fn();

vi.mock('../api/client.js', async () => {
  const actual = await vi.importActual<typeof import('../api/client.js')>(
    '../api/client.js',
  );
  return {
    ...actual,
    getApiClient: () => ({
      listCoworkers: listCoworkersSpy,
      getMe: getMeSpy,
      listCoworkerConversations: listConvsSpy,
      setToken: vi.fn(),
    }),
  };
});

import { groupConversations, RmChatShell } from './chat-shell.js';
import type { Conversation, Coworker, Me } from '../api/client.js';

const COWORKER_A: Coworker = {
  id: 'cw-a',
  tenant_id: 't1',
  name: 'Ops coworker',
  folder: 'ops',
  agent_backend: 'claude',
  status: 'idle',
  agent_role: 'operations',
  max_concurrent: 1,
  created_at: '2025-01-01T00:00:00Z',
};

const COWORKER_B: Coworker = {
  ...COWORKER_A,
  id: 'cw-b',
  name: 'Finance coworker',
  folder: 'finance',
  agent_role: 'finance',
};

const ME: Me = {
  user_id: 'u-1',
  tenant_id: 'tenant-acme',
  name: 'Jerry Guan',
  email: 'j@example.com',
  role: 'owner',
};

function conv(id: string, name: string, when: Date): Conversation {
  return {
    id,
    tenant_id: 't1',
    coworker_id: 'cw-a',
    channel_binding_id: 'ch-1',
    channel_chat_id: 'web:' + id,
    name,
    requires_trigger: true,
    created_at: when.toISOString(),
  };
}

interface LocationStub {
  hrefAssignments: string[];
  hashAssignments: string[];
  hashGetter: string;
  hrefGetter: string;
  restore: () => void;
}

/** Replace location.href / location.hash setters with spy-backed
 *  ones. happy-dom's real setter triggers an async navigation that
 *  we cannot observe synchronously; the stub captures the assigned
 *  string. */
function stubLocation(initialHash = '#/', initialSearch = ''): LocationStub {
  const stub: LocationStub = {
    hrefAssignments: [],
    hashAssignments: [],
    hashGetter: initialHash,
    hrefGetter: 'http://localhost/' + initialSearch + initialHash,
    restore: () => {},
  };
  const desc = {
    hash: Object.getOwnPropertyDescriptor(location, 'hash'),
    href: Object.getOwnPropertyDescriptor(location, 'href'),
    search: Object.getOwnPropertyDescriptor(location, 'search'),
  };
  Object.defineProperty(location, 'hash', {
    configurable: true,
    get: () => stub.hashGetter,
    set: (v: string) => {
      stub.hashAssignments.push(v);
      stub.hashGetter = v;
    },
  });
  Object.defineProperty(location, 'href', {
    configurable: true,
    get: () => stub.hrefGetter,
    set: (v: string) => {
      stub.hrefAssignments.push(v);
      stub.hrefGetter = v;
    },
  });
  Object.defineProperty(location, 'search', {
    configurable: true,
    get: () => initialSearch,
  });
  stub.restore = () => {
    if (desc.hash) Object.defineProperty(location, 'hash', desc.hash);
    if (desc.href) Object.defineProperty(location, 'href', desc.href);
    if (desc.search) Object.defineProperty(location, 'search', desc.search);
  };
  return stub;
}

async function settle(el: RmChatShell): Promise<void> {
  for (let i = 0; i < 30; i += 1) {
    await Promise.resolve();
    await el.updateComplete;
  }
}

async function mountShell(): Promise<RmChatShell> {
  const el = document.createElement('rm-chat-shell') as RmChatShell;
  document.body.appendChild(el);
  await settle(el);
  return el;
}

describe('groupConversations', () => {
  // Anchor "now" at noon UTC on a known date so the bucket math is
  // deterministic across timezones — the function bucketises by
  // user-local start-of-day, so we feed in a `now` rather than
  // relying on test clock state.
  const now = new Date('2026-05-22T12:00:00Z');

  it('groups items at start of today into Today', () => {
    const startOfToday = new Date(
      now.getFullYear(),
      now.getMonth(),
      now.getDate(),
    );
    const g = groupConversations(
      [conv('1', 'a', startOfToday), conv('2', 'b', now)],
      now,
    );
    expect(g.length).toBe(1);
    expect(g[0].label).toBe('Today');
    expect(g[0].items.map((c) => c.id)).toEqual(['1', '2']);
  });

  it('groups items from the previous local day into Yesterday', () => {
    const startOfToday = new Date(
      now.getFullYear(),
      now.getMonth(),
      now.getDate(),
    );
    const yest = new Date(startOfToday.getTime() - 3600 * 1000);
    const g = groupConversations([conv('y1', 'yest', yest)], now);
    expect(g.length).toBe(1);
    expect(g[0].label).toBe('Yesterday');
  });

  it('falls through to Earlier for anything older than yesterday', () => {
    const older = new Date('2026-05-01T00:00:00Z');
    const g = groupConversations([conv('o', 'old', older)], now);
    expect(g[0].label).toBe('Earlier');
  });

  it('omits empty groups so the rail does not render dead labels', () => {
    const today = new Date(now.getTime());
    const old = new Date('2025-01-01T00:00:00Z');
    const g = groupConversations(
      [conv('t', 't', today), conv('o', 'o', old)],
      now,
    );
    expect(g.map((x) => x.label)).toEqual(['Today', 'Earlier']);
  });
});

describe('<rm-chat-shell>', () => {
  let loc: LocationStub;

  beforeEach(() => {
    [listCoworkersSpy, getMeSpy, listConvsSpy].forEach((s) => s.mockReset());
    listCoworkersSpy.mockResolvedValue([COWORKER_A, COWORKER_B]);
    getMeSpy.mockResolvedValue(ME);
    listConvsSpy.mockResolvedValue([
      conv('c-1', 'Today thread', new Date()),
    ]);
    loc = stubLocation('#/', '');
    // localStorage is shared across tests; chat-shell sets the
    // chat-panel collapse flag on mount. Reset between cases so we
    // can assert it independently.
    localStorage.removeItem('rm-sidebar-collapsed');
  });

  afterEach(() => {
    document.querySelectorAll('rm-chat-shell').forEach((el) => el.remove());
    document.querySelectorAll('rm-chat-panel').forEach((el) => el.remove());
    loc.restore();
  });

  it('collapses the chat-panel inner sidebar by writing localStorage on mount', async () => {
    await mountShell();
    expect(localStorage.getItem('rm-sidebar-collapsed')).toBe('true');
  });

  it('lays out reauth banner above a sidebar+main grid (not as a grid item)', async () => {
    // Regression: connectedCallback used to set `style.display = 'block'`
    // inline, which beat the rendered <style> grid rule on
    // specificity. The banner then became grid item #1, the
    // sidebar got pushed into the 1fr column, and main wrapped to
    // a new row. Pin the flex-column host + .cs-layout grid wrapper
    // so this can't regress silently.
    const el = await mountShell();
    expect(el.style.display).toBe('flex');
    expect(el.style.flexDirection).toBe('column');
    const layout = el.querySelector('.cs-layout');
    expect(layout, '.cs-layout wrapper must exist').not.toBeNull();
    const sidebar = el.querySelector('.cs-sidebar');
    const main = el.querySelector('.cs-main');
    expect(layout?.contains(sidebar!)).toBe(true);
    expect(layout?.contains(main!)).toBe(true);
  });

  it('renders one row in the coworker switcher menu per coworker', async () => {
    const el = await mountShell();
    el.querySelector<HTMLButtonElement>(
      '[data-testid="coworker-switcher"]',
    )!.click();
    await settle(el);
    const rows = el.querySelectorAll('[data-testid="coworker-option"]');
    expect(rows.length).toBe(2);
    const names = Array.from(rows).map((r) => r.textContent?.trim());
    expect(names!.some((n) => n?.includes('Ops coworker'))).toBe(true);
    expect(names!.some((n) => n?.includes('Finance coworker'))).toBe(true);
  });

  it('clicking another coworker navigates with a new ?agent_id', async () => {
    const el = await mountShell();
    el.querySelector<HTMLButtonElement>(
      '[data-testid="coworker-switcher"]',
    )!.click();
    await settle(el);
    const finance = el.querySelector<HTMLButtonElement>(
      '[data-coworker-id="cw-b"]',
    )!;
    finance.click();
    expect(loc.hrefAssignments.length).toBe(1);
    expect(loc.hrefAssignments[0]).toContain('agent_id=cw-b');
    // Switching coworker must reset chat — chat_id must NOT be in
    // the new URL so chat-panel starts fresh.
    expect(loc.hrefAssignments[0]).not.toContain('chat_id=');
  });

  it('clicking "Manage coworkers…" goes to the settings shell hash', async () => {
    const el = await mountShell();
    el.querySelector<HTMLButtonElement>(
      '[data-testid="coworker-switcher"]',
    )!.click();
    await settle(el);
    el.querySelector<HTMLButtonElement>(
      '[data-testid="manage-coworkers"]',
    )!.click();
    expect(loc.hashAssignments).toEqual(['#/manage/coworkers']);
  });

  it('topbar Activity button hashes to #/activity', async () => {
    const el = await mountShell();
    el.querySelector<HTMLButtonElement>(
      '[data-testid="topbar-activity"]',
    )!.click();
    expect(loc.hashAssignments).toEqual(['#/activity']);
  });

  it('topbar Settings button hashes to #/manage/coworkers', async () => {
    const el = await mountShell();
    el.querySelector<HTMLButtonElement>(
      '[data-testid="topbar-settings"]',
    )!.click();
    expect(loc.hashAssignments).toEqual(['#/manage/coworkers']);
  });

  it('topbar Approvals button toggles the popover placeholder', async () => {
    const el = await mountShell();
    expect(el.querySelector('[data-menu="approvals"]')).toBeNull();
    el.querySelector<HTMLButtonElement>(
      '[data-testid="topbar-approvals"]',
    )!.click();
    await settle(el);
    expect(el.querySelector('[data-menu="approvals"]')).not.toBeNull();
    // Hash MUST NOT change — Approvals is a popover, not a route.
    expect(loc.hashAssignments).toEqual([]);
  });

  it('does not show a non-zero approvals badge until v2-C wires it', async () => {
    const el = await mountShell();
    const apprBtn = el.querySelector<HTMLButtonElement>(
      '[data-testid="topbar-approvals"]',
    )!;
    // Badge is rendered only when approvalsBadge > 0; locked to 0 in v2-A.
    expect(apprBtn.querySelector('.bdg')).toBeNull();
  });

  it('clicking a conversation row navigates with a new ?chat_id', async () => {
    listConvsSpy.mockResolvedValue([conv('cv-9', 'My chat', new Date())]);
    const el = await mountShell();
    const row = el.querySelector<HTMLButtonElement>(
      '[data-testid="conversation-row"]',
    )!;
    expect(row).not.toBeNull();
    row.click();
    expect(loc.hrefAssignments.length).toBe(1);
    expect(loc.hrefAssignments[0]).toContain('chat_id=cv-9');
    // It MUST also include the current agent_id so chat-panel still
    // knows which coworker owns the conversation.
    expect(loc.hrefAssignments[0]).toContain('agent_id=cw-a');
  });

  it('opens the user-pill menu and exposes Settings + Log out', async () => {
    const el = await mountShell();
    el.querySelector<HTMLButtonElement>(
      '[data-testid="user-pill"]',
    )!.click();
    await settle(el);
    expect(el.querySelector('[data-testid="user-menu-settings"]'))
      .not.toBeNull();
    expect(el.querySelector('[data-testid="user-menu-logout"]'))
      .not.toBeNull();
  });

  it('user-menu Settings entry routes to #/manage/coworkers', async () => {
    const el = await mountShell();
    el.querySelector<HTMLButtonElement>(
      '[data-testid="user-pill"]',
    )!.click();
    await settle(el);
    el.querySelector<HTMLButtonElement>(
      '[data-testid="user-menu-settings"]',
    )!.click();
    expect(loc.hashAssignments).toEqual(['#/manage/coworkers']);
  });

  it('renders the tenant pill with the tenant identifier from /me', async () => {
    const el = await mountShell();
    const pill = el.querySelector('[data-testid="tenant-pill"]')!;
    // The first 12 chars of the tenant id are shown so the pill
    // does not overflow; full tenant slug is a v3 deliverable.
    expect(pill.textContent).toContain('tenant-acme');
    expect(pill.textContent).toContain('prod');
  });

  it('does not crash when listCoworkers fails', async () => {
    listCoworkersSpy.mockRejectedValue(new Error('boom'));
    const el = await mountShell();
    // Sidebar must still render the brand + the empty user pill so
    // the user has some way to escape (log out).
    expect(el.querySelector('.cs-brand')).not.toBeNull();
    expect(el.querySelector('[data-testid="user-pill"]')).not.toBeNull();
  });
});
