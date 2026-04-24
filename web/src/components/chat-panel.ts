import { LitElement, html } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { AgentClient, type AgentStatus, type ConversationSummary, type ServerMessage } from '../services/agent-client.js';

interface AgentStatusState {
  status: AgentStatus;
  tool?: string;
  input?: string;
}

export interface ChatMessage {
  role: 'user' | 'assistant' | 'safety';
  content: string;
  streaming?: boolean;
  // Populated when role === 'safety'. Stage is the pipeline stage the
  // rule fired at (input_prompt / model_output / ...); rule_id is the
  // UUID of the rule when present.
  safetyStage?: string;
  safetyRuleId?: string;
}

@customElement('rm-chat-panel')
export class ChatPanel extends LitElement {
  private client: AgentClient;
  private unsubscribe?: () => void;
  private tokenRefreshHandler?: (e: Event) => void;

  @state() messages: ChatMessage[] = [];
  @state() isStreaming = false;
  @state() connected = false;
  @state() conversations: ConversationSummary[] = [];
  @state() activeChatId: string | null = null;
  @state() sidebarCollapsed: boolean;
  @state() pendingNewChat = false;
  @state() agentStatus: AgentStatusState | null = null;
  // UI state machine: 'idle' | 'running' | 'stopping'
  //  idle     — send button active, accepts new message
  //  running  — stop button active, agent is working (new messages queued)
  //  stopping — user clicked stop, waiting for server confirmation
  //             (button disabled with spinner, auto-recovers after timeout)
  @state() agentState: 'idle' | 'running' | 'stopping' = 'idle';
  private stoppingTimer: ReturnType<typeof setTimeout> | null = null;
  // Watchdog while agentState='running'. Reset on every event; fires after
  // RUNNING_WATCHDOG_MS of silence. Purpose: recover from the case where a
  // WebSocket reconnect happens after the agent's 'done' event was already
  // published — the NATS consumer uses DeliverPolicy.NEW and will never see
  // it, so without this fallback the UI stays stuck on 'thinking' forever.
  private runningWatchdogTimer: ReturnType<typeof setTimeout> | null = null;
  private static readonly RUNNING_WATCHDOG_MS = 120_000;

  constructor() {
    super();
    const params = new URLSearchParams(location.search);
    const agentId = params.get('agent_id') || '';
    // Token resolution priority: URL query param > sessionStorage (OIDC) > empty
    const token = params.get('token') || sessionStorage.getItem('rm_id_token') || '';
    this.activeChatId = params.get('chat_id');
    this.client = new AgentClient(agentId, token);
    this.sidebarCollapsed = localStorage.getItem('rm-sidebar-collapsed') === 'true';
  }

  protected override createRenderRoot() { return this; }

  override connectedCallback() {
    super.connectedCallback();
    this.style.display = 'flex';
    this.style.flexDirection = 'column';
    this.style.minHeight = '0';
    // Fill the parent's resolved height so inner h-full works, and
    // contain overflow here so the sidebar's conversation list and
    // the messages area scroll independently instead of dragging the
    // whole page with one shared scrollbar.
    this.style.height = '100%';
    this.style.overflow = 'hidden';
    this.unsubscribe = this.client.subscribe((msg) => this.handleMessage(msg));

    // React to background token refresh: update client token and reconnect WebSocket
    this.tokenRefreshHandler = (e: Event) => {
      const newToken = (e as CustomEvent<string>).detail;
      if (newToken) {
        this.client.setToken(newToken);
        this.client.reconnect(this.activeChatId ?? undefined);
      }
    };
    window.addEventListener('rm-token-refreshed', this.tokenRefreshHandler);

    if (this.activeChatId) {
      // Restore conversation from URL
      this.client.connect(this.activeChatId);
      this.loadHistory(this.activeChatId);
    } else {
      // No active chat — connect for auth only, show placeholder
      this.client.connect();
    }

    this.refreshConversations();
  }

  override disconnectedCallback() {
    super.disconnectedCallback();
    this.clearStoppingTimer();
    this.clearRunningWatchdog();
    this.unsubscribe?.();
    if (this.tokenRefreshHandler) {
      window.removeEventListener('rm-token-refreshed', this.tokenRefreshHandler);
    }
    this.client.disconnect();
  }

  private async refreshConversations() {
    this.conversations = await this.client.fetchConversations();
  }

  private async loadHistory(chatId: string) {
    const history = await this.client.fetchMessages(chatId);
    this.messages = history
      .filter((m) => m.content.trim())
      .map((m) => ({ role: m.role, content: m.content }));
  }

  private handleMessage(msg: ServerMessage) {
    // Any inbound event while running means the turn is still alive — push
    // the watchdog deadline out. Terminal events (done/error/stopped) clear
    // it explicitly below.
    if (this.agentState === 'running') {
      this.resetRunningWatchdog();
    }
    switch (msg.type) {
      case 'session':
        this.connected = true;
        if (msg.chatId) {
          this.activeChatId = msg.chatId;
          this.updateUrl();
        }
        break;
      case 'thinking':
        // Ignore duplicate thinking messages if already streaming
        if (!this.isStreaming) {
          this.messages = [...this.messages, { role: 'assistant', content: '', streaming: true }];
          this.isStreaming = true;
          // Only elevate idle → running. Don't overwrite a 'stopping' state.
          if (this.agentState === 'idle') {
            this.agentState = 'running';
            this.resetRunningWatchdog();
          }
        }
        break;
      case 'status':
        if (msg.status === 'stopped') {
          // Server confirmed the turn was aborted. Clear indicators and
          // exit the 'stopping' transitional state. Don't touch messages
          // array — any partial text remains visible in the last bubble.
          this.clearStoppingTimer();
          this.clearRunningWatchdog();
          this.agentStatus = null;
          this.agentState = 'idle';
          this.isStreaming = false;
          break;
        }
        // Progress indicator — overwrites prior status; cleared on text/done/error.
        // Don't reset 'stopping' back to 'running' if a late progress event
        // arrives after the user clicked Stop.
        this.agentStatus = { status: msg.status, tool: msg.tool, input: msg.input };
        if (this.agentState === 'idle') {
          this.agentState = 'running';
          this.resetRunningWatchdog();
        }
        break;
      case 'text': {
        // First text chunk means real output has begun — retire the status bar.
        this.agentStatus = null;
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
        this.clearStoppingTimer();
        this.clearRunningWatchdog();
        this.agentStatus = null;
        const last = this.messages[this.messages.length - 1];
        if (last?.role === 'assistant') {
          this.messages = [...this.messages.slice(0, -1), { ...last, streaming: false }];
        }
        this.isStreaming = false;
        this.agentState = 'idle';
        this.refreshConversations();
        break;
      }
      case 'error': {
        this.clearStoppingTimer();
        this.clearRunningWatchdog();
        this.agentStatus = null;
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
        this.agentState = 'idle';
        break;
      }
      case 'safety_blocked': {
        // Safety layer intercepted the turn. Replace the empty placeholder
        // assistant bubble (if one was spawned from the 'thinking' event)
        // with a dedicated safety bubble so the reason is visually distinct
        // from real assistant replies. A concurrent 'done' is still
        // expected and handled normally.
        const last = this.messages[this.messages.length - 1];
        const safetyMsg: ChatMessage = {
          role: 'safety',
          content: msg.reason,
          safetyStage: msg.stage,
          safetyRuleId: msg.rule_id,
        };
        if (last?.role === 'assistant' && last.streaming && !last.content) {
          this.messages = [...this.messages.slice(0, -1), safetyMsg];
        } else {
          this.messages = [...this.messages, safetyMsg];
        }
        this.agentStatus = null;
        break;
      }
    }
  }

  private resetRunningWatchdog() {
    this.clearRunningWatchdog();
    this.runningWatchdogTimer = setTimeout(() => {
      // No inbound events for ~2 minutes while running. Most likely the
      // 'done' event was published during a WebSocket reconnect window and
      // our NATS subscription (DeliverPolicy.NEW) missed it. Recover the
      // UI so the user isn't trapped reloading the page. Any partial text
      // already rendered stays visible.
      if (this.agentState === 'running') {
        this.agentState = 'idle';
        this.agentStatus = null;
        this.isStreaming = false;
        const last = this.messages[this.messages.length - 1];
        if (last?.role === 'assistant' && last.streaming) {
          this.messages = [...this.messages.slice(0, -1), { ...last, streaming: false }];
        }
      }
      this.runningWatchdogTimer = null;
    }, ChatPanel.RUNNING_WATCHDOG_MS);
  }

  private clearRunningWatchdog() {
    if (this.runningWatchdogTimer) {
      clearTimeout(this.runningWatchdogTimer);
      this.runningWatchdogTimer = null;
    }
  }

  private handleStop() {
    if (this.agentState !== 'running') return;
    this.agentState = 'stopping';
    this.client.stop();
    // Fallback: if server never confirms (e.g. container crashed mid-abort),
    // recover to idle after 10s so the UI doesn't wedge.
    this.clearStoppingTimer();
    this.stoppingTimer = setTimeout(() => {
      if (this.agentState === 'stopping') {
        this.agentState = 'idle';
        this.agentStatus = null;
        this.isStreaming = false;
      }
      this.stoppingTimer = null;
    }, 10_000);
  }

  private clearStoppingTimer() {
    if (this.stoppingTimer) {
      clearTimeout(this.stoppingTimer);
      this.stoppingTimer = null;
    }
  }

  private renderStatusLine(s: AgentStatusState): string {
    switch (s.status) {
      case 'queued':
        return 'Queued…';
      case 'container_starting':
        return 'Starting…';
      case 'running':
        return 'Thinking…';
      case 'tool_use': {
        const tool = s.tool || 'Tool';
        return s.input ? `${tool} · ${s.input}` : tool;
      }
      case 'stopped':
        // handleMessage clears agentStatus on 'stopped', so this branch
        // is defensive — the bar shouldn't render in this state.
        return 'Stopped';
    }
  }

  private handleSend(e: CustomEvent<{ content: string }>) {
    const { content } = e.detail;
    if (!content.trim()) return;

    // If pending new chat, generate chat_id and connect
    if (this.pendingNewChat || !this.activeChatId) {
      const newChatId = crypto.randomUUID();
      this.activeChatId = newChatId;
      this.pendingNewChat = false;
      this.client.reconnect(newChatId);
      this.updateUrl();
    }

    // Follow-up mid-turn: finalize any still-streaming assistant bubble
    // before inserting the user's new message. If the previous bubble is
    // an empty placeholder (three-dot spinner from a 'thinking' event
    // that hasn't yet received text), drop it — otherwise it becomes an
    // orphan once the agent's text lands in a fresh bubble after the
    // user message, and the spinner animates forever.
    // If the previous bubble already has partial content, mark it as
    // non-streaming (removes the blinking caret) so any remaining text
    // from the original turn starts a new bubble instead of continuing
    // into an outdated one.
    const last = this.messages[this.messages.length - 1];
    if (last?.role === 'assistant' && last.streaming) {
      if (!last.content) {
        this.messages = this.messages.slice(0, -1);
      } else {
        this.messages = [...this.messages.slice(0, -1), { ...last, streaming: false }];
      }
      // Allow the next 'thinking' event to spawn a fresh placeholder.
      this.isStreaming = false;
    }

    this.messages = [...this.messages, { role: 'user', content }];
    this.client.send(content);
  }

  private handleSelectConversation(e: CustomEvent<{ chatId: string }>) {
    const { chatId } = e.detail;
    if (chatId === this.activeChatId) return;
    this.activeChatId = chatId;
    this.pendingNewChat = false;
    this.messages = [];
    this.isStreaming = false;
    this.agentStatus = null;
    this.agentState = 'idle';
    this.clearStoppingTimer();
    this.updateUrl();
    this.client.reconnect(chatId);
    this.loadHistory(chatId);
  }

  private handleNewChat() {
    this.activeChatId = null;
    this.pendingNewChat = true;
    this.messages = [];
    this.isStreaming = false;
    this.agentStatus = null;
    this.agentState = 'idle';
    this.clearStoppingTimer();
    this.updateUrl();
  }

  private handleToggleSidebar() {
    this.sidebarCollapsed = !this.sidebarCollapsed;
    localStorage.setItem('rm-sidebar-collapsed', String(this.sidebarCollapsed));
  }

  private updateUrl() {
    const params = new URLSearchParams(location.search);
    if (this.activeChatId) {
      params.set('chat_id', this.activeChatId);
    } else {
      params.delete('chat_id');
    }
    const url = `${location.pathname}?${params.toString()}`;
    history.replaceState(null, '', url);
  }

  override render() {
    return html`
      <div class="flex h-full">
        <!-- Sidebar -->
        <rm-sidebar
          .conversations=${this.conversations}
          .activeChatId=${this.activeChatId}
          .collapsed=${this.sidebarCollapsed}
          @select-conversation=${(e: CustomEvent) => this.handleSelectConversation(e)}
          @new-chat=${this.handleNewChat}
          @toggle-sidebar=${this.handleToggleSidebar}
        ></rm-sidebar>

        <!-- Main area -->
        <div class="flex-1 flex flex-col min-w-0">
          <!-- Header -->
          <div class="shrink-0 flex items-center justify-between px-4 py-3 border-b border-surface-3 dark:border-d-surface-3">
            <div class="flex items-center gap-2">
              <!-- Toggle sidebar -->
              <button
                class="w-7 h-7 flex items-center justify-center rounded-lg text-ink-2 dark:text-d-ink-2 hover:bg-surface-2 dark:hover:bg-d-surface-2 transition-colors cursor-pointer"
                @click=${this.handleToggleSidebar}
                title=${this.sidebarCollapsed ? 'Open sidebar' : 'Close sidebar'}
              >
                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" viewBox="0 0 24 24"><path d="M3 12h18"/><path d="M3 6h18"/><path d="M3 18h18"/></svg>
              </button>
              <!-- New chat (visible when sidebar collapsed) -->
              ${this.sidebarCollapsed ? html`
                <button
                  class="w-7 h-7 flex items-center justify-center rounded-lg text-ink-2 dark:text-d-ink-2 hover:bg-surface-2 dark:hover:bg-d-surface-2 transition-colors cursor-pointer"
                  @click=${this.handleNewChat}
                  title="New chat"
                >
                  <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" viewBox="0 0 24 24"><path d="M12 5v14"/><path d="M5 12h14"/></svg>
                </button>
              ` : ''}
              <div class="w-6 h-6 rounded-md bg-gradient-to-br from-brand-light to-brand flex items-center justify-center shadow-sm">
                <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" fill="none" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24">
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

          <!-- Status indicator — one-line transient progress above the input -->
          ${this.agentStatus ? html`
            <div class="shrink-0 px-4">
              <div class="max-w-[720px] mx-auto w-full flex items-center gap-2 py-1.5 text-[12px] text-ink-3 dark:text-d-ink-3">
                <span class="w-1.5 h-1.5 rounded-full bg-brand animate-pulse"></span>
                <span class="truncate">${this.renderStatusLine(this.agentStatus)}</span>
              </div>
            </div>
          ` : ''}

          <!-- Input -->
          <div class="shrink-0 pb-5 pt-2 px-4">
            <div class="max-w-[720px] mx-auto w-full">
              <rm-message-editor
                .agentState=${this.agentState}
                .connected=${this.connected}
                @send=${(e: CustomEvent) => this.handleSend(e)}
                @stop=${() => this.handleStop()}
              ></rm-message-editor>
              <div class="text-center mt-2.5 text-[11px] text-ink-3 dark:text-d-ink-3 select-none">
                AI responses may be inaccurate. Verify important information.
              </div>
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
