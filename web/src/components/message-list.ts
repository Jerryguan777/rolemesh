import { LitElement, html } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import type { ChatMessage } from './chat-panel.js';
import type { ApprovalCard } from './approval-store.js';
import './approval-card.js';

@customElement('rm-message-list')
export class MessageList extends LitElement {
  @property({ attribute: false }) messages: ChatMessage[] = [];
  // In-flight + resolved HITL approval cards, rendered inline at the tail of the
  // conversation stream so they scroll with it (not as a detached footer block).
  // An approval is raised by the latest agent turn and the container blocks until
  // it resolves, so the tail is its correct chronological position.
  @property({ attribute: false }) approvals: ApprovalCard[] = [];
  @property({ attribute: false }) approvalBusy: Set<string> = new Set();
  @property() coworkerName = '';

  protected override createRenderRoot() { return this; }

  // "Stick to bottom" only while the user is already near the bottom. Without
  // this guard the unconditional scroll-to-bottom below yanked the view down on
  // EVERY re-render — and re-renders are frequent (connection-state changes / WS
  // reconnect churn) — so a user who scrolled up to read an approval card or
  // earlier messages was snapped back to the bottom and could not scroll.
  private stickToBottom = true;
  private scrollEl: HTMLElement | null = null;
  private readonly onScroll = () => {
    const c = this.scrollEl;
    if (c) this.stickToBottom = c.scrollHeight - c.scrollTop - c.clientHeight < 80;
  };

  override firstUpdated() {
    this.scrollEl = document.getElementById('scroll-area');
    this.scrollEl?.addEventListener('scroll', this.onScroll, { passive: true });
  }

  override disconnectedCallback() {
    super.disconnectedCallback();
    this.scrollEl?.removeEventListener('scroll', this.onScroll);
  }

  override updated() {
    const container = this.scrollEl ?? document.getElementById('scroll-area');
    if (container && this.stickToBottom) {
      requestAnimationFrame(() => { container.scrollTop = container.scrollHeight; });
    }
  }

  override render() {
    if (this.messages.length === 0 && this.approvals.length === 0) return html``;
    return html`
      <div class="flex flex-col px-4 pt-6">
        ${this.messages.map((msg) => html`<rm-message-item .message=${msg}></rm-message-item>`)}
        ${this.approvals.map(
          (c) => html`<rm-approval-card
            .requestId=${c.requestId}
            .actionSummary=${c.actionSummary}
            .status=${c.status}
            .mcpServerName=${c.mcpServerName}
            .toolName=${c.toolName}
            .params=${c.params}
            .rationale=${c.rationale}
            .requestedAt=${c.requestedAt}
            .expiresAt=${c.expiresAt}
            .coworkerName=${this.coworkerName}
            .resolvedAt=${c.resolvedAt}
            .note=${c.note}
            .busy=${this.approvalBusy.has(c.requestId)}
          ></rm-approval-card>`,
        )}
      </div>
    `;
  }
}
