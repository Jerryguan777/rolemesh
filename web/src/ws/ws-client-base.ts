// WsClientBase — shared lifecycle for ticket-authed WebSocket clients.
//
// Why this exists: `V1WsClient` (conversation streaming) and
// `UserApprovalsClient` (user-scoped approvals) historically duplicated
// ~80% of the same plumbing — exponential-backoff reconnect, a
// `generation` guard so an in-flight ticket fetch that lost a race to
// a `disconnect()` doesn't stamp a fresh socket onto a torn-down
// client, and a tiny status state machine. Two copies in lockstep is
// two reconnect bugs waiting to drift. This base owns the lifecycle;
// subclasses fill in the three subclass-specific seams:
//
//   * `fetchTicket()` — POST whichever ws-ticket endpoint
//   * `buildWsUrl(ticket)` — assemble the per-client WS URL
//   * `handleMessage(data)` — parse + dispatch a received payload
//
// Subclasses MAY override `reconnectNow()` to add a pre-flight check
// before opening a fresh socket (V1WsClient does this — it GETs
// `/runs/{id}` first so a reconnect to a terminal run doesn't
// subscribe to a dead topic and silently wait forever).
//
// The `ConnectionState` plumbing is opt-in: subclasses pass a
// `connectionChannel` string and the base publishes `true` on open /
// `false` on close. Callers that don't want to participate in the
// aggregate dot pass `undefined` (V1WsClient currently routes through
// `ConnectionState`; UserApprovalsClient does not — its socket
// breaking should NOT turn the top-bar dot red because that dot
// reflects the chat stream, not the approvals fanout).

import { connectionState, type ConnectionState } from './connection-state.js';

export type WsConnectionStatus =
  | 'idle'
  | 'connecting'
  | 'open'
  | 'reconnecting'
  | 'closed'
  | 'terminal';

export type WsStatusHandler<S extends string = WsConnectionStatus> = (s: S) => void;

export interface WsClientBaseDeps {
  fetch?: typeof fetch;
  WebSocket?: typeof WebSocket;
  /** Override WS origin (defaults to current location). Tests pass `ws://test`. */
  wsOrigin?: string;
  /** Backoff between reconnects in ms. Tests pass `0` to drive the loop. */
  reconnectDelayMs?: number;
  /** Optional ConnectionState injection. Defaults to the module singleton. */
  connectionState?: ConnectionState;
}

export interface WsClientBaseConfig {
  /** If set, the base will publish `true` on socket open and `false`
   *  on close/teardown under this channel id. Subclasses that don't
   *  participate in the aggregate dot pass `undefined`. */
  connectionChannel?: string;
}

export abstract class WsClientBase<S extends WsConnectionStatus = WsConnectionStatus> {
  protected readonly fetchFn: typeof fetch;
  protected readonly WebSocketCtor: typeof WebSocket;
  protected readonly wsOrigin: string;
  protected readonly reconnectDelayMs: number;
  protected readonly connState: ConnectionState;
  private readonly connectionChannel: string | undefined;

  protected ws: WebSocket | null = null;
  protected explicitlyClosed = false;
  // Generation guard. Bumped on every (re)connect and on every
  // disconnect so any async callback (ticket fetch settled, ws.onopen
  // fired) can compare its captured generation against the live one
  // and bail out if a teardown raced ahead.
  protected generation = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  private statusValue: S;
  private readonly statusHandlers = new Set<WsStatusHandler<S>>();

  protected constructor(
    initialStatus: S,
    deps: WsClientBaseDeps = {},
    config: WsClientBaseConfig = {},
  ) {
    this.statusValue = initialStatus;
    this.fetchFn = deps.fetch ?? globalThis.fetch.bind(globalThis);
    this.WebSocketCtor = deps.WebSocket ?? globalThis.WebSocket;
    this.wsOrigin =
      deps.wsOrigin ??
      (typeof location !== 'undefined'
        ? `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}`
        : '');
    this.reconnectDelayMs = deps.reconnectDelayMs ?? 3000;
    this.connState = deps.connectionState ?? connectionState;
    this.connectionChannel = config.connectionChannel;
  }

  get connectionStatus(): S {
    return this.statusValue;
  }

  onStatus(handler: WsStatusHandler<S>): () => void {
    this.statusHandlers.add(handler);
    return () => this.statusHandlers.delete(handler);
  }

  protected setStatus(next: S): void {
    if (this.statusValue === next) return;
    this.statusValue = next;
    if (this.connectionChannel !== undefined) {
      this.connState.set(this.connectionChannel, next === ('open' as S));
    }
    for (const h of this.statusHandlers) h(next);
  }

  // --- Subclass seams ---

  protected abstract fetchTicket(): Promise<string>;
  protected abstract buildWsUrl(ticket: string): string;
  protected abstract handleMessage(data: unknown): void;

  /** Called by the reconnect timer. Default: just reopen the socket.
   *  Subclasses override to add a pre-flight check (e.g. GET truth
   *  for an active run; if terminal, suppress the reopen). */
  protected async reconnectNow(): Promise<void> {
    if (this.explicitlyClosed) return;
    await this.openSocket();
  }

  // --- Lifecycle (shared) ---

  protected async openSocket(): Promise<void> {
    const gen = ++this.generation;
    this.setStatus('connecting' as S);
    let ticket: string;
    try {
      ticket = await this.fetchTicket();
    } catch {
      // Ticket failure is degraded-but-not-fatal: status flips to
      // closed and we schedule a reconnect attempt. The subclass-level
      // tests pin the "backend not deployed" path here.
      this.setStatus('closed' as S);
      this.scheduleReconnect();
      return;
    }
    if (gen !== this.generation) return; // raced with disconnect()
    const url = this.buildWsUrl(ticket);
    const ws = new this.WebSocketCtor(url);
    this.ws = ws;
    ws.onopen = () => {
      if (gen !== this.generation) return;
      this.setStatus('open' as S);
    };
    ws.onmessage = (evt: MessageEvent) => {
      if (gen !== this.generation) return;
      let data: unknown;
      try {
        data = JSON.parse(typeof evt.data === 'string' ? evt.data : '');
      } catch {
        return;
      }
      if (data && typeof data === 'object') {
        this.handleMessage(data);
      }
    };
    ws.onerror = () => {
      // No-op: onclose is the canonical reconnect trigger. onerror fires
      // a beat earlier without enough info to act on.
    };
    ws.onclose = () => {
      if (gen !== this.generation) return;
      this.ws = null;
      if (this.explicitlyClosed) return;
      this.setStatus('reconnecting' as S);
      this.scheduleReconnect();
    };
  }

  protected scheduleReconnect(): void {
    if (this.explicitlyClosed) return;
    if (this.reconnectTimer) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      if (this.explicitlyClosed) return;
      void this.reconnectNow();
    }, this.reconnectDelayMs);
  }

  /** Shared teardown — closes the socket, cancels reconnect, removes
   *  this client from `ConnectionState`. Subclasses can wrap this in
   *  their public `disconnect()` / `stop()` to retain the existing
   *  name. */
  protected closeAndTeardown(): void {
    this.explicitlyClosed = true;
    this.generation += 1;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.ws) {
      this.ws.onclose = null;
      this.ws.onerror = null;
      try {
        this.ws.close();
      } catch {
        // ignore
      }
      this.ws = null;
    }
    if (this.connectionChannel !== undefined) {
      this.connState.remove(this.connectionChannel);
    }
    this.setStatus('closed' as S);
  }
}
