import { LitElement, html } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';

export type AgentState = 'idle' | 'running' | 'stopping';

@customElement('rm-message-editor')
export class MessageEditor extends LitElement {
  // Three-state UI:
  //  idle     — brand-colored Send button (↑), enabled when text present
  //  running  — dark Stop button (■), always enabled; clicking emits 'stop'
  //  stopping — dimmed Stop button with spinner ring, disabled
  @property({ type: String }) agentState: AgentState = 'idle';
  @property({ type: Boolean }) connected = false;
  @state() private value = '';
  @state() private focused = false;

  protected override createRenderRoot() { return this; }

  private handleKeyDown(e: KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      this.handleSend();
    }
  }

  private handleInput(e: Event) {
    this.value = (e.target as HTMLTextAreaElement).value;
  }

  private handleSend() {
    // Follow-up messages are allowed even while the agent is running.
    // The orchestrator queues them for after the current turn (see README).
    if (!this.value.trim()) return;
    this.dispatchEvent(new CustomEvent('send', {
      detail: { content: this.value },
      bubbles: true, composed: true,
    }));
    this.value = '';
  }

  private handleStop() {
    this.dispatchEvent(new CustomEvent('stop', {
      bubbles: true, composed: true,
    }));
  }

  private handleButtonClick() {
    if (this.agentState === 'idle') this.handleSend();
    else if (this.agentState === 'running') this.handleStop();
    // stopping: no-op (button is disabled)
  }

  private get canSend(): boolean {
    return this.agentState === 'idle' && this.value.trim().length > 0;
  }

  private renderButtonContent() {
    if (this.agentState === 'idle') {
      // Up-arrow (send)
      return html`<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="m5 12 7-7 7 7"/><path d="M12 19V5"/></svg>`;
    }
    if (this.agentState === 'running') {
      // Filled square (stop)
      return html`<span class="block w-2.5 h-2.5 bg-white rounded-sm"></span>`;
    }
    // stopping: dimmed square + rotating ring overlay
    return html`
      <span class="absolute inset-0 flex items-center justify-center">
        <span class="block w-5 h-5 border-2 border-white/30 border-t-white rounded-full animate-spin"></span>
      </span>
      <span class="block w-2 h-2 bg-white/80 rounded-sm"></span>
    `;
  }

  private get buttonClass(): string {
    const base = 'flex items-center justify-center w-7 h-7 rounded-lg transition-all duration-150 relative';
    if (this.agentState === 'idle') {
      return this.canSend
        ? `${base} bg-brand text-white hover:bg-brand-dark active:scale-95 shadow-sm`
        : `${base} bg-surface-2 dark:bg-d-surface-2 text-ink-4 dark:text-d-ink-4 cursor-default`;
    }
    if (this.agentState === 'running') {
      return `${base} bg-ink-0 dark:bg-d-ink-0 text-white hover:bg-ink-1 dark:hover:bg-d-ink-1 active:scale-95 shadow-sm cursor-pointer`;
    }
    // stopping
    return `${base} bg-ink-0/60 dark:bg-d-ink-0/60 text-white cursor-wait`;
  }

  private get buttonTitle(): string {
    switch (this.agentState) {
      case 'idle': return 'Send';
      case 'running': return 'Stop';
      case 'stopping': return 'Stopping…';
    }
  }

  private get buttonDisabled(): boolean {
    if (this.agentState === 'idle') return !this.canSend;
    if (this.agentState === 'stopping') return true;
    return false;  // running: always enabled
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
            class=${this.buttonClass}
            ?disabled=${this.buttonDisabled}
            title=${this.buttonTitle}
            @click=${this.handleButtonClick}
          >
            ${this.renderButtonContent()}
          </button>
        </div>
      </div>
    `;
  }
}
