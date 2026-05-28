// @vitest-environment happy-dom
// v6.1 §P1.4 — Connected channels page behaviour.
//
// The DB and HTTP layers are covered by the backend test suite; this
// file targets the bits that only the component owns:
//
//   - The poll loop only fires the success transition when a NEW
//     identity-id appears (not just any id), so an existing link from
//     a previous session never flashes a spurious "connected" state.
//   - 409 RESOURCE_NOT_AVAILABLE from POST renders the static "no
//     bot configured" hint, not a transient error.
//   - The deep-link, when returned, is rendered as an actual `<a>`
//     with the embedded URL — the design's preferred entry point
//     must work in one click.
//   - Disconnect calls DELETE and then refreshes the list, leaving
//     no stale row in the DOM.

import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest';

const listMock = vi.fn();
const issueMock = vi.fn();
const unlinkMock = vi.fn();

vi.mock('../api/client.js', async () => {
  const actual = await vi.importActual<typeof import('../api/client.js')>(
    '../api/client.js',
  );
  return {
    ...actual,
    getApiClient: () => ({
      listTelegramLinks: listMock,
      issueTelegramLinkToken: issueMock,
      unlinkChannelIdentity: unlinkMock,
    }),
  };
});

import { ApiError } from '../api/client.js';
import './connected-channels-page.js';
import type { ConnectedChannelsPage } from './connected-channels-page.js';

async function settle(el: ConnectedChannelsPage): Promise<void> {
  for (let i = 0; i < 30; i += 1) {
    await Promise.resolve();
    await el.updateComplete;
  }
}

async function mount(): Promise<ConnectedChannelsPage> {
  const el = document.createElement(
    'rm-connected-channels-page',
  ) as ConnectedChannelsPage;
  document.body.appendChild(el);
  await settle(el);
  return el;
}

function expiresIn(seconds: number): string {
  return new Date(Date.now() + seconds * 1000).toISOString();
}

describe('<rm-connected-channels-page>', () => {
  beforeEach(() => {
    listMock.mockReset();
    issueMock.mockReset();
    unlinkMock.mockReset();
  });

  afterEach(() => {
    document
      .querySelectorAll('rm-connected-channels-page')
      .forEach((el) => el.remove());
    if (vi.isFakeTimers()) {
      vi.useRealTimers();
    }
  });

  it('renders an empty state when the user has no linked accounts', async () => {
    listMock.mockResolvedValue([]);
    const el = await mount();
    expect(el.querySelector('[data-testid="cc-empty"]')).not.toBeNull();
    expect(
      el.querySelector('[data-testid="cc-connect-telegram"]'),
    ).not.toBeNull();
  });

  it('renders one row per existing identity with a Disconnect button', async () => {
    listMock.mockResolvedValue([
      { id: 'id-1', platform: 'telegram', channel_id: '1001', created_at: null },
      { id: 'id-2', platform: 'telegram', channel_id: '2002', created_at: null },
    ]);
    const el = await mount();
    const rows = el.querySelectorAll('[data-testid="cc-identity-row"]');
    expect(rows.length).toBe(2);
    const ids = Array.from(
      el.querySelectorAll('[data-testid="cc-channel-id"]'),
    ).map((n) => n.textContent);
    expect(ids).toEqual(['1001', '2002']);
    expect(el.querySelectorAll('[data-testid="cc-disconnect"]').length).toBe(2);
  });

  it('renders the no-bot hint when POST returns 409 RESOURCE_NOT_AVAILABLE', async () => {
    listMock.mockResolvedValue([]);
    issueMock.mockRejectedValue(
      new ApiError(
        409,
        { code: 'RESOURCE_NOT_AVAILABLE', message: 'no bot' },
        'no bot',
      ),
    );
    const el = await mount();
    const connect = el.querySelector(
      '[data-testid="cc-connect-telegram"]',
    ) as HTMLButtonElement;
    connect.click();
    await settle(el);
    expect(el.querySelector('[data-testid="cc-no-bot"]')).not.toBeNull();
    // Distinguish from generic error path.
    expect(
      el.querySelector('[data-testid="cc-pending-error"]'),
    ).toBeNull();
  });

  it('renders a deep-link anchor when POST returns one, plus the raw token', async () => {
    listMock.mockResolvedValue([]);
    issueMock.mockResolvedValue({
      token: 'abc-token-with-enough-chars-22',
      expires_at: expiresIn(600),
      deep_link:
        'https://t.me/rolemesh_bot?start=abc-token-with-enough-chars-22',
    });
    const el = await mount();
    (el.querySelector(
      '[data-testid="cc-connect-telegram"]',
    ) as HTMLButtonElement).click();
    await settle(el);
    const link = el.querySelector(
      '[data-testid="cc-deep-link"]',
    ) as HTMLAnchorElement | null;
    expect(link).not.toBeNull();
    expect(link?.href).toContain(
      'https://t.me/rolemesh_bot?start=abc-token-with-enough-chars-22',
    );
    const code = el.querySelector('[data-testid="cc-token"]');
    expect(code?.textContent).toBe('abc-token-with-enough-chars-22');
  });

  it('omits the deep-link anchor when POST returns null', async () => {
    listMock.mockResolvedValue([]);
    issueMock.mockResolvedValue({
      token: 'no-deep-link-fallback-tok-22',
      expires_at: expiresIn(600),
      deep_link: null,
    });
    const el = await mount();
    (el.querySelector(
      '[data-testid="cc-connect-telegram"]',
    ) as HTMLButtonElement).click();
    await settle(el);
    expect(el.querySelector('[data-testid="cc-deep-link"]')).toBeNull();
    // The token MUST still be present so the user can copy-paste.
    const code = el.querySelector('[data-testid="cc-token"]');
    expect(code?.textContent).toBe('no-deep-link-fallback-tok-22');
  });

  it('poll loop only flips to success when a NEW identity-id appears', async () => {
    // Existing identity present before the user clicks Connect.
    listMock.mockResolvedValueOnce([
      { id: 'existing', platform: 'telegram', channel_id: '777', created_at: null },
    ]);
    issueMock.mockResolvedValue({
      token: 'tok-1234567890123456789012',
      expires_at: expiresIn(600),
      deep_link: null,
    });
    const el = await mount();
    vi.useFakeTimers();
    (el.querySelector(
      '[data-testid="cc-connect-telegram"]',
    ) as HTMLButtonElement).click();
    await settle(el);
    // First poll round — the SAME identity is returned. Pending must
    // stay open; a future-mutation that drops the new-id check (e.g.
    // "any non-empty list → success") would fail this assertion.
    listMock.mockResolvedValueOnce([
      { id: 'existing', platform: 'telegram', channel_id: '777', created_at: null },
    ]);
    await vi.advanceTimersByTimeAsync(3000);
    await settle(el);
    expect(el.querySelector('[data-testid="cc-pending"]')).not.toBeNull();
    // Second poll round — a NEW id arrives, success transition fires.
    listMock.mockResolvedValueOnce([
      { id: 'existing', platform: 'telegram', channel_id: '777', created_at: null },
      { id: 'newly-bound', platform: 'telegram', channel_id: '888', created_at: null },
    ]);
    await vi.advanceTimersByTimeAsync(3000);
    await settle(el);
    expect(el.querySelector('[data-testid="cc-pending"]')).toBeNull();
    // And the row count reflects the post-link state.
    expect(
      el.querySelectorAll('[data-testid="cc-identity-row"]').length,
    ).toBe(2);
  });

  it('countdown decrements as the tick fires and ends in a friendly expiry message', async () => {
    listMock.mockResolvedValue([]);
    issueMock.mockResolvedValue({
      token: 'tok-1234567890123456789012',
      expires_at: expiresIn(2),
      deep_link: null,
    });
    const el = await mount();
    vi.useFakeTimers();
    (el.querySelector(
      '[data-testid="cc-connect-telegram"]',
    ) as HTMLButtonElement).click();
    await settle(el);
    expect(
      el.querySelector('[data-testid="cc-countdown"]')?.textContent,
    ).toMatch(/\d+s/);
    // Drive past the expires_at boundary; pending should clear and
    // a hint message appear under the Connect button. Tick interval
    // is 1 s, so 3 s of fake time covers the 2-second TTL.
    await vi.advanceTimersByTimeAsync(3000);
    await settle(el);
    expect(el.querySelector('[data-testid="cc-pending"]')).toBeNull();
    expect(
      el.querySelector('[data-testid="cc-pending-error"]')?.textContent,
    ).toMatch(/expired/i);
  });

  it('clicking Disconnect calls DELETE and refreshes the list', async () => {
    listMock.mockResolvedValueOnce([
      { id: 'id-1', platform: 'telegram', channel_id: '1001', created_at: null },
    ]);
    listMock.mockResolvedValueOnce([]); // post-DELETE refresh
    unlinkMock.mockResolvedValue(undefined);
    const el = await mount();
    (el.querySelector('[data-testid="cc-disconnect"]') as HTMLButtonElement).click();
    await settle(el);
    expect(unlinkMock).toHaveBeenCalledWith('id-1');
    expect(el.querySelector('[data-testid="cc-empty"]')).not.toBeNull();
  });
});
