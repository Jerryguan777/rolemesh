// @vitest-environment happy-dom
// <rm-chat-shell> — pins the contract that the shell exposes to
// users:
//   * topbar icons each route to the correct hash
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
const listMessagesSpy = vi.fn();
const createConvSpy = vi.fn();
const listModelsSpy = vi.fn();
const listPendingApprovalsSpy = vi.fn();

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
      listMessages: listMessagesSpy,
      createCoworkerConversation: createConvSpy,
      listModels: listModelsSpy,
      // The slotted <rm-approvals-inbox> self-fetches on mount.
      listPendingApprovals: listPendingApprovalsSpy,
      setToken: vi.fn(),
    }),
  };
});

import { groupConversations, RmChatShell } from './chat-shell.js';
import { connectionState } from '../ws/connection-state.js';
import type {
  Conversation,
  Coworker,
  Me,
} from '../api/client.js';

const COWORKER_A: Coworker = {
  id: 'cw-a',
  tenant_id: 't1',
  name: 'Ops coworker',
  folder: 'ops',
  agent_backend: 'claude',
  status: 'idle',
  max_concurrent: 1,
  created_at: '2025-01-01T00:00:00Z',
};

const COWORKER_B: Coworker = {
  ...COWORKER_A,
  id: 'cw-b',
  name: 'Finance coworker',
  folder: 'finance',
};

const ME: Me = {
  user_id: 'u-1',
  tenant_id: 'tenant-acme',
  name: 'Jerry Guan',
  email: 'j@example.com',
  role: 'owner',
  plane: 'tenant',
  capabilities: [],
};

function conv(id: string, name: string, when: Date): Conversation {
  return {
    id,
    tenant_id: 't1',
    coworker_id: 'cw-a',
    channel_binding_id: 'ch-1',
    channel_chat_id: 'web:' + id,
    name,
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

  it('groups items at start of today into Today, newest-first within the bucket', () => {
    const startOfToday = new Date(
      now.getFullYear(),
      now.getMonth(),
      now.getDate(),
    );
    // Input order is oldest-then-newest; output must be flipped so
    // a freshly-created chat lands at the top of the rail.
    const g = groupConversations(
      [conv('1', 'a', startOfToday), conv('2', 'b', now)],
      now,
    );
    expect(g.length).toBe(1);
    expect(g[0].label).toBe('Today');
    expect(g[0].items.map((c) => c.id)).toEqual(['2', '1']);
  });

  it('sorts Today bucket newest-first regardless of input order', () => {
    // Three rows scattered in input order; pin the output is
    // strictly newest → oldest within Today.
    const t0 = new Date('2026-05-23T08:00:00Z');
    const t1 = new Date('2026-05-23T10:00:00Z');
    const t2 = new Date('2026-05-23T12:00:00Z');
    const fixedNow = new Date('2026-05-23T13:00:00Z');
    const g = groupConversations(
      [conv('a', 'a', t0), conv('c', 'c', t2), conv('b', 'b', t1)],
      fixedNow,
    );
    expect(g[0].items.map((c) => c.id)).toEqual(['c', 'b', 'a']);
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

  /** Global fetch stub — the slotted chat-panel's WS clients reach for
   *  `POST /api/v1/auth/ws-ticket` on mount; happy-dom's real fetch
   *  makes a TCP connection and times out. Returning 404 lets the
   *  client resolve to a closed socket and the shell keeps rendering. */
  let originalFetch: typeof globalThis.fetch;

  beforeEach(() => {
    [
      listCoworkersSpy,
      getMeSpy,
      listConvsSpy,
      listMessagesSpy,
      createConvSpy,
      listModelsSpy,
      listPendingApprovalsSpy,
    ].forEach((s) => s.mockReset());
    listPendingApprovalsSpy.mockResolvedValue([]);
    listCoworkersSpy.mockResolvedValue([COWORKER_A, COWORKER_B]);
    getMeSpy.mockResolvedValue(ME);
    listConvsSpy.mockResolvedValue([
      conv('c-1', 'Today thread', new Date()),
    ]);
    listMessagesSpy.mockResolvedValue([]);
    // Default: createCoworkerConversation produces a shell row the
    // tests can match by id. Individual tests override when they
    // need to assert a specific creation.
    createConvSpy.mockResolvedValue({
      id: 'created-default',
      created_at: '2026-05-23T00:00:00Z',
    });
    listModelsSpy.mockResolvedValue([]);
    originalFetch = globalThis.fetch;
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify({ code: 'NOT_FOUND' }), { status: 404 }),
      ) as unknown as typeof globalThis.fetch;
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
    globalThis.fetch = originalFetch;
  });

  it('collapses the chat-panel inner sidebar by writing localStorage on mount', async () => {
    await mountShell();
    expect(localStorage.getItem('rm-sidebar-collapsed')).toBe('true');
  });

  it('main column carries min-height:0 + overflow:hidden so chat scroll stays internal', async () => {
    // Regression: without these two rules, .cs-main sizes to the
    // chat-panel's content (grid items default to min-height:auto).
    // The whole .cs-layout then overflows upward, the outer shell
    // becomes the scroll container, and the sidebar scrolls in
    // lockstep with the chat. Pin the CSS contract so a future
    // refactor doesn't silently bring back the dual-scroll bug.
    const el = await mountShell();
    const main = el.querySelector('.cs-main') as HTMLElement | null;
    expect(main).not.toBeNull();
    const cs = getComputedStyle(main!);
    // happy-dom reports the raw "0" — browsers normalise to "0px";
    // accept either since we only care about the value semantics.
    expect(['0', '0px']).toContain(cs.minHeight);
    expect(cs.overflow).toBe('hidden');
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

  it('clicking another coworker lands on that coworker AND its latest existing conversation', async () => {
    // Mock cw-b's history with two rows; the newer one must win.
    listConvsSpy.mockResolvedValue([
      conv('older', null as unknown as string, new Date('2026-05-01')),
      conv('newest', null as unknown as string, new Date('2026-05-22')),
    ]);
    const el = await mountShell();
    el.querySelector<HTMLButtonElement>(
      '[data-testid="coworker-switcher"]',
    )!.click();
    await settle(el);
    const finance = el.querySelector<HTMLButtonElement>(
      '[data-coworker-id="cw-b"]',
    )!;
    finance.click();
    await settle(el);
    // One navigation only — we resolve the chat_id pre-navigate so
    // the next page lands connected, not on a Disconnected blank.
    expect(loc.hrefAssignments.length).toBe(1);
    expect(loc.hrefAssignments[0]).toContain('agent_id=cw-b');
    expect(loc.hrefAssignments[0]).toContain('chat_id=newest');
  });

  it('bootstrap: when URL has agent_id but no chat_id, replaceState injects the latest conv id', async () => {
    // Stub the URL to have agent_id but no chat_id, so bootstrap
    // has to find a chat for it. The sidebar conv list returns one
    // row; bootstrap should pick it and update the URL.
    loc.restore();
    loc = stubLocation('#/', '?agent_id=cw-a');
    listConvsSpy.mockResolvedValue([
      conv('boot-cv', null as unknown as string, new Date('2026-05-23')),
    ]);
    const replaceSpy = vi
      .spyOn(history, 'replaceState')
      .mockImplementation(() => {});
    try {
      await mountShell();
      const replaceUrls = replaceSpy.mock.calls.map(
        (c) => String(c[2] ?? ''),
      );
      const found = replaceUrls.find((u) => u.includes('chat_id=boot-cv'));
      expect(found, 'history.replaceState must include chat_id').toBeDefined();
      // No full reload — replaceState only.
      expect(loc.hrefAssignments).toHaveLength(0);
    } finally {
      replaceSpy.mockRestore();
    }
  });

  it('bootstrap: empty coworker → POSTs a fresh conversation, then replaceState', async () => {
    loc.restore();
    loc = stubLocation('#/', '?agent_id=cw-a');
    listConvsSpy.mockResolvedValue([]);
    createConvSpy.mockResolvedValue({
      id: 'boot-new',
      created_at: '2026-05-23T00:00:00Z',
    });
    const replaceSpy = vi
      .spyOn(history, 'replaceState')
      .mockImplementation(() => {});
    try {
      await mountShell();
      expect(createConvSpy).toHaveBeenCalledWith('cw-a');
      const replaceUrls = replaceSpy.mock.calls.map(
        (c) => String(c[2] ?? ''),
      );
      expect(
        replaceUrls.some((u) => u.includes('chat_id=boot-new')),
        'replaceState should carry the freshly-created chat_id',
      ).toBe(true);
    } finally {
      replaceSpy.mockRestore();
    }
  });

  it('+ New chat creates a conversation upfront, then navigates with chat_id', async () => {
    createConvSpy.mockResolvedValue({
      id: 'fresh-on-newchat',
      created_at: '2026-05-23T00:00:00Z',
    });
    const el = await mountShell();
    el.querySelector<HTMLButtonElement>('[data-testid="new-chat"]')!.click();
    await settle(el);
    expect(createConvSpy).toHaveBeenCalledWith('cw-a');
    const navUrl = loc.hrefAssignments.at(-1) ?? '';
    expect(navUrl).toContain('chat_id=fresh-on-newchat');
  });

  it('chat-panel only mounts AFTER bootstrap resolves (avoids stale-URL paint)', async () => {
    // Block bootstrap on a pending promise — chat-panel must stay
    // unmounted while it waits. This is the whole point of the
    // `bootstrapped` gate: chat-panel reads URL params in its
    // constructor, so we must NOT mount it before the URL is fixed
    // up with the resolved chat_id.
    let resolveListCoworkers: (v: typeof COWORKER_A[]) => void = () => {};
    listCoworkersSpy.mockImplementation(
      () =>
        new Promise((r) => {
          resolveListCoworkers = r;
        }),
    );
    const el = document.createElement('rm-chat-shell') as RmChatShell;
    document.body.appendChild(el);
    // One render tick — enough to paint the loading placeholder, but
    // listCoworkers is still pending so bootstrap can't complete.
    await el.updateComplete;
    await el.updateComplete;
    expect(el.querySelector('rm-chat-panel')).toBeNull();
    expect(
      el.querySelector('[data-testid="chat-bootstrapping"]'),
    ).not.toBeNull();
    // Release bootstrap; chat-panel should mount on the next paint.
    resolveListCoworkers([COWORKER_A, COWORKER_B]);
    await settle(el);
    expect(el.querySelector('rm-chat-panel')).not.toBeNull();
    expect(
      el.querySelector('[data-testid="chat-bootstrapping"]'),
    ).toBeNull();
  });

  it('switches to a coworker with zero history by POSTing a new conversation first', async () => {
    listConvsSpy.mockResolvedValue([]); // cw-b has nothing yet
    createConvSpy.mockResolvedValue({
      id: 'fresh-cv',
      created_at: '2026-05-23T00:00:00Z',
    });
    const el = await mountShell();
    el.querySelector<HTMLButtonElement>(
      '[data-testid="coworker-switcher"]',
    )!.click();
    await settle(el);
    el.querySelector<HTMLButtonElement>(
      '[data-coworker-id="cw-b"]',
    )!.click();
    await settle(el);
    expect(createConvSpy).toHaveBeenCalledWith('cw-b');
    expect(loc.hrefAssignments[0]).toContain('chat_id=fresh-cv');
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

  describe('approvals inbox integration', () => {
    it('renders an approvals trigger in the top-bar icon strip', async () => {
      const el = await mountShell();
      expect(
        el.querySelector('[data-testid="topbar-approvals"]'),
      ).not.toBeNull();
      // It is NOT a hash-route button — it toggles a popover, so it must
      // not navigate.
      el.querySelector<HTMLButtonElement>(
        '[data-testid="topbar-approvals"]',
      )!.click();
      expect(loc.hashAssignments).toEqual([]);
    });

    it('clicking the trigger opens the inbox panel; clicking again closes it', async () => {
      const el = await mountShell();
      // Closed by default — the inbox renders nothing.
      expect(el.querySelector('[data-testid="approvals-panel"]')).toBeNull();
      const btn = el.querySelector<HTMLButtonElement>(
        '[data-testid="topbar-approvals"]',
      )!;
      btn.click();
      await settle(el);
      expect(
        el.querySelector('[data-testid="approvals-panel"]'),
      ).not.toBeNull();
      btn.click();
      await settle(el);
      expect(el.querySelector('[data-testid="approvals-panel"]')).toBeNull();
    });

    it('no badge when the inbox reports zero pending', async () => {
      const el = await mountShell();
      expect(el.querySelector('[data-testid="approvals-badge"]')).toBeNull();
    });

    it('paints a badge with the pending count from the inbox count event', async () => {
      const el = await mountShell();
      const inbox = el.querySelector('rm-approvals-inbox')!;
      inbox.dispatchEvent(
        new CustomEvent('approvals-count', {
          detail: { total: 3, urgent: 0 },
          bubbles: true,
          composed: true,
        }),
      );
      await settle(el);
      const badge = el.querySelector('[data-testid="approvals-badge"]');
      expect(badge).not.toBeNull();
      expect(badge?.textContent?.trim()).toBe('3');
      expect(badge?.getAttribute('data-urgent')).toBe('false');
    });

    it('deepens the badge (data-urgent) when at least one item is expiring soon', async () => {
      const el = await mountShell();
      const inbox = el.querySelector('rm-approvals-inbox')!;
      inbox.dispatchEvent(
        new CustomEvent('approvals-count', {
          detail: { total: 2, urgent: 1 },
          bubbles: true,
          composed: true,
        }),
      );
      await settle(el);
      expect(
        el
          .querySelector('[data-testid="approvals-badge"]')
          ?.getAttribute('data-urgent'),
      ).toBe('true');
    });

    it('re-pulls the inbox when the chat-panel bubbles approval-activity', async () => {
      const el = await mountShell();
      // The inbox already seeded once on mount; isolate the trigger.
      listPendingApprovalsSpy.mockClear();
      // chat-panel dispatches this bubbling+composed on requested/resolved.
      el.querySelector('rm-chat-panel')!.dispatchEvent(
        new CustomEvent('approval-activity', {
          bubbles: true,
          composed: true,
        }),
      );
      await settle(el);
      expect(listPendingApprovalsSpy).toHaveBeenCalled();
    });
  });

  it('clicking a conversation row navigates with a new ?chat_id', async () => {
    // Two rows — bootstrap auto-selects the newest, so we click the
    // OLDER one to assert that navigation actually fires (clicking
    // the already-active row is a documented no-op).
    listConvsSpy.mockResolvedValue([
      conv('cv-old', 'Older chat', new Date('2026-04-01')),
      conv('cv-new', 'Newer chat', new Date('2026-05-22')),
    ]);
    const el = await mountShell();
    const older = el.querySelector<HTMLButtonElement>(
      '[data-conv-id="cv-old"]',
    )!;
    expect(older).not.toBeNull();
    older.click();
    expect(loc.hrefAssignments.length).toBe(1);
    expect(loc.hrefAssignments[0]).toContain('chat_id=cv-old');
    // It MUST also include the current agent_id so chat-panel still
    // knows which coworker owns the conversation.
    expect(loc.hrefAssignments[0]).toContain('agent_id=cw-a');
  });

  describe('sidebar search', () => {
    // Set up 3 named rows so we can prove the filter narrows by name.
    function mockSearchableConversations(): void {
      listConvsSpy.mockResolvedValue([
        conv('cv-finance', 'Finance review Q3', new Date('2026-05-23T10:00:00Z')),
        conv('cv-ops', 'Ops postmortem', new Date('2026-05-23T09:00:00Z')),
        conv('cv-design', 'Design sprint kickoff', new Date('2026-05-23T08:00:00Z')),
      ]);
    }

    it('clicking the search button swaps it for an input', async () => {
      mockSearchableConversations();
      const el = await mountShell();
      expect(el.querySelector('[data-testid="search-input"]')).toBeNull();
      el.querySelector<HTMLButtonElement>(
        '[data-testid="search-conversations"]',
      )!.click();
      await settle(el);
      expect(el.querySelector('[data-testid="search-conversations"]')).toBeNull();
      expect(el.querySelector('[data-testid="search-input"]')).not.toBeNull();
    });

    it('filters conversation rows by case-insensitive name substring', async () => {
      // Pin active to cv-ops so the implicit "always keep active row"
      // rule does not also keep cv-finance (the first row, which the
      // shell would otherwise auto-pick as active).
      loc.restore();
      loc = stubLocation('#/', '?agent_id=cw-a&chat_id=cv-ops');
      mockSearchableConversations();
      const el = await mountShell();
      el.querySelector<HTMLButtonElement>(
        '[data-testid="search-conversations"]',
      )!.click();
      await settle(el);
      const input = el.querySelector<HTMLInputElement>(
        '[data-testid="search-input"]',
      )!;
      input.value = 'OPS';
      input.dispatchEvent(new Event('input'));
      await settle(el);
      const rows = el.querySelectorAll('[data-testid="conversation-row"]');
      const ids = Array.from(rows).map((r) => r.getAttribute('data-conv-id'));
      // Only the Ops row should match a case-insensitive "OPS" search.
      expect(ids).toContain('cv-ops');
      expect(ids).not.toContain('cv-finance');
      expect(ids).not.toContain('cv-design');
    });

    it('filters by preview text when the row has no Conversation.name', async () => {
      listConvsSpy.mockResolvedValue([
        conv('cv-x', null as unknown as string, new Date('2026-05-23T10:00:00Z')),
        conv('cv-y', null as unknown as string, new Date('2026-05-23T09:00:00Z')),
      ]);
      // listMessages is called per-conversation; route by id so the
      // two rows get distinct preview text.
      listMessagesSpy.mockImplementation((id: string) =>
        Promise.resolve([
          {
            id: 'm-' + id,
            role: 'user',
            content:
              id === 'cv-x' ? 'Ship the migration tonight' : 'Hire a backend lead',
            timestamp: '2026-05-23T00:00:00Z',
          },
        ]),
      );
      const el = await mountShell();
      el.querySelector<HTMLButtonElement>(
        '[data-testid="search-conversations"]',
      )!.click();
      await settle(el);
      const input = el.querySelector<HTMLInputElement>(
        '[data-testid="search-input"]',
      )!;
      input.value = 'migration';
      input.dispatchEvent(new Event('input'));
      await settle(el);
      const rows = el.querySelectorAll('[data-testid="conversation-row"]');
      const ids = Array.from(rows).map((r) => r.getAttribute('data-conv-id'));
      expect(ids).toContain('cv-x');
      expect(ids).not.toContain('cv-y');
    });

    it('clearing the input restores the full list', async () => {
      mockSearchableConversations();
      const el = await mountShell();
      el.querySelector<HTMLButtonElement>(
        '[data-testid="search-conversations"]',
      )!.click();
      await settle(el);
      const input = el.querySelector<HTMLInputElement>(
        '[data-testid="search-input"]',
      )!;
      input.value = 'finance';
      input.dispatchEvent(new Event('input'));
      await settle(el);
      expect(el.querySelectorAll('[data-testid="conversation-row"]').length).toBe(1);
      input.value = '';
      input.dispatchEvent(new Event('input'));
      await settle(el);
      expect(el.querySelectorAll('[data-testid="conversation-row"]').length).toBe(3);
    });

    it('close button restores the button + drops the filter', async () => {
      mockSearchableConversations();
      const el = await mountShell();
      el.querySelector<HTMLButtonElement>(
        '[data-testid="search-conversations"]',
      )!.click();
      await settle(el);
      const input = el.querySelector<HTMLInputElement>(
        '[data-testid="search-input"]',
      )!;
      input.value = 'finance';
      input.dispatchEvent(new Event('input'));
      await settle(el);
      expect(el.querySelectorAll('[data-testid="conversation-row"]').length).toBe(1);
      el.querySelector<HTMLButtonElement>(
        '[data-testid="search-close"]',
      )!.click();
      await settle(el);
      expect(el.querySelector('[data-testid="search-input"]')).toBeNull();
      expect(
        el.querySelector('[data-testid="search-conversations"]'),
      ).not.toBeNull();
      // Filter cleared, full list restored.
      expect(el.querySelectorAll('[data-testid="conversation-row"]').length).toBe(3);
    });

    it('keeps the active conversation visible even if it does not match', async () => {
      loc.restore();
      loc = stubLocation('#/', '?agent_id=cw-a&chat_id=cv-active');
      listConvsSpy.mockResolvedValue([
        conv('cv-active', 'Active thread', new Date('2026-05-23T10:00:00Z')),
        conv('cv-other', 'Other thread', new Date('2026-05-23T09:00:00Z')),
      ]);
      const el = await mountShell();
      el.querySelector<HTMLButtonElement>(
        '[data-testid="search-conversations"]',
      )!.click();
      await settle(el);
      const input = el.querySelector<HTMLInputElement>(
        '[data-testid="search-input"]',
      )!;
      // "zzz" matches nothing. Active row must still render so the
      // user cannot lose the page they're currently on by typing.
      input.value = 'zzz';
      input.dispatchEvent(new Event('input'));
      await settle(el);
      const ids = Array.from(
        el.querySelectorAll('[data-testid="conversation-row"]'),
      ).map((r) => r.getAttribute('data-conv-id'));
      expect(ids).toContain('cv-active');
      expect(ids).not.toContain('cv-other');
    });
  });

  it('uses Conversation.name as the row label when one is set', async () => {
    listConvsSpy.mockResolvedValue([conv('cv-9', 'My chat', new Date())]);
    const el = await mountShell();
    const row = el.querySelector('[data-testid="conversation-row"]');
    expect(row?.textContent?.trim()).toBe('My chat');
    // No need to fetch messages when the name already provides a label,
    // but the implementation still warms previews in the background;
    // we don't pin that — only the visible label matters here.
  });

  it('falls back to the first user message when Conversation.name is null', async () => {
    listConvsSpy.mockResolvedValue([
      conv('cv-null', null as unknown as string, new Date()),
    ]);
    listMessagesSpy.mockResolvedValue([
      {
        id: 'm-sys',
        role: 'assistant',
        content: 'Hello, how can I help?',
        timestamp: '2026-05-23T00:00:00Z',
      },
      {
        id: 'm-user',
        role: 'user',
        content: 'Help me ship the v2 UI redesign',
        timestamp: '2026-05-23T00:00:01Z',
      },
    ]);
    const el = await mountShell();
    const row = el.querySelector('[data-testid="conversation-row"]');
    expect(row?.textContent?.trim()).toBe('Help me ship the v2 UI redesign');
  });

  it('truncates a long first message with an ellipsis (~48 chars)', async () => {
    const longMsg =
      'This is an unusually verbose user message that should ' +
      'absolutely get truncated to fit the sidebar rail without ' +
      'wrapping or breaking the layout in unpleasant ways.';
    listConvsSpy.mockResolvedValue([
      conv('cv-long', null as unknown as string, new Date()),
    ]);
    listMessagesSpy.mockResolvedValue([
      {
        id: 'm-1',
        role: 'user',
        content: longMsg,
        timestamp: '2026-05-23T00:00:00Z',
      },
    ]);
    const el = await mountShell();
    const row = el.querySelector('[data-testid="conversation-row"]');
    const text = row?.textContent?.trim() ?? '';
    expect(text.length).toBeLessThan(longMsg.length);
    expect(text.endsWith('…')).toBe(true);
  });

  it('shows "New chat" when there is neither name nor any messages', async () => {
    listConvsSpy.mockResolvedValue([
      conv('cv-empty', null as unknown as string, new Date()),
    ]);
    listMessagesSpy.mockResolvedValue([]);
    const el = await mountShell();
    const row = el.querySelector('[data-testid="conversation-row"]');
    expect(row?.textContent?.trim()).toBe('New chat');
  });

  it('hides unnamed empty conversations from history, except the active one', async () => {
    loc.restore();
    loc = stubLocation('#/', '?agent_id=cw-a&chat_id=cv-active');
    // Three rows:
    //   cv-active — unnamed, no messages — IS the active conv, must stay
    //   cv-ghost  — unnamed, no messages — must be hidden
    //   cv-named  — named, no messages — must stay (user gave it a label)
    //   cv-real   — unnamed, has a message — must stay (preview wins)
    listConvsSpy.mockResolvedValue([
      conv('cv-active', null as unknown as string, new Date('2026-05-23')),
      conv('cv-ghost', null as unknown as string, new Date('2026-05-22')),
      conv('cv-named', 'Quarterly report', new Date('2026-05-21')),
      conv('cv-real', null as unknown as string, new Date('2026-05-20')),
    ]);
    listMessagesSpy.mockImplementation((id: string) => {
      if (id === 'cv-real') {
        return Promise.resolve([
          {
            id: 'm-1',
            role: 'user' as const,
            content: 'How is Q3 tracking?',
            timestamp: '2026-05-20T00:00:00Z',
          },
        ]);
      }
      return Promise.resolve([]);
    });
    const el = await mountShell();
    const rows = el.querySelectorAll('[data-testid="conversation-row"]');
    const ids = Array.from(rows).map((r) => r.getAttribute('data-conv-id'));
    expect(ids).toContain('cv-active');
    expect(ids).toContain('cv-named');
    expect(ids).toContain('cv-real');
    // The ghost row must be filtered out.
    expect(ids).not.toContain('cv-ghost');
  });

  it('a failed listMessages does not blow up the sidebar (row keeps fallback)', async () => {
    listConvsSpy.mockResolvedValue([
      conv('cv-bad', null as unknown as string, new Date()),
    ]);
    listMessagesSpy.mockRejectedValue(new Error('boom'));
    const el = await mountShell();
    const row = el.querySelector('[data-testid="conversation-row"]');
    expect(row).not.toBeNull();
    expect(row?.textContent?.trim()).toBe('New chat');
  });

  it('tenant pill connection dot reflects ConnectionState directly (no agent-connection event needed)', async () => {
    // Regression for H5: previously the dot only flipped when
    // message-editor bubbled an `agent-connection` event. A single
    // missed dispatch left it stuck. Now the shell subscribes to
    // `ConnectionState`, so a WS client flipping its channel must
    // drive the dot end-to-end without any event relay.
    connectionState.reset();
    const el = await mountShell();
    const dot = () => el.querySelector('[data-testid="connection-dot"]');
    expect(dot()?.getAttribute('data-connected')).toBe('false');
    connectionState.set('v1:conv-a', true);
    await settle(el);
    expect(dot()?.getAttribute('data-connected')).toBe('true');
    connectionState.set('v1:conv-a', false);
    await settle(el);
    expect(dot()?.getAttribute('data-connected')).toBe('false');
    connectionState.reset();
  });

  it('tenant pill connection dot flips to "off" when an agent-connection event reports disconnected', async () => {
    // v2-C dropped chat-panel's standalone "Disconnected" indicator;
    // the dot now lives in chat-shell's tenant pill. Message-editor
    // bubbles `agent-connection` whenever its `connected` prop flips.
    const el = await mountShell();
    // Default render is disconnected (no event yet) — confirm baseline.
    const dot = () =>
      el.querySelector('[data-testid="connection-dot"]');
    expect(dot()?.getAttribute('data-connected')).toBe('false');
    // Simulate the editor's event landing on the shell.
    el.dispatchEvent(
      new CustomEvent('agent-connection', {
        detail: { connected: true },
        bubbles: true,
        composed: true,
      }),
    );
    await settle(el);
    expect(dot()?.getAttribute('data-connected')).toBe('true');
    el.dispatchEvent(
      new CustomEvent('agent-connection', {
        detail: { connected: false },
        bubbles: true,
        composed: true,
      }),
    );
    await settle(el);
    expect(dot()?.getAttribute('data-connected')).toBe('false');
  });

  it('renders the coworker subtitle as Backend · Model', async () => {
    // Pin the v2-C label: the sidebar coworker switcher subtitle is
    // "Backend · Model display_name", looked up via the models map.
    listCoworkersSpy.mockResolvedValue([
      // agent_backend=claude, model_id=mdl-1
      { ...COWORKER_A, model_id: 'mdl-1' },
    ]);
    listModelsSpy.mockResolvedValue([
      {
        id: 'mdl-1',
        provider: 'anthropic',
        model_id: 'claude-sonnet-4-7',
        model_family: 'claude-sonnet',
        display_name: 'Claude Sonnet 4.7',
        is_active: true,
      },
    ]);
    const el = await mountShell();
    const switcher = el.querySelector(
      '[data-testid="coworker-switcher"] .csw-txt',
    );
    expect(switcher?.textContent).toContain('Claude');
    expect(switcher?.textContent).toContain('Claude Sonnet 4.7');
  });

  it('renders a 2-tone wordmark (Role + Mesh) without the legacy R square', async () => {
    const el = await mountShell();
    const brand = el.querySelector('.cs-brand');
    expect(brand).not.toBeNull();
    // The "R" accent-coloured square used to live here. Pin its
    // absence so a future revert doesn't sneak back the dup-with-
    // sidebar avatar.
    expect(brand?.querySelector('.mark')).toBeNull();
    // The text reads RoleMesh top-to-bottom — pin the split via the
    // semantic spans so visual tweaks to either half don't break
    // the test, but the structure remains observable.
    const wm = el.querySelector('[data-testid="brand-wordmark"]');
    expect(wm).not.toBeNull();
    expect(wm?.textContent?.replace(/\s+/g, '')).toBe('RoleMesh');
    expect(wm?.querySelector('.cs-brand-pri')?.textContent).toBe('Role');
    expect(wm?.querySelector('.cs-brand-sec')?.textContent).toBe('Mesh');
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
    // No env suffix in test mode (VITE_RM_ENV unset). The old build
    // hard-coded "· prod" here; that was a stale literal, not real
    // environment metadata — we now refuse to invent a label.
    expect(pill.textContent).not.toContain('prod');
    expect(pill.textContent).not.toContain(' · ');
  });

  it('renders an env suffix on the tenant pill when VITE_RM_ENV is set', async () => {
    // Vite exposes import.meta.env as a plain object at runtime; we
    // mutate it for the duration of the test and roll back in finally
    // so other tests still see the default (unset) value.
    const meta = (import.meta as unknown as { env: Record<string, string | undefined> });
    const prev = meta.env.VITE_RM_ENV;
    meta.env.VITE_RM_ENV = 'staging';
    try {
      const el = await mountShell();
      const pill = el.querySelector('[data-testid="tenant-pill"]')!;
      expect(pill.textContent).toContain('staging');
    } finally {
      meta.env.VITE_RM_ENV = prev;
    }
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
