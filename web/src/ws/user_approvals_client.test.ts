// Pinned tests for `UserApprovalsClient`. The component owns:
//   1. handshake order (POST /api/v1/auth/ws-ticket with the
//      `user-approvals` scope BEFORE opening a WS)
//   2. event routing — `.required` / `.resolved` reach their handler
//      buckets; other event types are silently dropped (we do NOT want
//      `event.run.token` etc. flowing into the popover)
//   3. failure modes — a ticket POST 4xx surfaces `status='closed'`
//      and schedules a reconnect (we do not want a missing backend
//      endpoint to silently leave the popover at status='connecting'
//      forever)
//   4. stop() short-circuits reconnect and closes the socket
//
// These tests use a hand-rolled WebSocket mock identical in shape to
// v1_client.test.ts; sharing it via a helper module would couple two
// otherwise-independent client suites for one constructor's worth of
// noise.

import { describe, expect, it, vi } from 'vitest';
import { UserApprovalsClient } from './user_approvals_client.js';

interface MockWebSocketLike {
  url: string;
  readyState: number;
  onopen: ((ev: Event) => void) | null;
  onmessage: ((ev: MessageEvent) => void) | null;
  onclose: ((ev: CloseEvent) => void) | null;
  onerror: ((ev: Event) => void) | null;
  sent: string[];
  close: () => void;
  send: (data: string) => void;
}

interface MockWebSocketCtor {
  new (url: string): MockWebSocketLike;
  readonly CONNECTING: 0;
  readonly OPEN: 1;
  readonly CLOSING: 2;
  readonly CLOSED: 3;
  readonly instances: MockWebSocketLike[];
}

function makeMockWebSocket(): MockWebSocketCtor {
  const instances: MockWebSocketLike[] = [];
  const ctor = function (this: MockWebSocketLike, url: string) {
    this.url = url;
    this.readyState = 0;
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;
    this.sent = [];
    this.close = () => {
      this.readyState = 3;
      this.onclose?.({ code: 1000, reason: '', wasClean: true } as CloseEvent);
    };
    this.send = (data: string) => {
      this.sent.push(data);
    };
    instances.push(this);
  } as unknown as MockWebSocketCtor;
  Object.assign(ctor, {
    CONNECTING: 0,
    OPEN: 1,
    CLOSING: 2,
    CLOSED: 3,
    instances,
  });
  return ctor;
}

function openMock(ws: MockWebSocketCtor): MockWebSocketLike {
  const inst = ws.instances[ws.instances.length - 1];
  inst.readyState = 1;
  inst.onopen?.({} as Event);
  return inst;
}

function deliver(inst: MockWebSocketLike, event: unknown): void {
  inst.onmessage?.({ data: JSON.stringify(event) } as MessageEvent);
}

interface FetchCall {
  url: string;
  init: RequestInit | undefined;
}

function makeFetch(
  routes: Record<string, (init?: RequestInit) => Response | Promise<Response>>,
): { fn: typeof fetch; calls: FetchCall[] } {
  const calls: FetchCall[] = [];
  const fn = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : String(input);
    calls.push({ url, init });
    const route = Object.keys(routes).find((p) => url.startsWith(p));
    if (!route) return new Response('not found', { status: 404 });
    return routes[route](init);
  }) as unknown as typeof fetch;
  return { fn, calls };
}

const flush = () => new Promise<void>((r) => setTimeout(r, 0));

describe('UserApprovalsClient — handshake', () => {
  it('mints a ws-ticket with scope=user-approvals before opening the socket', async () => {
    const { fn: fetchFn, calls } = makeFetch({
      '/api/v1/auth/ws-ticket': () =>
        new Response(
          JSON.stringify({ ticket: 'tkt-u', expires_in_s: 60 }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
    });
    const WS = makeMockWebSocket();
    const client = new UserApprovalsClient(
      { getToken: () => 'bearer' },
      {
        fetch: fetchFn,
        WebSocket: WS as unknown as typeof WebSocket,
        wsOrigin: 'ws://test',
      },
    );
    await client.start();
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe('/api/v1/auth/ws-ticket');
    expect(calls[0].init?.method).toBe('POST');
    const body = JSON.parse(String(calls[0].init?.body));
    expect(body.scope).toBe('user-approvals');
    expect(WS.instances).toHaveLength(1);
    expect(WS.instances[0].url).toBe(
      'ws://test/api/v1/users/me/approvals/stream?ticket=tkt-u',
    );
  });
});

describe('UserApprovalsClient — event routing', () => {
  it('fans approval.required / approval.resolved to their handler buckets', async () => {
    const { fn: fetchFn } = makeFetch({
      '/api/v1/auth/ws-ticket': () =>
        new Response(JSON.stringify({ ticket: 't', expires_in_s: 60 }), {
          status: 200,
        }),
    });
    const WS = makeMockWebSocket();
    const client = new UserApprovalsClient(
      { getToken: () => null },
      { fetch: fetchFn, WebSocket: WS as unknown as typeof WebSocket },
    );
    const required: unknown[] = [];
    const resolved: unknown[] = [];
    client.onRequired((e) => required.push(e));
    client.onResolved((e) => resolved.push(e));
    await client.start();
    const inst = openMock(WS);
    deliver(inst, {
      type: 'event.approval.required',
      approval_id: 'apr-1',
      run_id: 'run-1',
      summary: { tool_name: 'refund' },
    });
    deliver(inst, {
      type: 'event.approval.resolved',
      approval_id: 'apr-1',
      decision: 'approve',
    });
    expect(required).toHaveLength(1);
    expect(resolved).toHaveLength(1);
  });

  it('drops unknown event types instead of leaking them into approval buckets', async () => {
    const { fn: fetchFn } = makeFetch({
      '/api/v1/auth/ws-ticket': () =>
        new Response(JSON.stringify({ ticket: 't', expires_in_s: 60 }), {
          status: 200,
        }),
    });
    const WS = makeMockWebSocket();
    const client = new UserApprovalsClient(
      { getToken: () => null },
      { fetch: fetchFn, WebSocket: WS as unknown as typeof WebSocket },
    );
    const required: unknown[] = [];
    const resolved: unknown[] = [];
    client.onRequired((e) => required.push(e));
    client.onResolved((e) => resolved.push(e));
    await client.start();
    const inst = openMock(WS);
    deliver(inst, { type: 'event.run.token', delta: 'hi' });
    deliver(inst, { not: 'an event' });
    expect(required).toHaveLength(0);
    expect(resolved).toHaveLength(0);
  });

  it('silently ignores malformed JSON frames', async () => {
    const { fn: fetchFn } = makeFetch({
      '/api/v1/auth/ws-ticket': () =>
        new Response(JSON.stringify({ ticket: 't', expires_in_s: 60 }), {
          status: 200,
        }),
    });
    const WS = makeMockWebSocket();
    const client = new UserApprovalsClient(
      { getToken: () => null },
      { fetch: fetchFn, WebSocket: WS as unknown as typeof WebSocket },
    );
    const required: unknown[] = [];
    client.onRequired((e) => required.push(e));
    await client.start();
    const inst = openMock(WS);
    inst.onmessage?.({ data: 'not-json' } as MessageEvent);
    expect(required).toHaveLength(0);
  });
});

describe('UserApprovalsClient — handshake failure', () => {
  it('surfaces status=closed when the ticket endpoint 404s (backend not deployed yet)', async () => {
    const { fn: fetchFn } = makeFetch({
      '/api/v1/auth/ws-ticket': () =>
        new Response(JSON.stringify({ code: 'NOT_FOUND' }), { status: 404 }),
    });
    const WS = makeMockWebSocket();
    const statuses: string[] = [];
    const client = new UserApprovalsClient(
      { getToken: () => null },
      {
        fetch: fetchFn,
        WebSocket: WS as unknown as typeof WebSocket,
        reconnectDelayMs: 60_000,
      },
    );
    client.onStatus((s) => statuses.push(s));
    await client.start();
    await flush();
    expect(WS.instances).toHaveLength(0);
    // connecting → closed; reconnect is scheduled (delay 60s, won't fire)
    expect(statuses).toContain('connecting');
    expect(statuses).toContain('closed');
    expect(client.connectionStatus).toBe('closed');
    client.stop();
  });

  it('reschedules reconnect on socket close until stop() is called', async () => {
    let ticketRequests = 0;
    const { fn: fetchFn } = makeFetch({
      '/api/v1/auth/ws-ticket': () => {
        ticketRequests += 1;
        return new Response(
          JSON.stringify({ ticket: `t-${ticketRequests}`, expires_in_s: 60 }),
          { status: 200 },
        );
      },
    });
    const WS = makeMockWebSocket();
    const client = new UserApprovalsClient(
      { getToken: () => null },
      {
        fetch: fetchFn,
        WebSocket: WS as unknown as typeof WebSocket,
        reconnectDelayMs: 0,
      },
    );
    await client.start();
    const inst = openMock(WS);
    // Drop the socket — the client should reconnect via the timer.
    inst.readyState = 3;
    inst.onclose?.({ code: 1006, reason: '', wasClean: false } as CloseEvent);
    await flush();
    await flush();
    expect(ticketRequests).toBeGreaterThanOrEqual(2);
    expect(WS.instances.length).toBeGreaterThanOrEqual(2);
    client.stop();
  });
});

describe('UserApprovalsClient — stop', () => {
  it('cancels the pending reconnect and closes the socket', async () => {
    const { fn: fetchFn } = makeFetch({
      '/api/v1/auth/ws-ticket': () =>
        new Response(JSON.stringify({ ticket: 't', expires_in_s: 60 }), {
          status: 200,
        }),
    });
    const WS = makeMockWebSocket();
    const client = new UserApprovalsClient(
      { getToken: () => null },
      {
        fetch: fetchFn,
        WebSocket: WS as unknown as typeof WebSocket,
        reconnectDelayMs: 60_000,
      },
    );
    await client.start();
    openMock(WS);
    expect(client.connectionStatus).toBe('open');
    const onClose = vi.fn();
    client.onStatus(onClose);
    client.stop();
    expect(client.connectionStatus).toBe('closed');
    expect(onClose).toHaveBeenCalled();
  });
});
