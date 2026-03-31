import { LitElement, html } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import type { ChatMessage } from './chat-panel.js';

@customElement('rm-message-list')
export class MessageList extends LitElement {
  @property({ attribute: false }) messages: ChatMessage[] = [];

  protected override createRenderRoot() { return this; }

  override updated() {
    const container = document.getElementById('scroll-area');
    if (container) {
      requestAnimationFrame(() => { container.scrollTop = container.scrollHeight; });
    }
  }

  override render() {
    if (this.messages.length === 0) return html``;
    return html`
      <div class="flex flex-col px-4 pt-6">
        ${this.messages.map((msg) => html`<rm-message-item .message=${msg}></rm-message-item>`)}
      </div>
    `;
  }
}
