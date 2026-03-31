import { LitElement, html } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { AgentClient, type ServerMessage } from '../services/agent-client.js';

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  streaming?: boolean;
}

@customElement('rm-chat-panel')
export class ChatPanel extends LitElement {
  private client: AgentClient;
  private unsubscribe?: () => void;

  @state() messages: ChatMessage[] = [];
  @state() isStreaming = false;
  @state() connected = false;

  constructor() {
    super();
    const params = new URLSearchParams(location.search);
    const bindingId = params.get('binding_id') || '';
    const token = params.get('token') || '';
    this.client = new AgentClient(bindingId, token);
  }

  protected override createRenderRoot() { return this; }

  override connectedCallback() {
    super.connectedCallback();
    this.style.display = 'flex';
    this.style.flexDirection = 'column';
    this.style.minHeight = '0';
    this.unsubscribe = this.client.subscribe((msg) => this.handleMessage(msg));
    this.client.connect();
  }

  override disconnectedCallback() {
    super.disconnectedCallback();
    this.unsubscribe?.();
    this.client.disconnect();
  }

  private handleMessage(msg: ServerMessage) {
    switch (msg.type) {
      case 'session':
        this.connected = true;
        break;
      case 'thinking':
        this.messages = [...this.messages, { role: 'assistant', content: '', streaming: true }];
        this.isStreaming = true;
        break;
      case 'text': {
        const last = this.messages[this.messages.length - 1];
        if (last?.role === 'assistant' && last.streaming) {
          this.messages = [
            ...this.messages.slice(0, -1),
            { ...last, content: last.content + msg.content },
          ];
        } else {
          this.messages = [...this.messages, { role: 'assistant', content: msg.content, streaming: true }];
        }
        break;
      }
      case 'done': {
        const last = this.messages[this.messages.length - 1];
        if (last?.role === 'assistant') {
          this.messages = [...this.messages.slice(0, -1), { ...last, streaming: false }];
        }
        this.isStreaming = false;
        break;
      }
      case 'error': {
        const last = this.messages[this.messages.length - 1];
        if (last?.role === 'assistant' && last.streaming && !last.content) {
          this.messages = [
            ...this.messages.slice(0, -1),
            { ...last, content: `**Error:** ${msg.message}`, streaming: false },
          ];
        } else {
          this.messages = [...this.messages, { role: 'assistant', content: `**Error:** ${msg.message}` }];
        }
        this.isStreaming = false;
        break;
      }
    }
  }

  private handleSend(e: CustomEvent<{ content: string }>) {
    const { content } = e.detail;
    if (!content.trim()) return;
    this.messages = [...this.messages, { role: 'user', content }];
    this.client.send(content);
  }

  override render() {
    return html`
      <div class="flex flex-col h-full">
        <!-- Header -->
        <div class="shrink-0 flex items-center justify-between px-5 py-3 border-b border-surface-3 dark:border-d-surface-3">
          <div class="flex items-center gap-2.5">
            <div class="w-7 h-7 rounded-lg bg-gradient-to-br from-brand-light to-brand flex items-center justify-center shadow-sm">
              <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" fill="none" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24">
                <path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/>
              </svg>
            </div>
            <span class="text-[14px] font-semibold text-ink-0 dark:text-d-ink-0">RoleMesh</span>
          </div>
          <div class="flex items-center gap-1.5">
            <span class="w-2 h-2 rounded-full ${this.connected ? 'bg-emerald-500' : 'bg-red-500'}"></span>
            <span class="text-[11.5px] text-ink-3 dark:text-d-ink-3">${this.connected ? 'Connected' : 'Disconnected'}</span>
          </div>
        </div>

        <!-- Messages -->
        <div class="flex-1 overflow-y-auto" id="scroll-area">
          <div class="max-w-[720px] mx-auto w-full">
            ${this.messages.length === 0 ? this.renderEmpty() : ''}
            <rm-message-list .messages=${this.messages}></rm-message-list>
            ${this.messages.length > 0 ? html`<div class="h-8"></div>` : ''}
          </div>
        </div>

        <!-- Input -->
        <div class="shrink-0 pb-5 pt-2 px-4">
          <div class="max-w-[720px] mx-auto w-full">
            <rm-message-editor
              .isStreaming=${this.isStreaming}
              .connected=${this.connected}
              @send=${(e: CustomEvent) => this.handleSend(e)}
            ></rm-message-editor>
            <div class="text-center mt-2.5 text-[11px] text-ink-3 dark:text-d-ink-3 select-none">
              AI responses may be inaccurate. Verify important information.
            </div>
          </div>
        </div>
      </div>
    `;
  }

  private renderEmpty() {
    return html`
      <div class="px-4 pt-[20vh] pb-10 anim-fade">
        <div class="text-center">
          <div class="inline-flex items-center justify-center w-14 h-14 rounded-[16px] bg-gradient-to-br from-brand-light to-brand mb-5 shadow-[0_8px_24px_-6px_rgba(99,102,241,0.35)]">
            <svg xmlns="http://www.w3.org/2000/svg" width="26" height="26" fill="none" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24">
              <path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/>
            </svg>
          </div>
          <h1 class="text-[22px] font-bold text-ink-0 dark:text-d-ink-0 tracking-[-0.03em] mb-1.5">RoleMesh</h1>
          <p class="text-[13.5px] text-ink-2 dark:text-d-ink-2 max-w-sm mx-auto leading-relaxed">
            Start a conversation with your AI coworker.
          </p>
        </div>
      </div>
    `;
  }
}
