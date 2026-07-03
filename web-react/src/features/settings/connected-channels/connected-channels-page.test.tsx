// @vitest-environment happy-dom
//
// Test baseline = the Lit page's eight cases (connected-channels-
// page.test.ts), re-expressed for React: empty state, identity rows,
// no-bot 409 panel, deep-link present/omitted, baseline-set poll
// detection, countdown expiry, disconnect (here through the D-M3
// confirm dialog).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import type { ChannelLinkIdentity } from '../../../api/client';
import { ConnectedChannelsPage } from './connected-channels-page';

const ID_A: ChannelLinkIdentity = {
  id: 'cli-a',
  platform: 'telegram',
  channel_id: '784412903',
  created_at: '2026-06-05T10:00:00Z',
};

let identities: ChannelLinkIdentity[] = [];
let postBody: () => { status: number; body: unknown };
let deleted: string[] = [];

function jsonResp(status: number, body: unknown) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

beforeEach(() => {
  identities = [];
  deleted = [];
  postBody = () => ({
    status: 201,
    body: {
      token: 'rk_test_token_0123456789ab',
      expires_at: new Date(Date.now() + 600_000).toISOString(),
      deep_link: 'https://t.me/demo_bot?start=rk_test',
    },
  });
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      const u = String(url);
      const method = init?.method ?? 'GET';
      if (u.endsWith('/me/channel-links/telegram')) {
        if (method === 'GET') return jsonResp(200, identities);
        if (method === 'POST') {
          const r = postBody();
          return jsonResp(r.status, r.body);
        }
      }
      if (method === 'DELETE') {
        const id = u.split('/').pop()!;
        deleted.push(id);
        identities = identities.filter((i) => i.id !== id);
        return new Response(null, { status: 204 });
      }
      throw new Error(`unexpected fetch ${method} ${u}`);
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ConnectedChannelsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('ConnectedChannelsPage', () => {
  it('renders the reassuring empty state when no accounts are linked', async () => {
    renderPage();
    expect((await screen.findByTestId('cc-empty')).textContent).toContain(
      'always remain available in the web app',
    );
  });

  it('renders one row per identity with a Disconnect button', async () => {
    identities = [ID_A];
    renderPage();
    const row = await screen.findByTestId('cc-row-cli-a');
    expect(row.textContent).toContain('telegram');
    expect(row.textContent).toContain('id 784412903');
    expect(screen.getByTestId('cc-disconnect')).toBeTruthy();
  });

  it('POST 409 RESOURCE_NOT_AVAILABLE renders the configuration panel and hides Connect', async () => {
    postBody = () => ({
      status: 409,
      body: { code: 'RESOURCE_NOT_AVAILABLE', message: 'no bot' },
    });
    renderPage();
    fireEvent.click(await screen.findByTestId('cc-connect'));
    expect((await screen.findByTestId('cc-no-bot')).textContent).toContain(
      'Configure a Telegram bot first',
    );
    expect(screen.queryByTestId('cc-connect')).toBeNull();
    expect(screen.queryByTestId('cc-pending')).toBeNull();
  });

  it('waiting panel shows deep-link anchor (when present) plus the raw token', async () => {
    renderPage();
    fireEvent.click(await screen.findByTestId('cc-connect'));
    await screen.findByTestId('cc-pending');
    expect((screen.getByTestId('cc-deep-link') as HTMLAnchorElement).href).toContain(
      't.me/demo_bot',
    );
    expect(screen.getByTestId('cc-token').textContent).toBe('rk_test_token_0123456789ab');
    expect(screen.queryByTestId('cc-connect')).toBeNull();
  });

  it('omits the deep-link anchor when POST returns null', async () => {
    postBody = () => ({
      status: 201,
      body: {
        token: 'rk_no_deeplink_0123456789',
        expires_at: new Date(Date.now() + 600_000).toISOString(),
        deep_link: null,
      },
    });
    renderPage();
    fireEvent.click(await screen.findByTestId('cc-connect'));
    await screen.findByTestId('cc-pending');
    expect(screen.queryByTestId('cc-deep-link')).toBeNull();
    expect(screen.getByTestId('cc-token').textContent).toBe('rk_no_deeplink_0123456789');
  });

  it('poll flips to success only when an id OUTSIDE the baseline appears', async () => {
    identities = [ID_A]; // pre-existing link — inside the baseline
    renderPage();
    // Initial load under real timers (Connect renders only after it —
    // the baseline must be captured from loaded data); THEN freeze.
    await screen.findByTestId('cc-row-cli-a');
    vi.useFakeTimers({
      toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'],
    });
    fireEvent.click(screen.getByTestId('cc-connect'));
    await act(async () => {});
    expect(screen.getByTestId('cc-pending')).toBeTruthy();

    // Poll round returns only baseline ids → still waiting. (The extra
    // 1 ms advance runs TanStack's setTimeout(0) notify batch, which
    // fake timers would otherwise hold forever.)
    await act(async () => {
      vi.advanceTimersByTime(3000);
    });
    await act(async () => {
      vi.advanceTimersByTime(1);
    });
    expect(screen.getByTestId('cc-pending')).toBeTruthy();
    expect(screen.queryByTestId('cc-ok')).toBeNull();

    // A fresh id appears → confirmation panel, pending cleared.
    identities = [
      ID_A,
      { id: 'cli-new', platform: 'telegram', channel_id: '551200784', created_at: null },
    ];
    await act(async () => {
      vi.advanceTimersByTime(3000);
    });
    await act(async () => {
      vi.advanceTimersByTime(1);
    });
    expect(screen.queryByTestId('cc-pending')).toBeNull();
    expect(screen.getByTestId('cc-ok').textContent).toContain('Telegram connected');
    expect(screen.getByTestId('cc-row-cli-new')).toBeTruthy();
  });

  it('countdown ticks mm:ss and expiry clears the attempt with a toast', async () => {
    postBody = () => ({
      status: 201,
      body: {
        token: 'rk_short_lived_0123456789',
        expires_at: new Date(Date.now() + 65_000).toISOString(),
        deep_link: null,
      },
    });
    renderPage();
    await screen.findByTestId('cc-connect');
    vi.useFakeTimers({
      toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'],
    });
    fireEvent.click(screen.getByTestId('cc-connect'));
    await act(async () => {});
    expect(screen.getByTestId('cc-countdown').textContent).toBe('1:05');
    await act(async () => {
      vi.advanceTimersByTime(1000);
    });
    expect(screen.getByTestId('cc-countdown').textContent).toBe('1:04');
    await act(async () => {
      vi.advanceTimersByTime(64_000);
    });
    expect(screen.queryByTestId('cc-pending')).toBeNull();
    expect(screen.getByText('Link code expired')).toBeTruthy();
  });

  it('disconnect goes through the D-M3 confirm with the what-stays copy, then DELETEs', async () => {
    identities = [ID_A];
    renderPage();
    fireEvent.click(await screen.findByTestId('cc-disconnect'));
    expect(screen.getByText('Disconnect this Telegram account?')).toBeTruthy();
    expect(screen.getByText(/pending approvals remain in the web app/)).toBeTruthy();
    expect(deleted).toEqual([]);
    fireEvent.click(
      within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Disconnect' }),
    );
    await screen.findByText('Telegram account disconnected');
    expect(deleted).toEqual(['cli-a']);
    expect(screen.queryByTestId('cc-row-cli-a')).toBeNull();
    expect(await screen.findByTestId('cc-empty')).toBeTruthy();
  });
});
