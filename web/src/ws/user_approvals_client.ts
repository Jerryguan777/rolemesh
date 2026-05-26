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
//
// Lifecycle plumbing (start/stop/backoff reconnect/generation guard)
// lives in `WsClientBase`. This client does NOT participate in
// `ConnectionState` — its socket reflects approval fanout health,
// which is separate from chat connectivity (the top-bar dot tracks
// the chat stream).

import type {
  ApprovalRequiredEvent,
  ApprovalResolvedEvent,
} from './v1_client.js';
import { WsClientBase, type WsClientBaseDeps } from './ws-client-base.js';

export type UserApprovalEvent =
  | ApprovalRequiredEvent
  | ApprovalResolvedEvent;

// Approvals client is never "terminal" — that state is run-specific
// in V1WsClient. Narrow the public status type to the subset this
// client actually emits so consumers exhausting the union don't have
// to handle a case the client cannot produce.
export type UserApprovalsStatus =
  | 'idle'
  | 'connecting'
  | 'open'
  | 'reconnecting'
  | 'closed';

export type UserApprovalsHandler = (event: UserApprovalEvent) => void;
export type UserApprovalsStatusHandler = (status: UserApprovalsStatus) => void;

export type UserApprovalsDeps = WsClientBaseDeps;

export interface UserApprovalsOptions {
  /** Bearer token supplier — same shape as V1WsClient. */
  getToken: () => string | null;
}

export class UserApprovalsClient extends WsClientBase<UserApprovalsStatus> {
  private readonly getToken: () => string | null;

  private readonly requiredHandlers = new Set<UserApprovalsHandler>();
  private readonly resolvedHandlers = new Set<UserApprovalsHandler>();

  constructor(opts: UserApprovalsOptions, deps: UserApprovalsDeps = {}) {
    super('idle', deps, {
      // Intentionally undefined: the approvals socket health is
      // distinct from chat connectivity; do not feed the top-bar
      // dot.
      connectionChannel: undefined,
    });
    this.getToken = opts.getToken;
  }

  onRequired(handler: UserApprovalsHandler): () => void {
    this.requiredHandlers.add(handler);
    return () => this.requiredHandlers.delete(handler);
  }

  onResolved(handler: UserApprovalsHandler): () => void {
    this.resolvedHandlers.add(handler);
    return () => this.resolvedHandlers.delete(handler);
  }

  /** Open the user-scoped approvals stream. Idempotent — a second call
   *  while already connecting/open is a no-op. */
  async start(): Promise<void> {
    if (this.connectionStatus === 'open' || this.connectionStatus === 'connecting') return;
    this.explicitlyClosed = false;
    await this.openSocket();
  }

  /** Stop the stream and cancel any pending reconnect. */
  stop(): void {
    this.closeAndTeardown();
  }

  // --- WsClientBase seams ---

  protected buildWsUrl(ticket: string): string {
    return (
      `${this.wsOrigin}/api/v1/users/me/approvals/stream` +
      `?ticket=${encodeURIComponent(ticket)}`
    );
  }

  protected async fetchTicket(): Promise<string> {
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

  protected handleMessage(data: unknown): void {
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
  }
}
