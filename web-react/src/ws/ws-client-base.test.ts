// Copied from web/src/ws/ws-client-base.test.ts @ cf6b0f1; keep in sync manually until workspace extraction.
// WsClientBase — pin the shared lifecycle invariants that
// ticket-authed clients such as V1WsClient rely on:
//   * generation guard: an in-flight ticket fetch resolved AFTER a
//     teardown must NOT open a fresh socket.
//   * onclose drives a backoff reconnect; explicit teardown short-circuits it.
//   * the configured `connectionChannel` flips `ConnectionState` on
//     open/close so the top-bar dot reflects socket reality.
//
// We exercise the base through a minimal subclass so the seams are
// exercised the same way the production subclasses use them.

import { describe, expect, it, vi } from 'vitest';

import { ConnectionState } from './connection-state.js';
import {
  WsClientBase,
  type WsClientBaseDeps,
} from './ws-client-base.js';

interface MockSocket {
  readyState: number;
  url: string;
  onopen: ((e: Event) => void) | null;
  onclose: ((e: CloseEvent) => void) | null;
  onerror: ((e: Event) => void) | null;
  onmessage: ((e: MessageEvent) => void) | null;
  sent: string[];
  close: () => void;
  send: (s: string) => void;
}

function makeMockWebSocket() {
  const instances: MockSocket[] = [];
  const Ctor = function (this: MockSocket, url: string) {
    this.readyState = 0;
    this.url = url;
    this.onopen = null;
    this.onclose = null;
    this.onerror = null;
    this.onmessage = null;
    this.close = () => {
      this.readyState = 3;
    };
    this.sent = [];
    this.send = (s: string) => {
      this.sent.push(s);
    };
    instances.push(this);
  } as unknown as { new (url: string): MockSocket } & {
    OPEN: number;
    CLOSED: number;
    instances: MockSocket[];
  };
  Ctor.OPEN = 1;
  Ctor.CLOSED = 3;
  Ctor.instances = instances;
  return Ctor;
}

function openSocket(WS: ReturnType<typeof makeMockWebSocket>): MockSocket {
  const inst = WS.instances[WS.instances.length - 1];
  inst.readyState = 1;
  inst.onopen?.(new Event('open'));
  return inst;
}

class TestClient extends WsClientBase {
  public ticketCount = 0;
  public messages: unknown[] = [];
  public failNextTicket = false;

  constructor(deps: WsClientBaseDeps = {}, channel = 'test:1') {
    super('idle', deps, { connectionChannel: channel });
  }

  async start(): Promise<void> {
    this.explicitlyClosed = false;
    await this.openSocket();
  }

  stop(): void {
    this.closeAndTeardown();
  }

  /** Public passthrough so tests can exercise the buffer-or-send path. */
  send(frame: Record<string, unknown>): void {
    this.queueOrSend(frame);
  }

  protected async fetchTicket(): Promise<string> {
    this.ticketCount += 1;
    if (this.failNextTicket) {
      this.failNextTicket = false;
      throw new Error('ticket fetch failed');
    }
    return 'ticket-' + this.ticketCount;
  }

  protected buildWsUrl(ticket: string): string {
    return `${this.wsOrigin}/test?t=${ticket}`;
  }

  protected handleMessage(data: unknown): void {
    this.messages.push(data);
  }
}

const flush = () =>
  new Promise<void>((resolve) => setTimeout(resolve, 0));

describe('WsClientBase lifecycle', () => {
  it('opens the socket using the URL built from a fresh ticket', async () => {
    const WS = makeMockWebSocket();
    const client = new TestClient({
      WebSocket: WS as unknown as typeof WebSocket,
      wsOrigin: 'ws://t',
      reconnectDelayMs: 0,
    });
    await client.start();
    openSocket(WS);
    expect(WS.instances).toHaveLength(1);
    expect(WS.instances[0].url).toBe('ws://t/test?t=ticket-1');
    expect(client.connectionStatus).toBe('open');
    client.stop();
  });

  it('flips ConnectionState on open and back on close', async () => {
    const WS = makeMockWebSocket();
    const cs = new ConnectionState();
    const observed: boolean[] = [];
    cs.subscribe((c) => observed.push(c));
    const client = new TestClient({
      WebSocket: WS as unknown as typeof WebSocket,
      wsOrigin: 'ws://t',
      reconnectDelayMs: 60_000,
      connectionState: cs,
    });
    await client.start();
    openSocket(WS);
    expect(cs.connected).toBe(true);
    // Drop the socket — onclose moves the channel to false, dot flips.
    WS.instances[0].readyState = 3;
    WS.instances[0].onclose?.(
      { code: 1006, reason: '', wasClean: false } as CloseEvent,
    );
    expect(cs.connected).toBe(false);
    expect(observed).toEqual([true, false]);
    client.stop();
  });

  it('teardown during an in-flight ticket fetch suppresses the resulting socket', async () => {
    const WS = makeMockWebSocket();
    let resolveTicket: ((t: string) => void) | null = null;
    // Slow ticket fetch — never resolves until we say so.
    const fetchFn = vi.fn().mockReturnValue(
      new Promise<Response>((resolve) => {
        resolveTicket = (t: string) => {
          resolve(
            new Response(JSON.stringify({ ticket: t }), { status: 200 }),
          );
        };
      }),
    ) as unknown as typeof fetch;
    class SlowClient extends TestClient {
      protected override async fetchTicket(): Promise<string> {
        const resp = await this.fetchFn('/api/v1/auth/ws-ticket');
        const body = (await resp.json()) as { ticket: string };
        return body.ticket;
      }
    }
    const client = new SlowClient({
      WebSocket: WS as unknown as typeof WebSocket,
      fetch: fetchFn,
      wsOrigin: 'ws://t',
      reconnectDelayMs: 60_000,
    });
    // Kick off connect — ticket fetch is in flight, never resolves.
    void client.start();
    await flush();
    // No socket yet.
    expect(WS.instances).toHaveLength(0);
    // Teardown while ticket fetch is in flight.
    client.stop();
    expect(client.connectionStatus).toBe('closed');
    // Now resolve the ticket. Generation guard MUST keep the base
    // from constructing a WebSocket post-teardown — otherwise a
    // stopped client would suddenly reconnect.
    resolveTicket!('late-ticket');
    await flush();
    await flush();
    expect(WS.instances).toHaveLength(0);
  });

  it('onclose schedules a reconnect; stop() short-circuits it', async () => {
    const WS = makeMockWebSocket();
    const client = new TestClient({
      WebSocket: WS as unknown as typeof WebSocket,
      wsOrigin: 'ws://t',
      reconnectDelayMs: 0,
    });
    await client.start();
    const first = openSocket(WS);
    first.readyState = 3;
    first.onclose?.(
      { code: 1006, reason: '', wasClean: false } as CloseEvent,
    );
    // Reconnect timer is 0ms — flush microtasks + a macrotask tick.
    await flush();
    await flush();
    expect(WS.instances.length).toBeGreaterThanOrEqual(2);
    client.stop();
    // Drop the new socket too; explicit close MUST suppress further reconnects.
    const second = WS.instances[WS.instances.length - 1];
    second.readyState = 3;
    second.onclose?.(
      { code: 1006, reason: '', wasClean: false } as CloseEvent,
    );
    await flush();
    await flush();
    const countAfterStop = WS.instances.length;
    await flush();
    await flush();
    expect(WS.instances.length).toBe(countAfterStop);
  });

  it('ticket-fetch failure schedules a retry that eventually opens a socket', async () => {
    const WS = makeMockWebSocket();
    const client = new TestClient({
      WebSocket: WS as unknown as typeof WebSocket,
      wsOrigin: 'ws://t',
      reconnectDelayMs: 0,
    });
    client.failNextTicket = true;
    await client.start();
    // First attempt failed (no socket built). Schedule fires via the
    // 0ms timer — flush enough turns for the retry to settle.
    for (let i = 0; i < 5; i += 1) await flush();
    // Two ticket attempts: the failing one + the retry that succeeds.
    expect(client.ticketCount).toBeGreaterThanOrEqual(2);
    expect(WS.instances).toHaveLength(1);
    client.stop();
  });

  it('buffers frames sent while not open and flushes them in order on open', async () => {
    const WS = makeMockWebSocket();
    const client = new TestClient({
      WebSocket: WS as unknown as typeof WebSocket,
      wsOrigin: 'ws://t',
      reconnectDelayMs: 60_000,
    });
    // Queued before any socket exists — must be delivered, not dropped.
    client.send({ type: 'a', n: 1 });
    client.send({ type: 'a', n: 2 });
    await client.start();
    const inst = openSocket(WS);
    expect(inst.sent.map((s) => JSON.parse(s))).toEqual([
      { type: 'a', n: 1 },
      { type: 'a', n: 2 },
    ]);
    client.stop();
  });

});
