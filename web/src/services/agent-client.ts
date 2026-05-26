// Legacy agent client for the pre-v1 chat protocol (`/ws/chat`,
// `/api/conversations`). Most of this surface is **deprecated** by
// the v1.1 cutover (session 01c):
//
//   - Streaming (token / status / done / error events) is owned by
//     `web/src/ws/v1_client.ts` going forward.
//   - REST history / conversation listing now goes through the typed
//     `ApiClient` in `web/src/api/client.ts` (v1 endpoints).
//
// The one method that stays load-bearing is `stop()`, which sends
// `{type:"stop"}` over `/ws/chat` and triggers the SDK's
// `interrupt_current_turn`. That is the **only** way to abort a turn
// without paying a container cold-start tax (design §4.1). Cancel —
// which *does* tear down the container — lives on the v1 client and
// the REST `POST /api/v1/runs/{id}/cancel` endpoint; the two surfaces
// are intentionally separate (Stop vs Cancel hard split).
//
// Do not delete this file or repoint Stop at the v1 client without a
// matching backend protocol change. Tracking work to unify the two
// is deferred to a later session (designed §4.1 + 01c Out of scope).

import { refreshTokenSilent } from './oidc-auth.js';
import { connectionState } from '../ws/connection-state.js';

export type AgentStatus =
  | 'queued'
  | 'container_starting'
  | 'running'
  | 'tool_use'
  | 'stopped';

export type ServerMessage =
  | { type: 'session'; chatId: string; agentId: string }
  | { type: 'thinking' }
  | { type: 'text'; content: string }
  | { type: 'done' }
  | { type: 'error'; message: string }
  | { type: 'status'; status: AgentStatus; tool?: string; input?: string }
  | { type: 'safety_blocked'; reason: string; stage: string; rule_id?: string };

export type MessageHandler = (msg: ServerMessage) => void;

export interface ConversationSummary {
  chatId: string;
  title: string;
  updatedAt: string;
}

export interface HistoryMessage {
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
}

export class AgentClient {
  private ws: WebSocket | null = null;
  private handlers: Set<MessageHandler> = new Set();
  private _connected = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private autoReconnect = true;
  private pendingMessages: string[] = [];
  readonly agentId: string;
  token: string;

  chatId: string | null = null;

  constructor(agentId: string, token: string) {
    this.agentId = agentId;
    this.token = token;
  }

  /** Update the token (called after a silent refresh). Does NOT reconnect. */
  setToken(newToken: string): void {
    this.token = newToken;
  }

  get connected(): boolean {
    return this._connected;
  }

  connect(chatId?: string): void {
    if (!this.agentId || !this.token) return;
    if (chatId !== undefined) this.chatId = chatId;

    // Cancel any pending reconnect timer to prevent double-connect when
    // both scheduleReconnect and an explicit reconnect fire together.
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }

    // Close existing WS. Detach handlers first so the async onclose event
    // doesn't trigger scheduleReconnect or "Connection lost" notifications.
    if (this.ws && this.ws.readyState !== WebSocket.CLOSED) {
      this.ws.onclose = null;
      this.ws.onerror = null;
      this.ws.close();
      this.ws = null;
    }

    this.autoReconnect = true;

    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    let url = `${protocol}//${location.host}/ws/chat?agent_id=${encodeURIComponent(this.agentId)}&token=${encodeURIComponent(this.token)}`;
    if (this.chatId) {
      url += `&chat_id=${encodeURIComponent(this.chatId)}`;
    }

    this.ws = new WebSocket(url);

    this.ws.onopen = () => {
      this._connected = true;
      // Mirror to the shared ConnectionState so the top-bar dot
      // reflects this socket without needing the message-editor
      // CustomEvent relay. Channel id is per-agent so two AgentClient
      // instances (rare, but possible during a coworker swap) don't
      // collide.
      connectionState.set(`stop:${this.agentId}`, true);
      // Flush any messages queued while connecting
      for (const msg of this.pendingMessages) {
        this.ws!.send(msg);
      }
      this.pendingMessages = [];
    };

    this.ws.onmessage = (evt) => {
      try {
        const msg: ServerMessage = JSON.parse(evt.data);
        if (msg.type === 'session') {
          this.chatId = msg.chatId;
        }
        this.notify(msg);
      } catch {
        // ignore parse errors
      }
    };

    this.ws.onclose = (event) => {
      const wasConnected = this._connected;
      this._connected = false;
      connectionState.set(`stop:${this.agentId}`, false);
      if (wasConnected) {
        this.notify({ type: 'error', message: 'Connection lost. Reconnecting...' });
      }
      // Auth failure codes: 1008 (policy violation, e.g. invalid token),
      // 4001-4099 (custom: 4003 = not assigned, 4004 = agent not found).
      // For 1008, try refresh first; for 4xxx, the user truly cannot access this agent.
      if (this.autoReconnect && event.code === 1008) {
        void this.tryRefreshAndReconnect();
        return;
      }
      if (event.code >= 4000 && event.code < 5000) {
        // Permanent failure — stop auto-reconnect to avoid loop
        this.autoReconnect = false;
        return;
      }
      if (this.autoReconnect) {
        this.scheduleReconnect();
      }
    };

    this.ws.onerror = () => {
      this._connected = false;
      connectionState.set(`stop:${this.agentId}`, false);
    };
  }

  /**
   * @deprecated 01c — chat-panel now sends `request.run` via `V1WsClient`.
   *     Kept so any not-yet-migrated caller still compiles, but new
   *     code paths must not call this.
   */
  send(content: string): void {
    const payload = JSON.stringify({ type: 'message', content, chatId: this.chatId });
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(payload);
    } else if (this.ws && this.ws.readyState === WebSocket.CONNECTING) {
      // Queue message to be sent once connection opens
      this.pendingMessages.push(payload);
    }
  }

  /** Send a stop signal to interrupt the agent's current turn.
   *
   * Unlike `send()`, this does NOT queue the signal if the WebSocket is
   * reconnecting — a Stop is time-sensitive and a late replay after
   * reconnect would likely abort a turn the user no longer wants stopped.
   * If the caller sees no Stop effect, they can click again once connected.
   */
  stop(): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: 'stop' }));
    } else {
      console.warn('AgentClient.stop: WebSocket not open, stop signal dropped');
    }
  }

  /**
   * @deprecated 01c — streaming events now go through `V1WsClient.onEvent`.
   *     The legacy client only carries the Stop signal.
   */
  subscribe(handler: MessageHandler): () => void {
    this.handlers.add(handler);
    return () => this.handlers.delete(handler);
  }

  private notify(msg: ServerMessage): void {
    for (const handler of this.handlers) {
      handler(msg);
    }
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, 3000);
  }

  /** WebSocket closed with auth failure → try refreshing token then reconnect. */
  private async tryRefreshAndReconnect(): Promise<void> {
    const newToken = await refreshTokenSilent();
    if (newToken) {
      this.token = newToken;
      this.connect();
    } else {
      // Refresh failed — give up auto-reconnect; let app surface auth-failed
      this.autoReconnect = false;
      window.dispatchEvent(new CustomEvent('rm-auth-failed'));
    }
  }

  disconnect(): void {
    this.autoReconnect = false;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws?.close();
    this.ws = null;
    this._connected = false;
    connectionState.remove(`stop:${this.agentId}`);
  }

  reconnect(chatId?: string): void {
    this.disconnect();
    this.connect(chatId);
  }

  // --- REST API helpers ---

  /** Fetch with automatic 401 → refresh → retry. */
  private async fetchWithRefresh(buildUrl: (token: string) => string): Promise<Response> {
    let res = await fetch(buildUrl(this.token));
    if (res.status === 401) {
      const newToken = await refreshTokenSilent();
      if (newToken) {
        this.token = newToken;
        res = await fetch(buildUrl(newToken));
      }
    }
    return res;
  }

  /**
   * @deprecated 01c — use `ApiClient.listConversations(coworkerId)` instead.
   *     The legacy `/api/conversations` endpoint will be removed once
   *     all chat code paths land on v1.
   */
  async fetchConversations(): Promise<ConversationSummary[]> {
    const res = await this.fetchWithRefresh(
      (token) =>
        `/api/conversations?agent_id=${encodeURIComponent(this.agentId)}&token=${encodeURIComponent(token)}`
    );
    if (!res.ok) return [];
    return res.json();
  }

  /**
   * @deprecated 01c — use `ApiClient.listMessages(conversationId)` instead.
   */
  async fetchMessages(chatId: string): Promise<HistoryMessage[]> {
    const res = await this.fetchWithRefresh(
      (token) =>
        `/api/conversations/${encodeURIComponent(chatId)}/messages?agent_id=${encodeURIComponent(this.agentId)}&token=${encodeURIComponent(token)}`
    );
    if (!res.ok) return [];
    return res.json();
  }
}
