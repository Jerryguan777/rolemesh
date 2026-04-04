export type ServerMessage =
  | { type: 'session'; chatId: string; agentId: string }
  | { type: 'thinking' }
  | { type: 'text'; content: string }
  | { type: 'done' }
  | { type: 'error'; message: string };

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
  readonly token: string;

  chatId: string | null = null;

  constructor(agentId: string, token: string) {
    this.agentId = agentId;
    this.token = token;
  }

  get connected(): boolean {
    return this._connected;
  }

  connect(chatId?: string): void {
    if (!this.agentId || !this.token) return;
    this.autoReconnect = true;
    if (chatId !== undefined) this.chatId = chatId;

    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    let url = `${protocol}//${location.host}/ws/chat?agent_id=${encodeURIComponent(this.agentId)}&token=${encodeURIComponent(this.token)}`;
    if (this.chatId) {
      url += `&chat_id=${encodeURIComponent(this.chatId)}`;
    }

    this.ws = new WebSocket(url);

    this.ws.onopen = () => {
      this._connected = true;
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

    this.ws.onclose = () => {
      const wasConnected = this._connected;
      this._connected = false;
      if (wasConnected) {
        this.notify({ type: 'error', message: 'Connection lost. Reconnecting...' });
      }
      if (this.autoReconnect) {
        this.scheduleReconnect();
      }
    };

    this.ws.onerror = () => {
      this._connected = false;
    };
  }

  send(content: string): void {
    const payload = JSON.stringify({ type: 'message', content, chatId: this.chatId });
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(payload);
    } else if (this.ws && this.ws.readyState === WebSocket.CONNECTING) {
      // Queue message to be sent once connection opens
      this.pendingMessages.push(payload);
    }
  }

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

  disconnect(): void {
    this.autoReconnect = false;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws?.close();
    this.ws = null;
    this._connected = false;
  }

  reconnect(chatId?: string): void {
    this.disconnect();
    this.connect(chatId);
  }

  // --- REST API helpers ---

  async fetchConversations(): Promise<ConversationSummary[]> {
    const res = await fetch(
      `/api/conversations?agent_id=${encodeURIComponent(this.agentId)}&token=${encodeURIComponent(this.token)}`
    );
    if (!res.ok) return [];
    return res.json();
  }

  async fetchMessages(chatId: string): Promise<HistoryMessage[]> {
    const res = await fetch(
      `/api/conversations/${encodeURIComponent(chatId)}/messages?agent_id=${encodeURIComponent(this.agentId)}&token=${encodeURIComponent(this.token)}`
    );
    if (!res.ok) return [];
    return res.json();
  }
}
