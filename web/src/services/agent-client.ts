export type ServerMessage =
  | { type: 'session'; chatId: string; bindingId: string }
  | { type: 'thinking' }
  | { type: 'text'; content: string }
  | { type: 'done' }
  | { type: 'error'; message: string };

export type MessageHandler = (msg: ServerMessage) => void;

export class AgentClient {
  private ws: WebSocket | null = null;
  private handlers: Set<MessageHandler> = new Set();
  private _connected = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private bindingId: string;
  private token: string;

  chatId: string | null = null;

  constructor(bindingId: string, token: string) {
    this.bindingId = bindingId;
    this.token = token;
  }

  get connected(): boolean {
    return this._connected;
  }

  connect(): void {
    if (!this.bindingId || !this.token) return;

    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${protocol}//${location.host}/ws/chat?binding_id=${encodeURIComponent(this.bindingId)}&token=${encodeURIComponent(this.token)}`;

    this.ws = new WebSocket(url);

    this.ws.onopen = () => {
      this._connected = true;
      this.notify({ type: 'session', chatId: '', bindingId: this.bindingId });
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
      this.scheduleReconnect();
    };

    this.ws.onerror = () => {
      this._connected = false;
    };
  }

  send(content: string): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(JSON.stringify({ type: 'message', content }));
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
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws?.close();
    this.ws = null;
    this._connected = false;
  }
}
