// Pinned tests for `V1WsClient` reconnect-with-GET-truth, idempotency
// and ticket flow. The whole point of the v1 client is the protocol
// contract; these tests assert on observable behaviour (which URLs
// were called with which payloads, which events fired in which order)
// rather than mirror the implementation.

import { describe, expect, it, vi } from 'vitest';
import { V1WsClient, type ServerEvent } from './v1_client.js';

// ---------------------------------------------------------------------------
// Mock WebSocket implementation
// ---------------------------------------------------------------------------

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

// Open a mock socket: nudges readyState→1 and fires onopen, mirroring
// the browser. Returns the latest mock instance.
function openMock(ws: MockWebSocketCtor): MockWebSocketLike {
  const inst = ws.instances[ws.instances.length - 1];
  inst.readyState = 1;
  inst.onopen?.({} as Event);
  return inst;
}

function deliver(inst: MockWebSocketLike, event: ServerEvent): void {
  inst.onmessage?.({ data: JSON.stringify(event) } as MessageEvent);
}

// Helper: wait for the microtask queue to drain so awaited fetches resolve.
const flush = () => new Promise<void>((r) => setTimeout(r, 0));

// ---------------------------------------------------------------------------
// Fetch stub
// ---------------------------------------------------------------------------

interface FetchCall {
  url: string;
  init: RequestInit | undefined;
}

function makeFetch(
  routes: Record<string, (init?: RequestInit) => Response | Promise<Response>>,
): {
  fn: typeof fetch;
  calls: FetchCall[];
} {
  const calls: FetchCall[] = [];
  const fn = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : String(input);
    calls.push({ url, init });
    const route = Object.keys(routes).find((p) => url.startsWith(p));
    if (!route) {
      return new Response('not found', { status: 404 });
    }
    return routes[route](init);
  }) as unknown as typeof fetch;
  return { fn, calls };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('V1WsClient — handshake', () => {
  it('mints a ws-ticket scoped to the conversation before opening the socket', async () => {
    const { fn: fetchFn, calls } = makeFetch({
      '/api/v1/auth/ws-ticket': () =>
        new Response(
          JSON.stringify({ ticket: 'tkt-123', expires_in_s: 60 }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
    });
    const WS = makeMockWebSocket();
    const client = new V1WsClient(
      { conversationId: 'conv-A', getToken: () => 'sess-token' },
      {
        fetch: fetchFn,
        WebSocket: WS as unknown as typeof WebSocket,
        uuid: () => 'fixed-uuid',
        wsOrigin: 'ws://test',
      },
    );

    await client.connect();
    expect(WS.instances).toHaveLength(1);
    // ticket lands in the WS URL
    expect(WS.instances[0].url).toContain('ticket=tkt-123');
    expect(WS.instances[0].url).toContain('/api/v1/conversations/conv-A/stream');
    // ticket request carried the conversation_id + Bearer token
    const tktCall = calls.find((c) => c.url.endsWith('/auth/ws-ticket'))!;
    expect(JSON.parse(tktCall.init!.body as string)).toEqual({
      conversation_id: 'conv-A',
    });
    expect(
      (tktCall.init!.headers as Record<string, string>)['Authorization'],
    ).toBe('Bearer sess-token');
  });
});

describe('V1WsClient — request.run idempotency', () => {
  it('reuses the same idempotency_key when send() is called with the same input twice', async () => {
    const { fn: fetchFn } = makeFetch({
      '/api/v1/auth/ws-ticket': () =>
        new Response(JSON.stringify({ ticket: 't', expires_in_s: 60 }), {
          status: 200,
        }),
    });
    const WS = makeMockWebSocket();
    let nextUuid = 0;
    const client = new V1WsClient(
      { conversationId: 'conv-A', getToken: () => null },
      {
        fetch: fetchFn,
        WebSocket: WS as unknown as typeof WebSocket,
        uuid: () => `uuid-${++nextUuid}`,
      },
    );
    await client.connect();
    const inst = openMock(WS);

    const k1 = client.send('hello');
    const k2 = client.send('hello'); // duplicate text → reuse key
    const k3 = client.send('world'); // new text → new key
    expect(k1).toBe('uuid-1');
    expect(k2).toBe('uuid-1');
    expect(k3).toBe('uuid-2');
    expect(inst.sent).toHaveLength(3);
    const sent = inst.sent.map((s) => JSON.parse(s));
    expect(sent[0]).toMatchObject({
      type: 'request.run',
      input: 'hello',
      idempotency_key: 'uuid-1',
    });
    expect(sent[1].idempotency_key).toBe('uuid-1');
    expect(sent[2].idempotency_key).toBe('uuid-2');
  });

  it('dispatches event.run.error when send() is called while the socket is not open', async () => {
    const { fn: fetchFn } = makeFetch({});
    const WS = makeMockWebSocket();
    const client = new V1WsClient(
      { conversationId: 'conv-A', getToken: () => null },
      {
        fetch: fetchFn,
        WebSocket: WS as unknown as typeof WebSocket,
        uuid: () => 'k',
      },
    );
    const errors: ServerEvent[] = [];
    client.onEvent('event.run.error', (e) => errors.push(e));
    // Never connected — socket is null.
    client.send('hi');
    expect(errors).toHaveLength(1);
    expect((errors[0] as { code: string }).code).toBe('WS_NOT_OPEN');
  });
});

describe('V1WsClient — reconnect first GETs run truth', () => {
  it('skips opening a new socket and synthesises event.run.completed when the run is terminal', async () => {
    let ticketCount = 0;
    const { fn: fetchFn } = makeFetch({
      '/api/v1/auth/ws-ticket': () => {
        ticketCount += 1;
        return new Response(JSON.stringify({ ticket: 't', expires_in_s: 60 }), {
          status: 200,
        });
      },
      '/api/v1/runs/run-1': () =>
        new Response(
          JSON.stringify({
            id: 'run-1',
            conversation_id: 'conv-A',
            status: 'completed',
          }),
          { status: 200 },
        ),
    });
    const WS = makeMockWebSocket();
    const client = new V1WsClient(
      { conversationId: 'conv-A', getToken: () => null },
      {
        fetch: fetchFn,
        WebSocket: WS as unknown as typeof WebSocket,
        uuid: () => 'k',
        reconnectDelayMs: 0,
      },
    );

    await client.connect();
    expect(ticketCount).toBe(1);
    const inst = openMock(WS);
    // Server confirms a run started so the client caches activeRunId.
    deliver(inst, {
      type: 'event.run.started',
      run_id: 'run-1',
      idempotent: false,
    });
    expect(client.currentRunId).toBe('run-1');

    const events: ServerEvent[] = [];
    client.onEvent('event.run.completed', (e) => events.push(e));
    client.onEvent('event.run.error', (e) => events.push(e));

    // Simulate a transport drop. The client should schedule a
    // reconnect; we await it explicitly via the public method to
    // bypass the setTimeout.
    inst.readyState = 3;
    inst.onclose?.({ code: 1006, reason: '', wasClean: false } as CloseEvent);
    await client.reconnect();
    await flush();

    // No second ticket — we short-circuited because the run is terminal.
    expect(ticketCount).toBe(1);
    expect(WS.instances).toHaveLength(1);
    // Synthesised completed event so chat-panel can exit "running" UI.
    expect(events.some((e) => e.type === 'event.run.completed')).toBe(true);
    expect(client.connectionStatus).toBe('terminal');
  });

  it('opens a fresh socket without replaying past tokens when the run is still running', async () => {
    let ticketCount = 0;
    const { fn: fetchFn } = makeFetch({
      '/api/v1/auth/ws-ticket': () => {
        ticketCount += 1;
        return new Response(JSON.stringify({ ticket: 't', expires_in_s: 60 }), {
          status: 200,
        });
      },
      '/api/v1/runs/run-1': () =>
        new Response(
          JSON.stringify({
            id: 'run-1',
            conversation_id: 'conv-A',
            status: 'running',
          }),
          { status: 200 },
        ),
    });
    const WS = makeMockWebSocket();
    const client = new V1WsClient(
      { conversationId: 'conv-A', getToken: () => null },
      {
        fetch: fetchFn,
        WebSocket: WS as unknown as typeof WebSocket,
        uuid: () => 'k',
        reconnectDelayMs: 0,
      },
    );

    await client.connect();
    expect(ticketCount).toBe(1);
    const inst = openMock(WS);
    deliver(inst, {
      type: 'event.run.started',
      run_id: 'run-1',
      idempotent: false,
    });

    // Drop transport.
    inst.readyState = 3;
    inst.onclose?.({ code: 1006, reason: '', wasClean: false } as CloseEvent);

    await client.reconnect();
    // Fresh ticket + fresh socket.
    expect(ticketCount).toBe(2);
    expect(WS.instances).toHaveLength(2);
    expect(WS.instances[1].url).toContain('/api/v1/conversations/conv-A/stream');
  });

  it('does not reconnect after an explicit disconnect()', async () => {
    const { fn: fetchFn } = makeFetch({
      '/api/v1/auth/ws-ticket': () =>
        new Response(JSON.stringify({ ticket: 't', expires_in_s: 60 }), {
          status: 200,
        }),
    });
    const WS = makeMockWebSocket();
    const client = new V1WsClient(
      { conversationId: 'conv-A', getToken: () => null },
      {
        fetch: fetchFn,
        WebSocket: WS as unknown as typeof WebSocket,
        uuid: () => 'k',
        reconnectDelayMs: 0,
      },
    );
    await client.connect();
    openMock(WS);
    client.disconnect();
    await client.reconnect();
    expect(WS.instances).toHaveLength(1);
    expect(client.connectionStatus).toBe('closed');
  });
});

describe('V1WsClient — cancel + reauth banner hooks', () => {
  it('cancelRun POSTs to the v1 REST endpoint and reports 409 ALREADY_TERMINAL distinctly', async () => {
    let calls = 0;
    const { fn: fetchFn } = makeFetch({
      '/api/v1/runs/run-1/cancel': () => {
        calls += 1;
        if (calls === 1) {
          return new Response(
            JSON.stringify({
              id: 'run-1',
              conversation_id: 'c',
              status: 'running',
            }),
            { status: 202 },
          );
        }
        return new Response(
          JSON.stringify({ code: 'ALREADY_TERMINAL', message: 'no-op' }),
          { status: 409 },
        );
      },
    });
    const WS = makeMockWebSocket();
    const client = new V1WsClient(
      { conversationId: 'conv-A', getToken: () => 't' },
      { fetch: fetchFn, WebSocket: WS as unknown as typeof WebSocket },
    );
    const first = await client.cancelRun('run-1');
    expect(first).toEqual({ ok: true, alreadyTerminal: false });
    const second = await client.cancelRun('run-1');
    expect(second).toEqual({ ok: false, alreadyTerminal: true });
  });

  it('routes event.run.requires_reauth to subscribers (reserved channel for the banner)', async () => {
    const { fn: fetchFn } = makeFetch({
      '/api/v1/auth/ws-ticket': () =>
        new Response(JSON.stringify({ ticket: 't', expires_in_s: 60 }), {
          status: 200,
        }),
    });
    const WS = makeMockWebSocket();
    const client = new V1WsClient(
      { conversationId: 'conv-A', getToken: () => null },
      { fetch: fetchFn, WebSocket: WS as unknown as typeof WebSocket },
    );
    await client.connect();
    const inst = openMock(WS);
    const seen = vi.fn();
    client.onEvent('event.run.requires_reauth', seen);
    deliver(inst, {
      type: 'event.run.requires_reauth',
      run_id: 'r',
      reason: 'refresh_token_expired',
    });
    expect(seen).toHaveBeenCalledOnce();
  });
});
