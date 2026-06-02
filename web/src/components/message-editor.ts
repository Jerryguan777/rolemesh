// <rm-message-editor> — composer for the chat-panel.
//
// History: shipped in v1.1 as a textarea + send button. v2-C grows the
// toolbar to match `docs/webui-ui-redesign-v2-prototype.html` (lines
// 449-466): an attach affordance on the left, a coworker selector in
// the middle, and the send button on the right. The selector is a
// second access path to the same switch chat-shell offers in its
// sidebar — useful when the user wants to address THIS message to a
// different coworker without leaving the conversation.
//
// Why self-fetching coworkers: chat-panel does not own the coworker
// catalogue (it only tracks `activeCoworkerId` from the URL). Wiring
// the list down would mean touching chat-panel, which v2-A's locked
// "v1.1 zero-touched" rule discourages. The editor is small enough
// that one extra API call on mount is the right trade-off.
//
// Why attach is a no-op: design has no spec'd upload backend yet.
// The button satisfies the prototype look so users do not file
// "missing feature" bugs; clicking surfaces a transient toast that
// explains the v3 dependency.

import { LitElement, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';

import { getApiClient, type Coworker, type Model } from '../api/client.js';
import {
  coworkerSubtitle,
  modelsByIdMap,
} from '../services/coworker-label.js';

export type AgentState = 'idle' | 'running' | 'stopping';

/** Coworker avatar palette — mirror of chat-shell's, kept inline so
 *  the editor doesn't import from a sibling. v3 should lift the
 *  palette + hash function into a shared module. */
const AVATAR_COLOURS = [
  '#C2613F',
  '#3F7DC2',
  '#2F7D5B',
  '#8A5BC2',
  '#C29A3F',
  '#C23F77',
];

function colourForCoworker(c: Coworker | null): string {
  if (!c) return AVATAR_COLOURS[0];
  let hash = 0;
  for (let i = 0; i < c.id.length; i += 1) {
    hash = (hash * 31 + c.id.charCodeAt(i)) | 0;
  }
  return AVATAR_COLOURS[Math.abs(hash) % AVATAR_COLOURS.length];
}

@customElement('rm-message-editor')
export class MessageEditor extends LitElement {
  // Three-state UI:
  //  idle     — accent (terracotta) Send button (↑), enabled when text present
  //  running  — dark Stop button (■), always enabled; clicking emits 'stop'
  //  stopping — dimmed Stop button with spinner ring, disabled
  @property({ type: String }) agentState: AgentState = 'idle';
  @property({ type: Boolean }) connected = false;
  /** True iff a hard-cancel REST call would do anything — set by the
   *  parent (chat-panel) from its run-lifecycle state. v2-C moved the
   *  Cancel button out of the chat-panel top bar and into the kebab
   *  here, but the cancel logic still belongs to chat-panel; we only
   *  surface the affordance. */
  @property({ type: Boolean }) canCancel = false;

  @state() private value = '';
  @state() private focused = false;
  @state() private coworkers: Coworker[] = [];
  @state() private activeCoworkerId: string | null = null;
  /** Tenant model catalogue — used by `coworkerSubtitle` to print the
   *  "Backend · Model" hint in the coworker dropdown. Failure here
   *  degrades gracefully (subtitle drops the model half). */
  @state() private modelsById: Map<string, Model> = new Map();
  @state() private menuOpen = false;
  @state() private kebabOpen = false;
  @state() private attachToast = false;

  protected override createRenderRoot() { return this; }

  override connectedCallback(): void {
    super.connectedCallback();
    this.activeCoworkerId = new URLSearchParams(location.search).get('agent_id');
    void this.loadCoworkers();
    void this.loadModels();
    document.addEventListener('click', this.onDocumentClick, true);
  }

  override disconnectedCallback(): void {
    super.disconnectedCallback();
    document.removeEventListener('click', this.onDocumentClick, true);
  }

  private async loadCoworkers(): Promise<void> {
    try {
      this.coworkers = await getApiClient().listCoworkers();
    } catch {
      // A failure here is non-fatal — the editor still sends messages;
      // only the switcher dropdown goes empty.
      this.coworkers = [];
    }
  }

  private async loadModels(): Promise<void> {
    try {
      const models = await getApiClient().listModels();
      this.modelsById = modelsByIdMap(models);
    } catch {
      // Non-fatal — coworker subtitle falls back to backend-only.
      this.modelsById = new Map();
    }
  }

  private onDocumentClick = (e: MouseEvent) => {
    const target = e.target as Node | null;
    if (!target) return;
    if (this.menuOpen) {
      const trigger = this.querySelector('[data-testid="composer-coworker-btn"]');
      const menu = this.querySelector('[data-testid="composer-coworker-menu"]');
      if (!trigger?.contains(target) && !menu?.contains(target)) {
        this.menuOpen = false;
      }
    }
    if (this.kebabOpen) {
      const trigger = this.querySelector('[data-testid="composer-kebab-btn"]');
      const menu = this.querySelector('[data-testid="composer-kebab-menu"]');
      if (!trigger?.contains(target) && !menu?.contains(target)) {
        this.kebabOpen = false;
      }
    }
  };

  override updated(changed: Map<string, unknown>): void {
    // Surface `connected` flips so chat-shell can render the
    // connection state in its tenant pill (no more standalone
    // Connected/Disconnected row inside chat-panel's top bar). The
    // event bubbles + composes through Light DOM so the shell can
    // catch it via plain @agent-connection on its slot.
    if (changed.has('connected')) {
      this.dispatchEvent(
        new CustomEvent<{ connected: boolean }>('agent-connection', {
          detail: { connected: this.connected },
          bubbles: true,
          composed: true,
        }),
      );
    }
  }

  private get activeCoworker(): Coworker | null {
    return this.coworkers.find((c) => c.id === this.activeCoworkerId) ?? null;
  }

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
    // The orchestrator queues them for after the current turn.
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

  private toggleMenu = () => {
    this.menuOpen = !this.menuOpen;
    if (this.menuOpen) this.kebabOpen = false;
  };

  private toggleKebab = () => {
    this.kebabOpen = !this.kebabOpen;
    if (this.kebabOpen) this.menuOpen = false;
  };

  private requestCancel = () => {
    this.kebabOpen = false;
    if (!this.canCancel) return;
    this.dispatchEvent(
      new CustomEvent('request-cancel', { bubbles: true, composed: true }),
    );
  };

  private selectCoworker = (id: string) => {
    this.menuOpen = false;
    if (id === this.activeCoworkerId) return;
    // Same reload pattern chat-shell uses (locked v2-A decision —
    // chat-panel reads agent_id from URL in its constructor, so the
    // simplest swap is a full reload).
    const params = new URLSearchParams(location.search);
    params.set('agent_id', id);
    params.delete('chat_id');
    location.href = `${location.pathname}?${params.toString()}#/`;
  };

  private onAttachClick = () => {
    // Surface a brief toast so users understand the affordance is
    // intentional but not yet backed by an upload pipeline.
    this.attachToast = true;
    setTimeout(() => { this.attachToast = false; }, 2400);
  };

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
    // Kebab carries the right-alignment now (it sits immediately
    // before us with `ml-auto`); send is its sibling so no margin
    // class needed here.
    const base = 'flex items-center justify-center w-8 h-8 rounded-lg transition-all duration-150 relative';
    if (this.agentState === 'idle') {
      return this.canSend
        // Terracotta accent to match the prototype's `.send` (var(--accent) /
        // accent-ink, hover accent-2). Uses --rm-* directly so it flips for
        // dark mode like the rest of the v2 palette.
        ? `${base} bg-[var(--rm-accent)] text-[var(--rm-accent-ink)] hover:bg-[var(--rm-accent-2)] active:scale-95 shadow-sm`
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

  private renderCoworkerMenu() {
    if (this.coworkers.length === 0) {
      return html`<div
        class="px-3 py-2 text-[12.5px] text-ink-3 dark:text-d-ink-3"
      >No coworkers configured</div>`;
    }
    return html`
      <div class="px-2.5 pt-2 pb-1 text-[11px] font-semibold uppercase tracking-wider text-ink-3 dark:text-d-ink-3">
        Switch coworker
      </div>
      ${this.coworkers.map((c) => html`
        <button
          type="button"
          class=${`flex items-center gap-2.5 w-full text-left px-2.5 py-1.5 rounded-md text-[13px] hover:bg-surface-2 dark:hover:bg-d-surface-2 cursor-pointer ${c.id === this.activeCoworkerId ? 'bg-surface-2 dark:bg-d-surface-2' : ''}`}
          data-testid="composer-coworker-option"
          data-coworker-id=${c.id}
          @click=${() => this.selectCoworker(c.id)}
        >
          <span class="w-2 h-2 rounded-full shrink-0" style=${`background:${colourForCoworker(c)}`}></span>
          <span class="flex-1 truncate">${c.name}</span>
          <!-- shrink-0 + whitespace-nowrap: keep the Backend · Model
               subtitle on one line. If the row is too narrow to fit
               name + subtitle, the truncate on name (flex-1) kicks
               in first — the subtitle is the more important hint
               so we keep it intact. -->
          <span class="text-[11px] text-ink-3 dark:text-d-ink-3 whitespace-nowrap shrink-0">
            ${coworkerSubtitle(c, this.modelsById)}
          </span>
        </button>
      `)}
    `;
  }

  override render() {
    const active = this.activeCoworker;
    const placeholder = active
      ? `Message ${active.name}…`
      : this.connected
        ? 'Send a message…'
        : 'Connecting…';
    return html`
      <div class="relative">
        <div class="rounded-2xl border transition-all duration-200
          ${this.focused
            ? 'border-brand/50 shadow-[0_0_0_3px_rgba(99,102,241,0.08)] dark:shadow-[0_0_0_3px_rgba(99,102,241,0.12)]'
            : 'border-surface-3 dark:border-d-surface-3 shadow-sm'}
          bg-surface-0 dark:bg-d-surface-1">

          <textarea
            class="w-full resize-none bg-transparent px-3.5 py-3 text-[13.5px] text-ink-0 dark:text-d-ink-0 placeholder:text-ink-3 dark:placeholder:text-d-ink-3 outline-none leading-relaxed"
            placeholder=${placeholder}
            rows="1"
            style="max-height: 160px; field-sizing: content; min-height: 1lh;"
            .value=${this.value}
            ?disabled=${!this.connected}
            @input=${this.handleInput}
            @keydown=${this.handleKeyDown}
            @focus=${() => { this.focused = true; }}
            @blur=${() => { this.focused = false; }}
          ></textarea>

          <div class="flex items-center gap-1.5 px-2 pb-2">
            <button
              type="button"
              class="flex items-center justify-center w-8 h-8 rounded-lg text-ink-2 dark:text-d-ink-2 hover:bg-surface-2 dark:hover:bg-d-surface-2 transition-colors cursor-pointer"
              title="Attach files (coming in v3)"
              data-testid="composer-attach"
              @click=${this.onAttachClick}
            >
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="m21.4 11.05-9.19 9.2a5 5 0 0 1-7.07-7.08l9.19-9.19a3.33 3.33 0 0 1 4.71 4.71l-9.2 9.19a1.67 1.67 0 0 1-2.36-2.36l8.49-8.48"/>
              </svg>
            </button>

            <div class="relative">
              <button
                type="button"
                class="flex items-center gap-2 h-8 px-2.5 rounded-lg text-[13px] text-ink-2 dark:text-d-ink-2 hover:bg-surface-2 dark:hover:bg-d-surface-2 border border-transparent hover:border-surface-3 dark:hover:border-d-surface-3 transition-colors cursor-pointer"
                data-testid="composer-coworker-btn"
                aria-haspopup="menu"
                aria-expanded=${this.menuOpen}
                @click=${this.toggleMenu}
              >
                <span
                  class="w-2 h-2 rounded-full shrink-0"
                  style=${`background:${colourForCoworker(active)}`}
                ></span>
                <span class="max-w-[160px] truncate">${active?.name ?? 'No coworker'}</span>
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" class="text-ink-3 dark:text-d-ink-3">
                  <path d="m6 9 6 6 6-6"/>
                </svg>
              </button>
              ${this.menuOpen
                ? html`<div
                    class="absolute bottom-full left-0 mb-1.5 z-30 min-w-[340px] rounded-lg border border-surface-3 dark:border-d-surface-3 bg-surface-0 dark:bg-d-surface-1 shadow-lg p-1.5"
                    role="menu"
                    data-testid="composer-coworker-menu"
                  >${this.renderCoworkerMenu()}</div>`
                : nothing}
            </div>

            <div class="relative ml-auto">
              <button
                type="button"
                class="flex items-center justify-center w-8 h-8 rounded-lg text-ink-2 dark:text-d-ink-2 hover:bg-surface-2 dark:hover:bg-d-surface-2 transition-colors cursor-pointer"
                title="More actions"
                data-testid="composer-kebab-btn"
                aria-haspopup="menu"
                aria-expanded=${this.kebabOpen}
                @click=${this.toggleKebab}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                  <circle cx="12" cy="5" r="1.6"/>
                  <circle cx="12" cy="12" r="1.6"/>
                  <circle cx="12" cy="19" r="1.6"/>
                </svg>
              </button>
              ${this.kebabOpen
                ? html`<div
                    class="absolute bottom-full right-0 mb-1.5 z-30 min-w-[260px] rounded-lg border border-surface-3 dark:border-d-surface-3 bg-surface-0 dark:bg-d-surface-1 shadow-lg p-1.5"
                    role="menu"
                    data-testid="composer-kebab-menu"
                  >
                    <button
                      type="button"
                      class=${`flex flex-col items-start gap-0.5 w-full text-left px-2.5 py-1.5 rounded-md text-[13px] ${this.canCancel ? 'text-red-700 dark:text-red-300 hover:bg-surface-2 dark:hover:bg-d-surface-2 cursor-pointer' : 'text-ink-4 dark:text-d-ink-4 cursor-not-allowed'}`}
                      data-testid="composer-kebab-cancel"
                      ?disabled=${!this.canCancel}
                      @click=${this.requestCancel}
                    >
                      <span class="font-medium">Cancel run</span>
                      <span class="text-[11px] text-ink-3 dark:text-d-ink-3">
                        ${this.canCancel
                          ? 'Hard stop — releases the container.'
                          : 'No active run to cancel.'}
                      </span>
                    </button>
                  </div>`
                : nothing}
            </div>
            <button
              class=${this.buttonClass}
              ?disabled=${this.buttonDisabled}
              title=${this.buttonTitle}
              data-testid="composer-send"
              @click=${this.handleButtonClick}
            >
              ${this.renderButtonContent()}
            </button>
          </div>
        </div>

        ${this.attachToast
          ? html`<div
              class="absolute -top-9 left-0 right-0 mx-auto w-fit px-3 py-1.5 rounded-md bg-ink-0 dark:bg-d-ink-0 text-white text-[12px] shadow-lg pointer-events-none"
              data-testid="composer-attach-toast"
              role="status"
            >File upload is a v3 feature.</div>`
          : nothing}
      </div>
    `;
  }
}
