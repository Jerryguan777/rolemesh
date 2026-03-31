import { LitElement, html } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';

@customElement('rm-message-editor')
export class MessageEditor extends LitElement {
  @property({ type: Boolean }) isStreaming = false;
  @property({ type: Boolean }) connected = false;
  @state() private value = '';
  @state() private focused = false;

  protected override createRenderRoot() { return this; }

  private handleKeyDown(e: KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); this.handleSend(); }
  }

  private handleInput(e: Event) {
    this.value = (e.target as HTMLTextAreaElement).value;
  }

  private handleSend() {
    if (this.isStreaming || !this.value.trim()) return;
    this.dispatchEvent(new CustomEvent('send', {
      detail: { content: this.value },
      bubbles: true, composed: true,
    }));
    this.value = '';
  }

  private get canSend() {
    return !this.isStreaming && this.value.trim().length > 0;
  }

  override render() {
    return html`
      <div class="rounded-2xl border transition-all duration-200
        ${this.focused
          ? 'border-brand/50 shadow-[0_0_0_3px_rgba(99,102,241,0.08)] dark:shadow-[0_0_0_3px_rgba(99,102,241,0.12)]'
          : 'border-surface-3 dark:border-d-surface-3 shadow-sm'}
        bg-surface-0 dark:bg-d-surface-1">

        <textarea
          class="w-full resize-none bg-transparent px-3.5 py-3 text-[13.5px] text-ink-0 dark:text-d-ink-0 placeholder:text-ink-3 dark:placeholder:text-d-ink-3 outline-none leading-relaxed"
          placeholder=${this.connected ? 'Send a message...' : 'Connecting...'}
          rows="1"
          style="max-height: 160px; field-sizing: content; min-height: 1lh;"
          .value=${this.value}
          ?disabled=${!this.connected}
          @input=${this.handleInput}
          @keydown=${this.handleKeyDown}
          @focus=${() => { this.focused = true; }}
          @blur=${() => { this.focused = false; }}
        ></textarea>

        <div class="flex items-center justify-end px-2.5 pb-2">
          <button
            class="flex items-center justify-center w-7 h-7 rounded-lg transition-all duration-150
              ${this.canSend
                ? 'bg-brand text-white hover:bg-brand-dark active:scale-95 shadow-sm'
                : 'bg-surface-2 dark:bg-d-surface-2 text-ink-4 dark:text-d-ink-4 cursor-default'}"
            ?disabled=${!this.canSend}
            @click=${this.handleSend}
          >
            ${this.isStreaming
              ? html`<span class="block w-3 h-3 border-2 border-white/50 border-t-white rounded-full animate-spin"></span>`
              : html`<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="m5 12 7-7 7 7"/><path d="M12 19V5"/></svg>`
            }
          </button>
        </div>
      </div>
    `;
  }
}
