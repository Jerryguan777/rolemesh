// @vitest-environment happy-dom
//
// <rm-approvals-inbox> — adversarial coverage of the triage popover
// (.hitl-ui/spec.md §4). The store lives IN the component (it self-fetches
// the tenant-wide pending set); the chat card is the only decision surface,
// so the inbox must never expose approve/reject nor emit a decision frame.
//
// Anti-mirror stance: we drive real DOM (clicks, property flips, document
// events) and assert externally observable effects — fetch calls, emitted
// events, scroll/highlight side effects — never the component's private
// state. The pure helpers (paramsInline / formatCountdown / isUrgent) are
// tested against the spec's stated truncation + threshold rules, including
// the boundary cases the row rendering turns on.

import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest';

const listPendingSpy = vi.fn();

vi.mock('../api/client.js', async () => {
  const actual = await vi.importActual<typeof import('../api/client.js')>(
    '../api/client.js',
  );
  return {
    ...actual,
    getApiClient: () => ({
      listPendingApprovals: listPendingSpy,
    }),
  };
});

import {
  ApprovalsInbox,
  paramsInline,
  formatCountdown,
  isUrgent,
} from './approvals-inbox.js';
import type {
  Conversation,
  Coworker,
  PendingApprovalRequest,
} from '../api/client.js';

// --- fixtures ---------------------------------------------------------------

const COWORKER_OPS: Coworker = {
  id: 'cw-ops',
  tenant_id: 't1',
  name: 'Ops',
  folder: 'ops',
  agent_backend: 'claude',
} as unknown as Coworker;

const COWORKER_LOG: Coworker = {
  ...COWORKER_OPS,
  id: 'cw-log',
  name: 'Logistics',
};

const CONV_OPS: Conversation = {
  id: 'conv-ops',
  tenant_id: 't1',
  coworker_id: 'cw-ops',
  channel_binding_id: 'ch-1',
  channel_chat_id: 'web:conv-ops',
  name: 'Q2 ad spend review',
  requires_trigger: true,
  created_at: '2026-05-30T00:00:00Z',
} as unknown as Conversation;

/** Minutes-from-now as an ISO `expires_at`. A few seconds of slack keep
 *  the floor-based countdown (`Xm left`) from rounding down under render
 *  latency — e.g. exactly 18min would otherwise display "17m left". */
function expiresInMin(mins: number): string {
  return new Date(Date.now() + mins * 60_000 + 5_000).toISOString();
}

function req(
  over: Partial<PendingApprovalRequest> & { request_id: string },
): PendingApprovalRequest {
  return {
    conversation_id: 'conv-ops',
    coworker_id: 'cw-ops',
    mcp_server_name: 'amazon-ads-api',
    tool_name: 'campaign.pause',
    action_summary: null,
    requested_at: new Date(Date.now() - 30_000).toISOString(),
    expires_at: expiresInMin(18),
    params: { campaign_id: 'SP-Auto-Hydration', account_id: 'ACME-PRIME' },
    rationale: null,
    ...over,
  } as PendingApprovalRequest;
}

async function settle(el: ApprovalsInbox): Promise<void> {
  for (let i = 0; i < 25; i += 1) {
    await Promise.resolve();
    await el.updateComplete;
  }
}

interface MountOpts {
  rows?: PendingApprovalRequest[];
  open?: boolean;
  coworkers?: Coworker[];
  conversations?: Conversation[];
  activeConversationId?: string | null;
  jumpHandler?: ApprovalsInbox['jumpHandler'];
  onCount?: (detail: { total: number; urgent: number }) => void;
}

async function mount(opts: MountOpts = {}): Promise<ApprovalsInbox> {
  listPendingSpy.mockResolvedValue(opts.rows ?? []);
  const el = document.createElement('rm-approvals-inbox') as ApprovalsInbox;
  if (opts.onCount) {
    el.addEventListener('approvals-count', (e) =>
      opts.onCount!((e as CustomEvent).detail),
    );
  }
  el.open = opts.open ?? false;
  el.coworkers = opts.coworkers ?? [COWORKER_OPS, COWORKER_LOG];
  el.conversations = opts.conversations ?? [CONV_OPS];
  if (opts.activeConversationId !== undefined) {
    el.activeConversationId = opts.activeConversationId;
  }
  if (opts.jumpHandler) el.jumpHandler = opts.jumpHandler;
  document.body.appendChild(el);
  await settle(el);
  return el;
}

describe('paramsInline', () => {
  it('joins up to the first 4 entries as k: v · …', () => {
    const out = paramsInline({ a: 1, b: 'two', c: true, d: 'four', e: 'five' });
    // Only the first four keys appear.
    expect(out).toBe('a: 1 · b: two · c: true · d: four');
    expect(out).not.toContain('e:');
  });

  it('truncates a long value at 30 chars + ellipsis (never silently drops it)', () => {
    const long = 'x'.repeat(50);
    const out = paramsInline({ text: long });
    expect(out).toBe('text: ' + 'x'.repeat(30) + '…');
  });

  it('returns empty string for empty object, non-object, array, and null', () => {
    expect(paramsInline({})).toBe('');
    expect(paramsInline(null)).toBe('');
    expect(paramsInline(undefined)).toBe('');
    expect(paramsInline('nope')).toBe('');
    expect(paramsInline([1, 2, 3])).toBe('');
  });
});

describe('formatCountdown', () => {
  const now = Date.parse('2026-06-01T12:00:00Z');
  it('renders minutes when > 60s remain', () => {
    expect(formatCountdown('2026-06-01T12:18:00Z', now)).toBe('18m left');
  });
  it('renders seconds under a minute', () => {
    expect(formatCountdown('2026-06-01T12:00:42Z', now)).toBe('42s left');
  });
  it('renders "expired" at or past the deadline', () => {
    expect(formatCountdown('2026-06-01T12:00:00Z', now)).toBe('expired');
    expect(formatCountdown('2026-06-01T11:59:00Z', now)).toBe('expired');
  });
  it('drops a missing or unparseable timestamp', () => {
    expect(formatCountdown(null, now)).toBe('');
    expect(formatCountdown('not-a-date', now)).toBe('');
  });
});

describe('isUrgent (badge / row threshold)', () => {
  const now = Date.parse('2026-06-01T12:00:00Z');
  it('is false at exactly 5 minutes out (boundary is strict <)', () => {
    expect(isUrgent('2026-06-01T12:05:00Z', now)).toBe(false);
  });
  it('is true just inside 5 minutes', () => {
    expect(isUrgent('2026-06-01T12:04:59Z', now)).toBe(true);
  });
  it('is true for an already-expired item', () => {
    expect(isUrgent('2026-06-01T11:50:00Z', now)).toBe(true);
  });
  it('is false with no expiry', () => {
    expect(isUrgent(null, now)).toBe(false);
  });
});

describe('<rm-approvals-inbox> rendering', () => {
  afterEach(() => {
    document.querySelectorAll('rm-approvals-inbox').forEach((el) => el.remove());
    listPendingSpy.mockReset();
    vi.useRealTimers();
  });

  it('renders nothing in the DOM while closed', async () => {
    const el = await mount({ open: false, rows: [req({ request_id: 'r1' })] });
    expect(el.querySelector('[data-testid="approvals-panel"]')).toBeNull();
  });

  it('renders a row with coworker name, tool chip, conv title, countdown and params', async () => {
    const el = await mount({
      open: true,
      rows: [req({ request_id: 'r1', expires_at: expiresInMin(18) })],
    });
    const row = el.querySelector('[data-testid="approvals-row"]')!;
    expect(row).not.toBeNull();
    expect(row.querySelector('.h')?.textContent).toContain('Ops coworker');
    expect(
      row.querySelector('[data-testid="approvals-row-tool"]')?.textContent,
    ).toBe('amazon-ads-api.campaign.pause');
    expect(row.querySelector('.m')?.textContent).toContain('Q2 ad spend review');
    expect(
      row.querySelector('[data-testid="approvals-row-countdown"]')?.textContent,
    ).toBe('18m left');
    expect(
      row.querySelector('[data-testid="approvals-row-params"]')?.textContent,
    ).toContain('campaign_id: SP-Auto-Hydration');
    expect(
      el.querySelector('[data-testid="approvals-row-open"]')?.textContent,
    ).toContain('Open in chat');
  });

  it('omits the params-inline line when params is empty', async () => {
    const el = await mount({
      open: true,
      rows: [req({ request_id: 'r1', params: {} })],
    });
    expect(el.querySelector('[data-testid="approvals-row-params"]')).toBeNull();
  });

  it('sorts rows by expires_at ascending (most urgent first), regardless of input order', async () => {
    const el = await mount({
      open: true,
      rows: [
        req({ request_id: 'mid', expires_at: expiresInMin(10) }),
        req({ request_id: 'soon', expires_at: expiresInMin(3) }),
        req({ request_id: 'far', expires_at: expiresInMin(18) }),
      ],
    });
    const ids = Array.from(
      el.querySelectorAll('[data-testid="approvals-row"]'),
    ).map((r) => r.getAttribute('data-appr-row-id'));
    expect(ids).toEqual(['soon', 'mid', 'far']);
  });

  it('marks a row urgent (data-urgent=true) when under 5 minutes', async () => {
    const el = await mount({
      open: true,
      rows: [
        req({ request_id: 'soon', expires_at: expiresInMin(3) }),
        req({ request_id: 'calm', expires_at: expiresInMin(18) }),
      ],
    });
    const byId = (id: string) =>
      el
        .querySelector(`[data-appr-row-id="${id}"]`)!
        .querySelector('[data-testid="approvals-row-countdown"]')!
        .getAttribute('data-urgent');
    expect(byId('soon')).toBe('true');
    expect(byId('calm')).toBe('false');
  });

  it('renders the empty state with the Activity pointer when nothing is pending', async () => {
    const el = await mount({ open: true, rows: [] });
    const empty = el.querySelector('[data-testid="approvals-empty"]');
    expect(empty).not.toBeNull();
    expect(empty?.textContent).toContain('Nothing waiting for you');
    expect(empty?.textContent).toContain('Activity');
    // No rows at all in the empty state.
    expect(el.querySelector('[data-testid="approvals-row"]')).toBeNull();
  });

  it('shows the "N expiring soon" sub-heading only when something is urgent', async () => {
    const calm = await mount({
      open: true,
      rows: [req({ request_id: 'r1', expires_at: expiresInMin(18) })],
    });
    expect(
      calm.querySelector('[data-testid="approvals-urgent-note"]'),
    ).toBeNull();
    calm.remove();

    const hot = await mount({
      open: true,
      rows: [
        req({ request_id: 'r1', expires_at: expiresInMin(2) }),
        req({ request_id: 'r2', expires_at: expiresInMin(18) }),
      ],
    });
    expect(
      hot.querySelector('[data-testid="approvals-urgent-note"]')?.textContent,
    ).toContain('1 expiring soon');
  });

  it('falls back to a generic coworker label when the id is unknown', async () => {
    const el = await mount({
      open: true,
      coworkers: [], // nothing to resolve against
      rows: [req({ request_id: 'r1' })],
    });
    expect(el.querySelector('.h')?.textContent?.trim()).toMatch(/^Coworker/);
  });
});

describe('<rm-approvals-inbox> badge count event', () => {
  afterEach(() => {
    document.querySelectorAll('rm-approvals-inbox').forEach((el) => el.remove());
    listPendingSpy.mockReset();
  });

  it('emits {total, urgent} for the shell badge after a fetch', async () => {
    const counts: Array<{ total: number; urgent: number }> = [];
    await mount({
      open: false,
      rows: [
        req({ request_id: 'r1', expires_at: expiresInMin(2) }), // urgent
        req({ request_id: 'r2', expires_at: expiresInMin(18) }), // calm
      ],
      onCount: (d) => counts.push(d),
    });
    const last = counts.at(-1);
    expect(last).toEqual({ total: 2, urgent: 1 });
  });

  it('emits a zero count when nothing is pending (clears a stale badge)', async () => {
    const counts: Array<{ total: number; urgent: number }> = [];
    await mount({ open: false, rows: [], onCount: (d) => counts.push(d) });
    expect(counts.at(-1)).toEqual({ total: 0, urgent: 0 });
  });
});

describe('<rm-approvals-inbox> re-fetch triggers (§4.8)', () => {
  afterEach(() => {
    document.querySelectorAll('rm-approvals-inbox').forEach((el) => el.remove());
    listPendingSpy.mockReset();
    vi.useRealTimers();
  });

  it('① opening the popover re-fetches', async () => {
    const el = await mount({ open: false, rows: [] });
    listPendingSpy.mockClear();
    el.open = true;
    await settle(el);
    expect(listPendingSpy).toHaveBeenCalled();
  });

  it('② switching the active conversation re-fetches', async () => {
    const el = await mount({ open: false, rows: [], activeConversationId: 'c1' });
    listPendingSpy.mockClear();
    el.activeConversationId = 'c2';
    await settle(el);
    expect(listPendingSpy).toHaveBeenCalled();
  });

  it('② does NOT re-fetch redundantly on the initial conversation assignment', async () => {
    // The connectedCallback seed is the only fetch; the first
    // activeConversationId set (undefined→value) must not double up.
    listPendingSpy.mockResolvedValue([]);
    const el = document.createElement('rm-approvals-inbox') as ApprovalsInbox;
    el.activeConversationId = 'c1';
    document.body.appendChild(el);
    await settle(el);
    expect(listPendingSpy).toHaveBeenCalledTimes(1);
  });

  it('③ tab becoming visible re-fetches', async () => {
    const el = await mount({ open: false, rows: [] });
    listPendingSpy.mockClear();
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => 'visible',
    });
    document.dispatchEvent(new Event('visibilitychange'));
    await settle(el);
    expect(listPendingSpy).toHaveBeenCalled();
  });

  it('④ refresh() (the approval-activity surrogate) re-fetches', async () => {
    const el = await mount({ open: false, rows: [] });
    listPendingSpy.mockClear();
    await el.refresh();
    expect(listPendingSpy).toHaveBeenCalled();
  });

  it('does not stand up a high-frequency timer while CLOSED', async () => {
    // Backstop: a closed inbox must not poll. We seed once on mount, then
    // assert no further fetch lands across a generous window.
    vi.useFakeTimers({ toFake: ['setInterval'] });
    listPendingSpy.mockResolvedValue([]);
    const el = document.createElement('rm-approvals-inbox') as ApprovalsInbox;
    document.body.appendChild(el);
    // Flush the connectedCallback seed.
    await Promise.resolve();
    const seedCalls = listPendingSpy.mock.calls.length;
    vi.advanceTimersByTime(120_000); // 2 minutes of wall-clock
    expect(listPendingSpy.mock.calls.length).toBe(seedCalls);
    el.remove();
  });
});

describe('<rm-approvals-inbox> jumpToConv (§4.7)', () => {
  afterEach(() => {
    document.querySelectorAll('rm-approvals-inbox').forEach((el) => el.remove());
    document.querySelectorAll('[data-appr-id]').forEach((el) => el.remove());
    listPendingSpy.mockReset();
  });

  it('asks the shell to switch coworker+conversation, then scrolls + highlights the card', async () => {
    const jumpSpy = vi.fn(async (_convId: string | null, _cwId: string | null) => {
      // The fake shell "switches" by materialising the target card in the
      // document, exactly as the slotted chat-panel would after navigating.
      const card = document.createElement('div');
      card.setAttribute('data-appr-id', 'r1');
      (card as unknown as { scrollIntoView: () => void }).scrollIntoView =
        vi.fn();
      document.body.appendChild(card);
    });
    const el = await mount({
      open: true,
      jumpHandler: jumpSpy,
      rows: [
        req({
          request_id: 'r1',
          conversation_id: 'conv-x',
          coworker_id: 'cw-log',
        }),
      ],
    });
    const closes = vi.fn();
    el.addEventListener('inbox-close', closes);

    el.querySelector<HTMLElement>('[data-testid="approvals-row"]')!.click();
    await settle(el);

    expect(jumpSpy).toHaveBeenCalledWith('conv-x', 'cw-log');
    expect(closes).toHaveBeenCalled();
    const card = document.querySelector('[data-appr-id="r1"]')!;
    expect(
      (card as unknown as { scrollIntoView: ReturnType<typeof vi.fn> })
        .scrollIntoView,
    ).toHaveBeenCalled();
    expect(card.classList.contains('rm-appr-highlight')).toBe(true);
  });

  it('the redundant "Open in chat" button jumps too (without double-bubbling the row click)', async () => {
    const jumpSpy = vi.fn(async () => {});
    const el = await mount({
      open: true,
      jumpHandler: jumpSpy,
      rows: [req({ request_id: 'r1', conversation_id: 'conv-x' })],
    });
    el.querySelector<HTMLElement>('[data-testid="approvals-row-open"]')!.click();
    await settle(el);
    // Exactly one jump despite the button living inside the clickable row.
    expect(jumpSpy).toHaveBeenCalledTimes(1);
  });
});

describe('<rm-approvals-inbox> is triage-only (never decides)', () => {
  afterEach(() => {
    document.querySelectorAll('rm-approvals-inbox').forEach((el) => el.remove());
    listPendingSpy.mockReset();
  });

  it('renders no approve/reject controls', async () => {
    const el = await mount({ open: true, rows: [req({ request_id: 'r1' })] });
    expect(el.querySelector('[data-testid="approval-approve"]')).toBeNull();
    expect(el.querySelector('[data-testid="approval-reject"]')).toBeNull();
    // No <textarea> reject-note form leaks into the triage surface.
    expect(el.querySelector('textarea')).toBeNull();
  });

  it('never emits an approval-decision frame when a row is actioned', async () => {
    const el = await mount({
      open: true,
      jumpHandler: vi.fn(async () => {}),
      rows: [req({ request_id: 'r1' })],
    });
    const decision = vi.fn();
    el.addEventListener('approval-decision', decision);
    // Both affordances: the row and the inner button.
    el.querySelector<HTMLElement>('[data-testid="approvals-row"]')!.click();
    el.querySelector<HTMLElement>('[data-testid="approvals-row-open"]')!.click();
    await settle(el);
    expect(decision).not.toHaveBeenCalled();
  });
});
