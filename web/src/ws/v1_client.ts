// v1 WS client for `/api/v1/conversations/{id}/stream` — design §4.
//
// Sits next to the legacy `services/agent-client.ts` during the
// migration. The two clients are kept intentionally separate because
// they encode different semantics:
//
// - The legacy client owns the Stop button (sends `{type:"stop"}` on
//   the legacy `/ws/chat` so the SDK's `interrupt_current_turn`
//   fires without restarting the container — design §4.1).
// - This v1 client owns the streaming surface, Cancel button (which
//   goes through REST `POST /api/v1/runs/{id}/cancel`, not the WS),
//   and reconnect-with-GET-truth.
//
// On the wire we speak the design §4 protocol:
//   client → server: `request.run` { input, idempotency_key, run_id? }
//                    `request.cancel` { run_id }
//                    `request.approval` { approval_id, decision, note? }
//   server → client: `event.run.started` / `event.run.token` /
//                    `event.run.completed` / `event.run.error` /
//                    `event.run.requires_reauth` (reserved, design §6.3 J).
//
// Reconnect contract (design §4 "重连" flow + 01b Open Question 1):
// every reconnect first does `GET /api/v1/runs/{id}` to learn the
// authoritative status. If `completed/failed/cancelled/awaiting_reauth`
// we skip the reconnect entirely so the UI doesn't subscribe to a
// dead topic and silently wait forever. Only `running` triggers an
// actual ws-ticket → WebSocket sequence.
//
// idempotency_key (Open Question 1, locked at session prompt) is
// in-memory only: a fresh UUIDv4 is minted per `send()` call. Page
// refresh loses the dict — that's intentional, because reconnect
// already covers the reload window by GETting truth before opening
// a new socket.

import type { components } from '../api/generated/types.js';

// --- Wire types (mirror what `webui.v1.ws_stream` ships) ---

export type RunStatus = components['schemas']['RunStatus'];

export interface ServerEventBase {
  type: string;
  run_id?: string;
  [k: string]: unknown;
}

export interface RunStartedEvent extends ServerEventBase {
  type: 'event.run.started';
  run_id: string;
  idempotent: boolean;
}
export interface RunTokenEvent extends ServerEventBase {
  type: 'event.run.token';
  run_id: string;
  delta: string;
}
export interface RunCompletedEvent extends ServerEventBase {
  type: 'event.run.completed';
  run_id: string;
}
export interface RunErrorEvent extends ServerEventBase {
  type: 'event.run.error';
  run_id?: string;
  code: string;
  message: string;
  details?: Record<string, unknown>;
}
// `event.run.requires_reauth` is reserved for the user-mode MCP path
// (architecturally present, end-to-end gated on the OIDC branch). The
// reauth banner subscribes here today so the UI is ready when the
// backend starts emitting it.
export interface RunRequiresReauthEvent extends ServerEventBase {
  type: 'event.run.requires_reauth';
  run_id?: string;
  reason?: string;
}

export type ServerEvent =
  | RunStartedEvent
  | RunTokenEvent
  | RunCompletedEvent
  | RunErrorEvent
  | RunRequiresReauthEvent
  | ServerEventBase;

export type ConnectionStatus =
  | 'idle'
  | 'connecting'
  | 'open'
  | 'reconnecting'
  | 'closed'
  | 'terminal';

export type EventHandler = (event: ServerEvent) => void;
export type StatusHandler = (status: ConnectionStatus) => void;

// --- Injection seams for tests ---
//
// Default fetch and WebSocket point to the browser globals; tests
// pass in fakes so the suite never opens real sockets.

export interface V1ClientDeps {
  fetch?: typeof fetch;
  WebSocket?: typeof WebSocket;
  /** UUID factory (overridable in tests). */
  uuid?: () => string;
  /** Override the WS base URL (defaults to current origin). */
  wsOrigin?: string;
  /** Backoff for reconnect attempts in ms. */
  reconnectDelayMs?: number;
}

export interface V1ClientOptions {
  conversationId: string;
  /** Bearer token for REST calls (ws-ticket / GET /runs / GET /backends). */
  getToken: () => string | null;
}

interface RunSnapshot {
  id: string;
  status: RunStatus;
}

const TERMINAL_STATUSES: ReadonlySet<RunStatus> = new Set<RunStatus>([
  'completed',
  'failed',
  'cancelled',
  'awaiting_reauth',
]);

/** Server →client v1 streaming client.
 *
 *  The contract is event-bus shaped: callers subscribe via
 *  {@link onEvent} (filtered by event type or `*` for everything)
 *  and {@link onStatus} for connection lifecycle. The client does
 *  not own UI state — chat-panel maps events to messages.
 */
export class V1WsClient {
  private readonly fetchFn: typeof fetch;
  private readonly WebSocketCtor: typeof WebSocket;
  private readonly uuid: () => string;
  private readonly wsOrigin: string;
  private readonly reconnectDelayMs: number;
  private readonly conversationId: string;
  private readonly getToken: () => string | null;

  private ws: WebSocket | null = null;
  // Per-conversation in-memory idempotency map: input → key. A second
  // `send(text)` with the same `text` inside the same client lifetime
  // reuses the key so a WS redelivery on reconnect collapses to one
  // run server-side. Page reload empties the map by design.
  private readonly idempotencyKeysByInput = new Map<string, string>();
  // The last run_id observed from `event.run.started` — needed so the
  // reconnect path knows which run to GET for truth, and so a Cancel
  // call has a target when the run hasn't echoed back yet.
  private activeRunId: string | null = null;

  private readonly eventHandlers = new Map<string, Set<EventHandler>>();
  private readonly wildcardHandlers = new Set<EventHandler>();
  private readonly statusHandlers = new Set<StatusHandler>();

  private status: ConnectionStatus = 'idle';
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private explicitlyClosed = false;
  // Generation guard — incremented on every (re)connect attempt and
  // disconnect so an async reconnect that lost the race can detect
  // the situation and bail out instead of stamping over a fresh ws.
  private generation = 0;

  constructor(opts: V1ClientOptions, deps: V1ClientDeps = {}) {
    this.conversationId = opts.conversationId;
    this.getToken = opts.getToken;
    this.fetchFn = deps.fetch ?? globalThis.fetch.bind(globalThis);
    this.WebSocketCtor = deps.WebSocket ?? globalThis.WebSocket;
    this.uuid = deps.uuid ?? (() => crypto.randomUUID());
    this.wsOrigin =
      deps.wsOrigin ??
      (typeof location !== 'undefined'
        ? `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}`
        : '');
    this.reconnectDelayMs = deps.reconnectDelayMs ?? 3000;
  }

  /** Current connection status. Mostly for tests and the connection dot. */
  get connectionStatus(): ConnectionStatus {
    return this.status;
  }

  /** The run_id of the most recently started run, or null before the
   *  first `event.run.started`. */
  get currentRunId(): string | null {
    return this.activeRunId;
  }

  /** Reset the cached run_id — used when chat-panel switches conversations. */
  resetRunId(): void {
    this.activeRunId = null;
  }

  // --- Event bus ---

  onEvent(type: string, handler: EventHandler): () => void {
    if (type === '*') {
      this.wildcardHandlers.add(handler);
      return () => this.wildcardHandlers.delete(handler);
    }
    let bucket = this.eventHandlers.get(type);
    if (!bucket) {
      bucket = new Set();
      this.eventHandlers.set(type, bucket);
    }
    bucket.add(handler);
    return () => bucket!.delete(handler);
  }

  onStatus(handler: StatusHandler): () => void {
    this.statusHandlers.add(handler);
    return () => this.statusHandlers.delete(handler);
  }

  private setStatus(next: ConnectionStatus): void {
    if (this.status === next) return;
    this.status = next;
    for (const h of this.statusHandlers) h(next);
  }

  private dispatch(event: ServerEvent): void {
    if (event.type === 'event.run.started' && typeof event.run_id === 'string') {
      this.activeRunId = event.run_id;
    }
    const bucket = this.eventHandlers.get(event.type);
    if (bucket) for (const h of bucket) h(event);
    for (const h of this.wildcardHandlers) h(event);
  }

  // --- REST helpers ---

  private authHeaders(): Record<string, string> {
    const h: Record<string, string> = { Accept: 'application/json' };
    const tok = this.getToken();
    if (tok) h['Authorization'] = `Bearer ${tok}`;
    return h;
  }

  /** Mint a ws-ticket for this conversation. Returns the ticket or
   *  throws on auth/network failure. The handshake-side error codes
   *  are surfaced when the socket connects, not here. */
  async fetchWsTicket(): Promise<string> {
    const resp = await this.fetchFn('/api/v1/auth/ws-ticket', {
      method: 'POST',
      headers: {
        ...this.authHeaders(),
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ conversation_id: this.conversationId }),
    });
    if (!resp.ok) {
      throw new Error(`ws-ticket failed: HTTP ${resp.status}`);
    }
    const body = (await resp.json()) as { ticket: string };
    return body.ticket;
  }

  /** Authoritative truth for a run. Returns null on 404. */
  async fetchRun(runId: string): Promise<RunSnapshot | null> {
    const resp = await this.fetchFn(
      `/api/v1/runs/${encodeURIComponent(runId)}`,
      { headers: this.authHeaders() },
    );
    if (resp.status === 404) return null;
    if (!resp.ok) throw new Error(`GET run failed: HTTP ${resp.status}`);
    const row = (await resp.json()) as { id: string; status: RunStatus };
    return { id: row.id, status: row.status };
  }

  /** Cancel a run via the REST endpoint. Independent of the WS — the
   *  Cancel button calls this and lets the orchestrator hard-stop the
   *  container. */
  async cancelRun(
    runId: string,
  ): Promise<{ ok: boolean; alreadyTerminal: boolean }> {
    const resp = await this.fetchFn(
      `/api/v1/runs/${encodeURIComponent(runId)}/cancel`,
      { method: 'POST', headers: this.authHeaders() },
    );
    if (resp.status === 409) return { ok: false, alreadyTerminal: true };
    if (!resp.ok) throw new Error(`cancel failed: HTTP ${resp.status}`);
    return { ok: true, alreadyTerminal: false };
  }

  // --- WebSocket lifecycle ---

  /** Open the socket, or fetch run truth + open if we have an active
   *  run id. Idempotent: a second call while already open is a no-op. */
  async connect(): Promise<void> {
    if (this.status === 'open' || this.status === 'connecting') return;
    this.explicitlyClosed = false;
    await this.openSocket();
  }

  /** Reconnect after disconnect. First does GET /runs/{id} to decide
   *  whether the run is already terminal — if so, we don't bother
   *  opening a new socket (the server uses DeliverPolicy.NEW so any
   *  past tokens are lost anyway; the SPA already has them in its
   *  message list from the previous connection). */
  async reconnect(): Promise<void> {
    if (this.explicitlyClosed) return;
    if (this.activeRunId) {
      let snap: RunSnapshot | null = null;
      try {
        snap = await this.fetchRun(this.activeRunId);
      } catch {
        // Network blip on the GET — fall through and try the socket
        // anyway; if the run is truly terminal the handshake will
        // succeed but yield no further events.
        snap = null;
      }
      if (snap && TERMINAL_STATUSES.has(snap.status)) {
        // Synthesize the appropriate terminal event so chat-panel can
        // close out UI state. The server won't replay it for us.
        this.setStatus('terminal');
        if (snap.status === 'completed') {
          this.dispatch({
            type: 'event.run.completed',
            run_id: snap.id,
          } as RunCompletedEvent);
        } else {
          this.dispatch({
            type: 'event.run.error',
            run_id: snap.id,
            code: snap.status.toUpperCase(),
            message: `run ended with status=${snap.status}`,
          } as RunErrorEvent);
        }
        return;
      }
    }
    await this.openSocket();
  }

  /** Cleanly close the socket and stop any pending reconnect. */
  disconnect(): void {
    this.explicitlyClosed = true;
    this.generation += 1;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.ws) {
      // Detach handlers first — onclose firing after disconnect()
      // shouldn't trigger another reconnect.
      this.ws.onclose = null;
      this.ws.onerror = null;
      try {
        this.ws.close();
      } catch {
        // ignore
      }
      this.ws = null;
    }
    this.setStatus('closed');
  }

  private async openSocket(): Promise<void> {
    const gen = ++this.generation;
    this.setStatus('connecting');
    let ticket: string;
    try {
      ticket = await this.fetchWsTicket();
    } catch {
      this.setStatus('closed');
      this.scheduleReconnect();
      return;
    }
    if (gen !== this.generation) return; // raced with disconnect()
    const url =
      `${this.wsOrigin}/api/v1/conversations/${encodeURIComponent(this.conversationId)}` +
      `/stream?ticket=${encodeURIComponent(ticket)}`;
    const ws = new this.WebSocketCtor(url);
    this.ws = ws;
    ws.onopen = () => {
      if (gen !== this.generation) return;
      this.setStatus('open');
    };
    ws.onmessage = (evt: MessageEvent) => {
      if (gen !== this.generation) return;
      let data: unknown;
      try {
        data = JSON.parse(typeof evt.data === 'string' ? evt.data : '');
      } catch {
        return;
      }
      if (data && typeof data === 'object' && typeof (data as ServerEvent).type === 'string') {
        this.dispatch(data as ServerEvent);
      }
    };
    ws.onerror = () => {
      // No-op: rely on onclose to drive reconnect.
    };
    ws.onclose = () => {
      if (gen !== this.generation) return;
      this.ws = null;
      if (this.explicitlyClosed) return;
      this.setStatus('reconnecting');
      this.scheduleReconnect();
    };
  }

  private scheduleReconnect(): void {
    if (this.explicitlyClosed) return;
    if (this.reconnectTimer) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      void this.reconnect();
    }, this.reconnectDelayMs);
  }

  // --- Client → server frames ---

  /** Send a `request.run` frame with a fresh (or cached) idempotency_key.
   *
   *  Returns the idempotency_key used so the caller can correlate echoed
   *  `event.run.started.idempotent === true` to "I already sent this".
   *  The same `input` string in this client lifetime reuses the same key
   *  — protects against a redelivery-on-reconnect double-publish. */
  send(input: string): string {
    const key = this.idempotencyKeysByInput.get(input) ?? this.uuid();
    this.idempotencyKeysByInput.set(input, key);
    this.rawSend({
      type: 'request.run',
      input,
      idempotency_key: key,
    });
    return key;
  }

  /** Send a `request.cancel` frame on the WS. Most call sites should
   *  use REST {@link cancelRun} instead — keeping this here for parity
   *  with the server protocol; not currently wired to a UI button. */
  sendCancel(runId: string): void {
    this.rawSend({ type: 'request.cancel', run_id: runId });
  }

  /** Send a `request.approval` frame. Used by the approvals UI (03a). */
  sendApproval(approvalId: string, decision: 'approve' | 'deny', note?: string): void {
    const frame: Record<string, unknown> = {
      type: 'request.approval',
      approval_id: approvalId,
      decision,
    };
    if (note) frame.note = note;
    this.rawSend(frame);
  }

  private rawSend(frame: Record<string, unknown>): void {
    if (!this.ws || this.ws.readyState !== this.WebSocketCtor.OPEN) {
      // request.run isn't queued — replaying after reconnect would
      // either land in the same idempotency window (no-op) or be a
      // duplicate the user no longer wants. Surface the dropped
      // frame as an error event so chat-panel can warn the user.
      this.dispatch({
        type: 'event.run.error',
        code: 'WS_NOT_OPEN',
        message: 'socket not open; frame dropped',
        details: { dropped_type: frame.type },
      } as RunErrorEvent);
      return;
    }
    this.ws.send(JSON.stringify(frame));
  }
}
