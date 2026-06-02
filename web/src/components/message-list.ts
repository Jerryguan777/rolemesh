import { LitElement, html } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import type { ChatMessage } from './chat-panel.js';
import type { ApprovalCard } from './approval-store.js';
import './approval-card.js';

@customElement('rm-message-list')
export class MessageList extends LitElement {
  @property({ attribute: false }) messages: ChatMessage[] = [];
  // In-flight + resolved HITL approval cards, interleaved into the message
  // stream by timestamp (see `render`) so each card sits in its true
  // chronological position — right after the user turn that triggered it and
  // before the confirmation — instead of being pinned to the conversation tail.
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

  /** Merge messages and approval cards into one chronological stream.
   *
   *  Both carry an epoch-ms ordering key (`ChatMessage.timestamp` /
   *  `ApprovalCard.orderTs`) that is consistent within its source — server
   *  clock on reload, browser clock on live push. The sort is stable, so when
   *  two items share a key (e.g. coarse server-second granularity) their
   *  original relative order is kept, with messages before cards on a tie. */
  private timeline(): Array<
    { kind: 'message'; ts: number; msg: ChatMessage }
    | { kind: 'approval'; ts: number; card: ApprovalCard }
  > {
    const items: Array<
      { kind: 'message'; ts: number; msg: ChatMessage }
      | { kind: 'approval'; ts: number; card: ApprovalCard }
    > = [
      ...this.messages.map(
        (msg) => ({ kind: 'message' as const, ts: msg.timestamp, msg }),
      ),
      ...this.approvals.map(
        (card) => ({ kind: 'approval' as const, ts: card.orderTs, card }),
      ),
    ];
    return items.sort((a, b) => a.ts - b.ts);
  }

  override render() {
    if (this.messages.length === 0 && this.approvals.length === 0) return html``;
    return html`
      <div class="flex flex-col px-4 pt-6">
        ${this.timeline().map((item) =>
          item.kind === 'message'
            ? html`<rm-message-item .message=${item.msg}></rm-message-item>`
            : html`<div class="pl-10">
              <rm-approval-card
                .requestId=${item.card.requestId}
                .actionSummary=${item.card.actionSummary}
                .status=${item.card.status}
                .mcpServerName=${item.card.mcpServerName}
                .toolName=${item.card.toolName}
                .params=${item.card.params}
                .rationale=${item.card.rationale}
                .requestedAt=${item.card.requestedAt}
                .expiresAt=${item.card.expiresAt}
                .coworkerName=${this.coworkerName}
                .resolvedAt=${item.card.resolvedAt}
                .note=${item.card.note}
                .busy=${this.approvalBusy.has(item.card.requestId)}
              ></rm-approval-card>
            </div>`,
        )}
      </div>
    `;
  }
}
