// Copied from web/src/ws/v1_client.ts @ cf6b0f1; keep in sync manually until workspace extraction.
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
// On the wire we speak the design §4 protocol. The schemas live in
// `contracts/openapi.yaml` (WsClientFrame / WsServerEvent — PR23) and
// the discriminated-union types below are generated from there, so
// adding a new event type requires editing the yaml first; the
// freshness drift test catches forgetting to regenerate.
//
// Reconnect contract (design §4 "reconnect" flow + 01b Open Question 1):
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
//
// Lifecycle plumbing (connect / disconnect / backoff reconnect /
// generation guard) lives in `WsClientBase`. This subclass only owns
// the v1-specific bits: the event bus, idempotency, the GET-truth
// reconnect pre-flight, and the request frames.

import type { components } from '../api/generated/types.js';
import {
  WsClientBase,
  type WsClientBaseDeps,
  type WsConnectionStatus,
} from './ws-client-base.js';

// --- Wire types (generated from contracts/openapi.yaml) ---

export type RunStatus = components['schemas']['RunStatus'];

/** Server → client event. Discriminated union keyed by `type`; a
 *  switch on `event.type` narrows to the matching member. */
export type ServerEvent = components['schemas']['WsServerEvent'];

/** Re-exports of the individual members so existing call sites that
 *  pattern-match against a specific shape (`RunTokenEvent`,
 *  `RunErrorEvent`, etc.) keep compiling without each importing the
 *  generated components map directly. */
export type RunStartedEvent =
  components['schemas']['WsServerEventRunStarted'];
export type RunTokenEvent =
  components['schemas']['WsServerEventRunToken'];
export type RunCompletedEvent =
  components['schemas']['WsServerEventRunCompleted'];
export type RunErrorEvent =
  components['schemas']['WsServerEventRunError'];

/** HITL tool-approval card push + its deterministic resolution
 *  (docs/12-hitl-approval-architecture.md §10 S4). The card UI subscribes to
 *  these via {@link V1WsClient.onEvent}. */
export type ApprovalRequestedEvent =
  components['schemas']['WsServerEventApprovalRequested'];
export type ApprovalResolvedEvent =
  components['schemas']['WsServerEventApprovalResolved'];

/** Frontdesk v1.5 delegation sub-chip stream (docs §1.5). The four
 *  frames drive the ephemeral `<rm-child-agent-chip>` elements the
 *  chat-panel mounts under the parent agent's status bar: started
 *  mounts a chip (keyed by `child_conv_id`), progress/tool_use update
 *  its status line, completed unmounts it. `run_id` ties the chip to
 *  the parent's active run so a stale redelivery can be ignored. */
export type DelegationStartedEvent =
  components['schemas']['WsServerEventDelegationStarted'];
export type DelegationProgressEvent =
  components['schemas']['WsServerEventDelegationProgress'];
export type DelegationToolUseEvent =
  components['schemas']['WsServerEventDelegationToolUse'];
export type DelegationCompletedEvent =
  components['schemas']['WsServerEventDelegationCompleted'];

export type ConnectionStatus = WsConnectionStatus;

export type EventHandler = (event: ServerEvent) => void;
export type StatusHandler = (status: ConnectionStatus) => void;

// --- Injection seams for tests ---
//
// Default fetch and WebSocket point to the browser globals; tests
// pass in fakes so the suite never opens real sockets.

export interface V1ClientDeps extends WsClientBaseDeps {
  /** UUID factory (overridable in tests). */
  uuid?: () => string;
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
export class V1WsClient extends WsClientBase<ConnectionStatus> {
  private readonly uuid: () => string;
  private readonly conversationId: string;
  private readonly getToken: () => string | null;

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

  constructor(opts: V1ClientOptions, deps: V1ClientDeps = {}) {
    super('idle', deps, { connectionChannel: `v1:${opts.conversationId}` });
    this.conversationId = opts.conversationId;
    this.getToken = opts.getToken;
    this.uuid = deps.uuid ?? (() => crypto.randomUUID());
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

  private dispatch(event: ServerEvent): void {
    if (event.type === 'event.run.started' && typeof event.run_id === 'string') {
      this.activeRunId = event.run_id;
    }
    const bucket = this.eventHandlers.get(event.type);
    if (bucket) for (const h of bucket) h(event);
    for (const h of this.wildcardHandlers) h(event);
  }

  protected handleMessage(data: unknown): void {
    if (
      data &&
      typeof data === 'object' &&
      typeof (data as ServerEvent).type === 'string'
    ) {
      this.dispatch(data as ServerEvent);
    }
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

  // --- WsClientBase seams ---

  protected fetchTicket(): Promise<string> {
    return this.fetchWsTicket();
  }

  protected buildWsUrl(ticket: string): string {
    return (
      `${this.wsOrigin}/api/v1/conversations/${encodeURIComponent(this.conversationId)}` +
      `/stream?ticket=${encodeURIComponent(ticket)}`
    );
  }

  // --- WebSocket lifecycle ---

  /** Open the socket. Idempotent: a second call while already open is a no-op. */
  async connect(): Promise<void> {
    if (this.connectionStatus === 'open' || this.connectionStatus === 'connecting') return;
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

  /** Backoff timer fires this. Routes through `reconnect()` so the
   *  GET-truth pre-flight is honored on every retry. */
  protected override async reconnectNow(): Promise<void> {
    if (this.explicitlyClosed) return;
    await this.reconnect();
  }

  /** Cleanly close the socket and stop any pending reconnect. */
  disconnect(): void {
    this.closeAndTeardown();
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

  /** Send a `request.stop` frame to interrupt the active turn.
   *
   *  Replaces the legacy `AgentClient.stop()` over `/ws/chat`. The
   *  server publishes to `web.stop.{binding}.{chat}` (same NATS
   *  subject the legacy path used) which the orchestrator's
   *  WebNatsGateway translates to an interrupt on the active
   *  container. The `run_id` argument is advisory — the orchestrator
   *  identifies the target from the authenticated handshake
   *  binding+chat, not from this field. Pass it anyway when known
   *  so server-side logs can correlate. */
  stop(): void {
    this.rawSend({
      type: 'request.stop',
      run_id: this.activeRunId ?? undefined,
    });
  }

  /** Send a `request.approval_decision` frame for a pending HITL approval
   *  (docs §10 S4). The frame carries only the `request_id` + verb; the
   *  server stamps the approver identity from the verified WS ticket
   *  (IDOR guard), so the browser never supplies `decided_by`. The
   *  orchestrator relays an approve to the blocked container and edits the
   *  card deterministically — the SPA also receives an
   *  `event.approval.resolved` to update the card in place. */
  sendApprovalDecision(
    requestId: string,
    decision: 'approve' | 'reject',
    note?: string,
  ): void {
    this.rawSend({
      type: 'request.approval_decision',
      request_id: requestId,
      decision,
      note: note ?? undefined,
    });
  }

  private rawSend(frame: Record<string, unknown>): void {
    // Buffer when the socket isn't open and flush on (re)connect, instead of
    // dropping (the old behaviour silently lost a message sent in a reconnect
    // gap). request.run carries an idempotency_key, so a replay collapses to a
    // single run server-side; request.stop/cancel/approval_decision are small
    // control frames whose server handlers are first-wins/idempotent.
    this.queueOrSend(frame);
  }
}
