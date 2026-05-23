// User-scoped approvals WS client — sibling to V1WsClient.
//
// Why a second client class instead of reusing V1WsClient: V1WsClient
// is conversation-keyed (its ticket request and WS path both carry a
// `conversation_id`), and its event vocabulary owns the run lifecycle.
// The approvals popover + topbar badge need a CROSS-conversation feed:
// "any approval where the signed-in user is an approver, anywhere in
// the tenant". Threading that through V1WsClient would mean wiring a
// nullable `conversation_id` end-to-end and growing the run-lifecycle
// surface; cleaner to ship a small dedicated client and keep V1WsClient
// focused.
//
// Backend contract (assumed; mirrors the per-conversation pattern):
//
//   POST /api/v1/auth/ws-ticket   { scope: "user-approvals" }
//        → { ticket, expires_in_s }
//   WS   /api/v1/users/me/approvals/stream?ticket=...
//
// On the wire the server emits the same event types V1WsClient already
// understands — `event.approval.required` / `event.approval.resolved`
// — but the upstream NATS subject is keyed on the user_id rather than
// the conversation_id. If the backend endpoint is not yet deployed,
// `start()` swallows the handshake failure and surfaces `status='closed'`
// to the caller; the popover renders a "stale, reconnecting…" hint and
// REST polling continues from the popover's mount-time fetch. We do
// NOT silently fail — connection state is observable via `onStatus`.

import type {
  ApprovalRequiredEvent,
  ApprovalResolvedEvent,
} from './v1_client.js';

export type UserApprovalEvent =
  | ApprovalRequiredEvent
  | ApprovalResolvedEvent;

export type UserApprovalsStatus =
  | 'idle'
  | 'connecting'
  | 'open'
  | 'reconnecting'
  | 'closed';

export type UserApprovalsHandler = (event: UserApprovalEvent) => void;
export type UserApprovalsStatusHandler = (status: UserApprovalsStatus) => void;

export interface UserApprovalsDeps {
  fetch?: typeof fetch;
  WebSocket?: typeof WebSocket;
  /** Override WS origin (defaults to current location). Tests pass `ws://test`. */
  wsOrigin?: string;
  /** Backoff between reconnects. Tests pass `0` to drive the loop. */
  reconnectDelayMs?: number;
}

export interface UserApprovalsOptions {
  /** Bearer token supplier — same shape as V1WsClient. */
  getToken: () => string | null;
}

export class UserApprovalsClient {
  private readonly fetchFn: typeof fetch;
  private readonly WebSocketCtor: typeof WebSocket;
  private readonly wsOrigin: string;
  private readonly reconnectDelayMs: number;
  private readonly getToken: () => string | null;

  private ws: WebSocket | null = null;
  private status: UserApprovalsStatus = 'idle';
  private explicitlyClosed = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private generation = 0;

  private readonly requiredHandlers = new Set<UserApprovalsHandler>();
  private readonly resolvedHandlers = new Set<UserApprovalsHandler>();
  private readonly statusHandlers = new Set<UserApprovalsStatusHandler>();

  constructor(opts: UserApprovalsOptions, deps: UserApprovalsDeps = {}) {
    this.getToken = opts.getToken;
    this.fetchFn = deps.fetch ?? globalThis.fetch.bind(globalThis);
    this.WebSocketCtor = deps.WebSocket ?? globalThis.WebSocket;
    this.wsOrigin =
      deps.wsOrigin ??
      (typeof location !== 'undefined'
        ? `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}`
        : '');
    this.reconnectDelayMs = deps.reconnectDelayMs ?? 3000;
  }

  get connectionStatus(): UserApprovalsStatus {
    return this.status;
  }

  onRequired(handler: UserApprovalsHandler): () => void {
    this.requiredHandlers.add(handler);
    return () => this.requiredHandlers.delete(handler);
  }

  onResolved(handler: UserApprovalsHandler): () => void {
    this.resolvedHandlers.add(handler);
    return () => this.resolvedHandlers.delete(handler);
  }

  onStatus(handler: UserApprovalsStatusHandler): () => void {
    this.statusHandlers.add(handler);
    return () => this.statusHandlers.delete(handler);
  }

  /** Open the user-scoped approvals stream. Idempotent — a second call
   *  while already connecting/open is a no-op. */
  async start(): Promise<void> {
    if (this.status === 'open' || this.status === 'connecting') return;
    this.explicitlyClosed = false;
    await this.openSocket();
  }

  /** Stop the stream and cancel any pending reconnect. */
  stop(): void {
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
    this.setStatus('closed');
  }

  private setStatus(next: UserApprovalsStatus): void {
    if (this.status === next) return;
    this.status = next;
    for (const h of this.statusHandlers) h(next);
  }

  private async openSocket(): Promise<void> {
    const gen = ++this.generation;
    this.setStatus('connecting');
    let ticket: string;
    try {
      ticket = await this.fetchTicket();
    } catch {
      // Surface the failure as a closed/reconnecting state; callers
      // render a degraded UI rather than tearing the app down.
      this.setStatus('closed');
      this.scheduleReconnect();
      return;
    }
    if (gen !== this.generation) return;
    const url =
      `${this.wsOrigin}/api/v1/users/me/approvals/stream` +
      `?ticket=${encodeURIComponent(ticket)}`;
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
      if (!data || typeof data !== 'object') return;
      const type = (data as { type?: unknown }).type;
      if (type === 'event.approval.required') {
        for (const h of this.requiredHandlers) {
          h(data as ApprovalRequiredEvent);
        }
      } else if (type === 'event.approval.resolved') {
        for (const h of this.resolvedHandlers) {
          h(data as ApprovalResolvedEvent);
        }
      }
    };
    ws.onerror = () => {
      // Rely on onclose to drive reconnect.
    };
    ws.onclose = () => {
      if (gen !== this.generation) return;
      this.ws = null;
      if (this.explicitlyClosed) return;
      this.setStatus('reconnecting');
      this.scheduleReconnect();
    };
  }

  private async fetchTicket(): Promise<string> {
    const headers: Record<string, string> = {
      Accept: 'application/json',
      'Content-Type': 'application/json',
    };
    const tok = this.getToken();
    if (tok) headers['Authorization'] = `Bearer ${tok}`;
    const resp = await this.fetchFn('/api/v1/auth/ws-ticket', {
      method: 'POST',
      headers,
      body: JSON.stringify({ scope: 'user-approvals' }),
    });
    if (!resp.ok) {
      throw new Error(`user-approvals ws-ticket failed: HTTP ${resp.status}`);
    }
    const body = (await resp.json()) as { ticket?: string };
    if (!body.ticket) throw new Error('user-approvals ws-ticket missing field');
    return body.ticket;
  }

  private scheduleReconnect(): void {
    if (this.explicitlyClosed) return;
    if (this.reconnectTimer) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      void this.openSocket();
    }, this.reconnectDelayMs);
  }
}
