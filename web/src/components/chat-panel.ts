// Chat panel — wired to the v1.1 protocol (session 01c).
//
// Two clients live side-by-side here, by design (§4.1 Stop vs Cancel
// hard split):
//
//   * `V1WsClient` (`web/src/ws/v1_client.ts`) — owns streaming +
//     reconnect + Cancel. All inbound rendering events flow from
//     `event.run.*` frames here.
//   * `AgentClient` (legacy `services/agent-client.ts`) — owns the
//     Stop button. Its `stop()` sends `{type:"stop"}` over `/ws/chat`
//     which triggers the SDK's `interrupt_current_turn`. **The two
//     surfaces are NOT collapsible**: merging Stop into the v1 cancel
//     endpoint would force every soft interrupt through a container
//     cold-start, which is exactly the cost the SDK interrupt avoids.
//
// REST surface goes through the typed `ApiClient` — no admin-prefix
// URL literals in this file (the `lint:no-admin-chat` script
// enforces this).

import { LitElement, html } from 'lit';
import { customElement, state } from 'lit/decorators.js';

import { getApiClient, ApiError } from '../api/client.js';
import type { Conversation, Coworker, Me, Message } from '../api/client.js';
import { AgentClient } from '../services/agent-client.js';
import { V1WsClient, type ServerEvent, type ConnectionStatus } from '../ws/v1_client.js';
import type { InlineApprovalStatus } from './inline-approval.js';
import './inline-approval.js';

interface AgentStatusState {
  /** Mirrored from the legacy status frame so the progress line keeps
   *  rendering even though the new protocol does not carry the same
   *  tool-use detail (yet). The v1 stream only surfaces token / done /
   *  error frames; future granularity lands when the orchestrator
   *  emits richer events. */
  label: string;
}

export interface ChatMessage {
  role: 'user' | 'assistant' | 'safety';
  content: string;
  streaming?: boolean;
  safetyStage?: string;
  safetyRuleId?: string;
}

type RunState = 'idle' | 'running' | 'stopping' | 'cancelling';

@customElement('rm-chat-panel')
export class ChatPanel extends LitElement {
  // v1 client: streaming + Cancel
  private v1: V1WsClient | null = null;
  private v1Unsubscribers: Array<() => void> = [];
  // Legacy client: ONLY for the Stop button. Recreated when the
  // coworker (agent_id) changes; not used for any other purpose.
  private stopClient: AgentClient | null = null;
  private stopClientUnsub?: () => void;
  private tokenRefreshHandler?: (e: Event) => void;
  private readonly api = getApiClient();

  @state() messages: ChatMessage[] = [];
  @state() connected = false;
  // Inline approval cards anchored to the current conversation. The
  // panel renders one card per approval below the message list; it's
  // additive — channel-based notifications from notification.py keep
  // working independently. Keyed by approval_id so a resolved event
  // updates the right card without re-rendering the others.
  @state() private approvals: Map<
    string,
    {
      approvalId: string;
      toolName: string;
      mcpServer: string;
      args: Record<string, unknown>;
      status: InlineApprovalStatus;
      actorName: string;
    }
  > = new Map();
  @state() private me: Me | null = null;
  /** Cached display name of the active coworker — used by the welcome
   *  state ("What should the {name} work on?"). Lazy-fetched on mount;
   *  null while loading or when no coworker is selected. */
  @state() private activeCoworkerName: string | null = null;
  @state() conversations: Conversation[] = [];
  @state() activeConversationId: string | null = null;
  @state() activeCoworkerId: string | null = null;
  @state() sidebarCollapsed: boolean;
  @state() pendingNewChat = false;
  @state() agentStatus: AgentStatusState | null = null;
  // `runState` is the union of both surfaces' state:
  //   idle       — send button active
  //   running    — agent producing tokens; Stop + Cancel both enabled
  //   stopping   — user clicked Stop; waiting for legacy `stopped` ack
  //   cancelling — user clicked Cancel; waiting for run to land
  //                terminal via the v1 stream's error / completed frame
  @state() runState: RunState = 'idle';
  // The run_id of the currently-running invocation, populated from
  // `event.run.started`. Cancel needs this to call REST.
  @state() activeRunId: string | null = null;
  // Sticky terminal flag — when the most recent run completed with
  // status != running, both Stop and Cancel disable until a new send.
  @state() runTerminal = false;
  private stoppingTimer: ReturnType<typeof setTimeout> | null = null;
  private cancellingTimer: ReturnType<typeof setTimeout> | null = null;
  private runningWatchdogTimer: ReturnType<typeof setTimeout> | null = null;
  private static readonly RUNNING_WATCHDOG_MS = 120_000;

  constructor() {
    super();
    const params = new URLSearchParams(location.search);
    this.activeCoworkerId = params.get('agent_id') || null;
    this.activeConversationId = params.get('chat_id');
    this.sidebarCollapsed = localStorage.getItem('rm-sidebar-collapsed') === 'true';
  }

  protected override createRenderRoot() {
    return this;
  }

  override connectedCallback() {
    super.connectedCallback();
    this.style.display = 'flex';
    this.style.flexDirection = 'column';
    this.style.minHeight = '0';
    this.style.height = '100%';
    this.style.overflow = 'hidden';

    this.tokenRefreshHandler = (e: Event) => {
      const newToken = (e as CustomEvent<string>).detail;
      if (newToken) {
        this.api.setToken(newToken);
        if (this.stopClient) {
          this.stopClient.setToken(newToken);
          this.stopClient.reconnect(this.activeConversationId ?? undefined);
        }
        // The v1 client re-reads the token from session storage on
        // every ws-ticket call, so no explicit reconnect is needed.
      }
    };
    window.addEventListener('rm-token-refreshed', this.tokenRefreshHandler);

    void this.bootstrap();
  }

  override disconnectedCallback() {
    super.disconnectedCallback();
    this.clearStoppingTimer();
    this.clearCancellingTimer();
    this.clearRunningWatchdog();
    this.teardownV1();
    this.teardownStopClient();
    if (this.tokenRefreshHandler) {
      window.removeEventListener('rm-token-refreshed', this.tokenRefreshHandler);
    }
  }

  private async bootstrap(): Promise<void> {
    // Load identity early so the inline-approval card can decide
    // whether the current user is in resolved_approvers. A failure
    // here just means the cards always render read-only — the
    // server still gates decide() with 403.
    try {
      this.me = await this.api.getMe();
    } catch {
      this.me = null;
    }
    if (!this.activeCoworkerId) {
      // No coworker selected — render empty state; user must enter
      // chat via the Coworkers route. Phase 1 keeps the legacy URL
      // entry (`?agent_id=...`) working unchanged.
      return;
    }
    // Best-effort lookup for the welcome state's "What should the
    // {coworker} work on?" copy. Cheap small call; failure leaves
    // the welcome on a generic "your coworker" fallback.
    void this.loadActiveCoworkerName(this.activeCoworkerId);
    await this.refreshConversations(this.activeCoworkerId);
    if (this.activeConversationId) {
      await this.openConversation(this.activeConversationId);
    }
  }

  private async loadActiveCoworkerName(coworkerId: string): Promise<void> {
    try {
      const all: Coworker[] = await this.api.listCoworkers();
      const match = all.find((c) => c.id === coworkerId);
      this.activeCoworkerName = match?.name ?? null;
    } catch {
      this.activeCoworkerName = null;
    }
  }

  private async refreshConversations(coworkerId: string): Promise<void> {
    try {
      this.conversations = await this.api.listCoworkerConversations(coworkerId);
    } catch (err) {
      console.warn('listCoworkerConversations failed', err);
      this.conversations = [];
    }
  }

  private async loadMessages(conversationId: string): Promise<void> {
    try {
      const msgs: Message[] = await this.api.listMessages(conversationId);
      this.messages = msgs
        .filter((m) => m.content.trim())
        .map((m) => ({
          role: m.role === 'assistant' ? 'assistant' : 'user',
          content: m.content,
        }));
    } catch (err) {
      console.warn('listMessages failed', err);
      this.messages = [];
    }
  }

  private async openConversation(conversationId: string): Promise<void> {
    if (!this.activeCoworkerId) return;
    this.teardownV1();
    this.teardownStopClient();
    this.activeConversationId = conversationId;
    this.runState = 'idle';
    this.runTerminal = false;
    this.activeRunId = null;
    // Switching conversations drops inline approval cards — they're
    // anchored to a specific conversation_id on the WS.
    this.approvals = new Map();

    // v1 client owns streaming / cancel
    this.v1 = new V1WsClient({
      conversationId,
      getToken: () => sessionStorage.getItem('rm_id_token'),
    });
    this.v1Unsubscribers.push(
      this.v1.onEvent('*', (e) => this.handleV1Event(e)),
      this.v1.onStatus((s) => this.handleV1Status(s)),
    );
    void this.v1.connect();

    // Legacy client only for Stop. Token re-read from sessionStorage.
    const token = sessionStorage.getItem('rm_id_token') ?? '';
    this.stopClient = new AgentClient(this.activeCoworkerId, token);
    this.stopClient.connect(conversationId);

    await this.loadMessages(conversationId);
  }

  private teardownV1(): void {
    for (const off of this.v1Unsubscribers) off();
    this.v1Unsubscribers = [];
    this.v1?.disconnect();
    this.v1 = null;
  }

  private teardownStopClient(): void {
    this.stopClientUnsub?.();
    this.stopClientUnsub = undefined;
    this.stopClient?.disconnect();
    this.stopClient = null;
  }

  private handleV1Status(s: ConnectionStatus): void {
    this.connected = s === 'open';
  }

  private handleV1Event(e: ServerEvent): void {
    if (this.runState === 'running') this.resetRunningWatchdog();
    switch (e.type) {
      case 'event.run.started': {
        const id = typeof e.run_id === 'string' ? e.run_id : null;
        this.activeRunId = id;
        this.runTerminal = false;
        // Spawn the assistant placeholder bubble if the user just sent
        // a message and no token has landed yet.
        const last = this.messages[this.messages.length - 1];
        const hasPlaceholder =
          last?.role === 'assistant' && last.streaming === true;
        if (!hasPlaceholder) {
          this.messages = [
            ...this.messages,
            { role: 'assistant', content: '', streaming: true },
          ];
        }
        this.agentStatus = { label: 'Thinking…' };
        this.runState = 'running';
        this.resetRunningWatchdog();
        break;
      }
      case 'event.run.token': {
        const delta = typeof (e as { delta?: unknown }).delta === 'string'
          ? ((e as { delta: string }).delta)
          : '';
        this.agentStatus = null;
        const last = this.messages[this.messages.length - 1];
        if (last?.role === 'assistant' && last.streaming) {
          this.messages = [
            ...this.messages.slice(0, -1),
            { ...last, content: last.content + delta },
          ];
        } else {
          this.messages = [
            ...this.messages,
            { role: 'assistant', content: delta, streaming: true },
          ];
        }
        break;
      }
      case 'event.run.completed': {
        this.finalizeStreamingBubble();
        this.runState = 'idle';
        this.runTerminal = true;
        this.agentStatus = null;
        this.clearStoppingTimer();
        this.clearCancellingTimer();
        this.clearRunningWatchdog();
        if (this.activeCoworkerId) {
          void this.refreshConversations(this.activeCoworkerId);
        }
        break;
      }
      case 'event.run.error': {
        const message =
          typeof (e as { message?: unknown }).message === 'string'
            ? (e as { message: string }).message
            : 'error';
        const code =
          typeof (e as { code?: unknown }).code === 'string'
            ? (e as { code: string }).code
            : '';
        const last = this.messages[this.messages.length - 1];
        // Safety blocks are surfaced as a dedicated safety bubble so
        // the reason is visually distinct from real assistant text.
        if (code === 'SAFETY_BLOCKED') {
          const details = (e as { details?: Record<string, unknown> }).details ?? {};
          const safetyMsg: ChatMessage = {
            role: 'safety',
            content: message,
            safetyStage:
              typeof details.stage === 'string' ? details.stage : 'unknown',
            safetyRuleId:
              typeof details.rule_id === 'string' ? details.rule_id : undefined,
          };
          if (last?.role === 'assistant' && last.streaming && !last.content) {
            this.messages = [...this.messages.slice(0, -1), safetyMsg];
          } else {
            this.messages = [...this.messages, safetyMsg];
          }
        } else {
          if (last?.role === 'assistant' && last.streaming && !last.content) {
            this.messages = [
              ...this.messages.slice(0, -1),
              { ...last, content: `**Error:** ${message}`, streaming: false },
            ];
          } else {
            this.messages = [
              ...this.messages,
              { role: 'assistant', content: `**Error:** ${message}` },
            ];
          }
        }
        this.runState = 'idle';
        this.runTerminal = true;
        this.agentStatus = null;
        this.clearStoppingTimer();
        this.clearCancellingTimer();
        this.clearRunningWatchdog();
        break;
      }
      case 'event.approval.required': {
        // Engine emits this when a new pending approval lands on
        // this conversation. Spawn an inline card so the approver
        // (and the requester) can see it without leaving chat.
        const raw = e as Record<string, unknown>;
        const approvalId =
          typeof raw.approval_id === 'string' ? raw.approval_id : '';
        if (!approvalId) break;
        const summary =
          (raw.summary && typeof raw.summary === 'object'
            ? (raw.summary as Record<string, unknown>)
            : {}) as Record<string, unknown>;
        const toolName =
          typeof summary.tool_name === 'string' ? summary.tool_name : '';
        const mcpServer =
          typeof summary.mcp_server_name === 'string'
            ? summary.mcp_server_name
            : '';
        const args = (summary.args ?? {}) as Record<string, unknown>;
        const next = new Map(this.approvals);
        next.set(approvalId, {
          approvalId,
          toolName,
          mcpServer,
          args,
          status: 'pending',
          actorName: '',
        });
        this.approvals = next;
        break;
      }
      case 'event.approval.resolved': {
        // Engine has already mapped engine outcome → WS wire enum
        // (approve / deny / expired / cancelled). Update the
        // matching card; if we never saw the .required event
        // (page just loaded mid-flow) silently ignore — the user
        // can refresh.
        const raw = e as Record<string, unknown>;
        const approvalId =
          typeof raw.approval_id === 'string' ? raw.approval_id : '';
        const decision =
          typeof raw.decision === 'string' ? raw.decision : '';
        if (!approvalId) break;
        const existing = this.approvals.get(approvalId);
        if (!existing) break;
        const status: InlineApprovalStatus =
          decision === 'approve'
            ? 'approved'
            : decision === 'deny'
              ? 'denied'
              : decision === 'expired'
                ? 'expired'
                : decision === 'cancelled'
                  ? 'cancelled'
                  : 'unknown';
        const next = new Map(this.approvals);
        next.set(approvalId, { ...existing, status });
        this.approvals = next;
        break;
      }
      case 'event.run.requires_reauth': {
        // Re-broadcast for `<rm-reauth-banner>` to pick up. The banner
        // lives on `<rm-app-shell>` so we go through window event bus
        // rather than tunnelling through chat-panel children.
        const detail = {
          reason: typeof (e as { reason?: unknown }).reason === 'string'
            ? (e as { reason: string }).reason
            : undefined,
          runId: this.activeRunId ?? undefined,
        };
        window.dispatchEvent(
          new CustomEvent('rm-reauth-required', { detail }),
        );
        break;
      }
      default:
        // Future event types — ignore for forward-compat.
        break;
    }
  }

  private finalizeStreamingBubble(): void {
    const last = this.messages[this.messages.length - 1];
    if (last?.role === 'assistant' && last.streaming) {
      this.messages = [...this.messages.slice(0, -1), { ...last, streaming: false }];
    }
  }

  private resetRunningWatchdog(): void {
    this.clearRunningWatchdog();
    this.runningWatchdogTimer = setTimeout(() => {
      if (this.runState === 'running') {
        this.runState = 'idle';
        this.runTerminal = true;
        this.agentStatus = null;
        this.finalizeStreamingBubble();
      }
      this.runningWatchdogTimer = null;
    }, ChatPanel.RUNNING_WATCHDOG_MS);
  }

  private clearRunningWatchdog(): void {
    if (this.runningWatchdogTimer) {
      clearTimeout(this.runningWatchdogTimer);
      this.runningWatchdogTimer = null;
    }
  }

  private clearStoppingTimer(): void {
    if (this.stoppingTimer) {
      clearTimeout(this.stoppingTimer);
      this.stoppingTimer = null;
    }
  }

  private clearCancellingTimer(): void {
    if (this.cancellingTimer) {
      clearTimeout(this.cancellingTimer);
      this.cancellingTimer = null;
    }
  }

  /** Stop = soft interrupt of the current turn via the legacy
   *  `{type:"stop"}` frame. Container stays alive; the next message
   *  is immediate. Design §4.1 — do NOT redirect this to Cancel. */
  private handleStop(): void {
    if (this.runState !== 'running') return;
    if (!this.stopClient) return;
    this.runState = 'stopping';
    this.stopClient.stop();
    this.clearStoppingTimer();
    this.stoppingTimer = setTimeout(() => {
      if (this.runState === 'stopping') {
        // Legacy stream never sent the `stopped` ack. Best-effort
        // recover to idle so the input isn't trapped.
        this.runState = 'idle';
        this.runTerminal = true;
        this.agentStatus = null;
        this.finalizeStreamingBubble();
      }
      this.stoppingTimer = null;
    }, 10_000);
  }

  /** Cancel = hard tear-down of the agent container. Next message
   *  pays cold-start. Design §4.1 — distinct from Stop. */
  private async handleCancel(): Promise<void> {
    if (!this.activeRunId) return;
    if (this.runState !== 'running' && this.runState !== 'stopping') return;
    this.runState = 'cancelling';
    this.clearCancellingTimer();
    this.cancellingTimer = setTimeout(() => {
      if (this.runState === 'cancelling') {
        // The orchestrator's UPDATE is the source of truth; if we
        // never observe a terminal event, fall back to idle so the
        // UI isn't wedged.
        this.runState = 'idle';
        this.runTerminal = true;
        this.agentStatus = null;
        this.finalizeStreamingBubble();
      }
      this.cancellingTimer = null;
    }, 15_000);
    try {
      const r = this.v1
        ? await this.v1.cancelRun(this.activeRunId)
        : await this.api.cancelRun(this.activeRunId);
      if (r.alreadyTerminal) {
        // Already terminal — synthesise the UI transition immediately
        // since no NATS publish happened (no event will arrive).
        this.runState = 'idle';
        this.runTerminal = true;
        this.agentStatus = null;
        this.finalizeStreamingBubble();
        this.clearCancellingTimer();
      }
    } catch (err) {
      console.warn('cancelRun failed', err);
      if (err instanceof ApiError) {
        this.messages = [
          ...this.messages,
          { role: 'assistant', content: `**Error:** cancel failed (${err.message})` },
        ];
      }
      this.runState = 'running';
      this.clearCancellingTimer();
    }
  }

  private async handleSend(e: CustomEvent<{ content: string }>): Promise<void> {
    const { content } = e.detail;
    const text = content.trim();
    if (!text || !this.activeCoworkerId) return;

    // First send in a new conversation — create the conversation row
    // server-side, then open the v1 stream against it.
    if (this.pendingNewChat || !this.activeConversationId) {
      try {
        const conv = await this.api.createCoworkerConversation(
          this.activeCoworkerId,
          null,
        );
        this.pendingNewChat = false;
        await this.openConversation(conv.id);
        this.updateUrl();
      } catch (err) {
        console.warn('createCoworkerConversation failed', err);
        this.messages = [
          ...this.messages,
          { role: 'assistant', content: '**Error:** could not create conversation' },
        ];
        return;
      }
    }

    // Mid-turn follow-up: finalize any in-flight assistant bubble so
    // the new user message doesn't visually interleave with the old
    // stream.
    const last = this.messages[this.messages.length - 1];
    if (last?.role === 'assistant' && last.streaming) {
      if (!last.content) {
        this.messages = this.messages.slice(0, -1);
      } else {
        this.messages = [
          ...this.messages.slice(0, -1),
          { ...last, streaming: false },
        ];
      }
    }

    this.messages = [...this.messages, { role: 'user', content: text }];
    // Reset terminal flag so Stop/Cancel re-enable for the new run.
    this.runTerminal = false;
    this.runState = 'running';
    this.agentStatus = { label: 'Thinking…' };
    this.resetRunningWatchdog();
    this.v1?.send(text);
  }

  private async handleSelectConversation(e: CustomEvent<{ conversationId: string }>): Promise<void> {
    const { conversationId } = e.detail;
    if (conversationId === this.activeConversationId) return;
    this.pendingNewChat = false;
    this.messages = [];
    this.agentStatus = null;
    this.clearStoppingTimer();
    this.clearCancellingTimer();
    this.updateUrl(conversationId);
    await this.openConversation(conversationId);
  }

  private handleNewChat(): void {
    this.teardownV1();
    this.teardownStopClient();
    this.activeConversationId = null;
    this.pendingNewChat = true;
    this.messages = [];
    this.agentStatus = null;
    this.runState = 'idle';
    this.runTerminal = false;
    this.activeRunId = null;
    this.clearStoppingTimer();
    this.clearCancellingTimer();
    this.updateUrl(null);
  }

  private handleToggleSidebar(): void {
    this.sidebarCollapsed = !this.sidebarCollapsed;
    localStorage.setItem('rm-sidebar-collapsed', String(this.sidebarCollapsed));
  }

  private updateUrl(conversationId?: string | null): void {
    const params = new URLSearchParams(location.search);
    const cid = conversationId === undefined ? this.activeConversationId : conversationId;
    if (cid) params.set('chat_id', cid);
    else params.delete('chat_id');
    history.replaceState(null, '', `${location.pathname}?${params.toString()}`);
  }

  private get conversationSummaries() {
    // The sidebar component currently consumes `{chatId, title,
    // updatedAt}`. Map v1 `Conversation` rows into that shape until
    // the sidebar is reworked (out of scope for 01c).
    return this.conversations.map((c) => ({
      chatId: c.id,
      title: c.name ?? 'Conversation',
      updatedAt: c.created_at,
    }));
  }

  override render() {
    const stopDisabled = this.runState !== 'running';
    const cancelDisabled =
      !this.activeRunId ||
      this.runTerminal ||
      this.runState === 'cancelling' ||
      this.runState === 'idle';
    return html`
      <div class="flex h-full">
        <rm-sidebar
          .conversations=${this.conversationSummaries}
          .activeChatId=${this.activeConversationId}
          .collapsed=${this.sidebarCollapsed}
          @select-conversation=${(e: CustomEvent) =>
            void this.handleSelectConversation(
              new CustomEvent('select-conversation', {
                detail: { conversationId: e.detail.chatId },
              }),
            )}
          @new-chat=${this.handleNewChat}
          @toggle-sidebar=${this.handleToggleSidebar}
        ></rm-sidebar>

        <div class="flex-1 flex flex-col min-w-0">
          <div class="shrink-0 flex items-center justify-between px-4 py-3 border-b border-surface-3 dark:border-d-surface-3">
            <div class="flex items-center gap-2">
              <button
                class="w-7 h-7 flex items-center justify-center rounded-lg text-ink-2 dark:text-d-ink-2 hover:bg-surface-2 dark:hover:bg-d-surface-2 transition-colors cursor-pointer"
                @click=${this.handleToggleSidebar}
                title=${this.sidebarCollapsed ? 'Open sidebar' : 'Close sidebar'}
              >
                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" viewBox="0 0 24 24"><path d="M3 12h18"/><path d="M3 6h18"/><path d="M3 18h18"/></svg>
              </button>
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
            <div class="flex items-center gap-3">
              <button
                type="button"
                class="text-[11.5px] px-2 py-1 rounded-md border transition-colors cursor-pointer
                  ${cancelDisabled
                    ? 'border-surface-3 dark:border-d-surface-3 text-ink-4 dark:text-d-ink-4 cursor-not-allowed'
                    : 'border-red-300 dark:border-red-700 text-red-600 dark:text-red-300 hover:bg-red-50 dark:hover:bg-red-900/30'}"
                ?disabled=${cancelDisabled}
                title=${cancelDisabled
                  ? 'Cancel run and release container — disabled (no active run)'
                  : 'Cancel run and release container (next message starts fresh)'}
                @click=${() => void this.handleCancel()}
              >${this.runState === 'cancelling' ? 'Cancelling…' : 'Cancel'}</button>
              <div class="flex items-center gap-1.5">
                <span class="w-2 h-2 rounded-full ${this.connected ? 'bg-emerald-500' : 'bg-red-500'}"></span>
                <span class="text-[11.5px] text-ink-3 dark:text-d-ink-3">${this.connected ? 'Connected' : 'Disconnected'}</span>
              </div>
            </div>
          </div>

          <div class="flex-1 overflow-y-auto" id="scroll-area">
            <div class="max-w-[720px] mx-auto w-full">
              ${this.messages.length === 0 ? this.renderEmpty() : ''}
              <rm-message-list .messages=${this.messages}></rm-message-list>
              ${this.renderApprovalCards()}
              ${this.messages.length > 0 ? html`<div class="h-8"></div>` : ''}
            </div>
          </div>

          ${this.agentStatus ? html`
            <div class="shrink-0 px-4">
              <div class="max-w-[720px] mx-auto w-full flex items-center gap-2 py-1.5 text-[12px] text-ink-3 dark:text-d-ink-3">
                <span class="w-1.5 h-1.5 rounded-full bg-brand animate-pulse"></span>
                <span class="truncate">${this.agentStatus.label}</span>
              </div>
            </div>
          ` : ''}

          <div class="shrink-0 pb-5 pt-2 px-4">
            <div class="max-w-[720px] mx-auto w-full">
              <rm-message-editor
                .agentState=${stopDisabled && this.runState !== 'stopping' ? 'idle' : this.runState === 'stopping' ? 'stopping' : 'running'}
                .connected=${this.connected}
                .canCancel=${!cancelDisabled}
                @send=${(e: CustomEvent) => void this.handleSend(e)}
                @stop=${() => this.handleStop()}
                @request-cancel=${() => void this.handleCancel()}
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

  private renderApprovalCards() {
    if (this.approvals.size === 0) return '';
    const meId = this.me?.user_id ?? '';
    // Iteration order is insertion order, so the oldest pending
    // request renders first. Resolved cards stay around so the
    // user can read the outcome after the WS event landed; a
    // future polish could auto-fade them after N seconds.
    return html`
      <div class="px-4">
        <ul class="flex flex-col gap-2 my-3">
          ${Array.from(this.approvals.values()).map(
            (a) => html`
              <li class="list-none">
                <rm-inline-approval
                  approval-id=${a.approvalId}
                  tool-name=${a.toolName}
                  mcp-server=${a.mcpServer}
                  .args=${a.args}
                  status=${a.status}
                  actor-name=${a.actorName}
                  ?can-decide=${!!meId}
                ></rm-inline-approval>
              </li>
            `,
          )}
        </ul>
      </div>
    `;
  }

  /** Time-of-day greeting. Hours are user-local. */
  private greetingPrefix(): string {
    const h = new Date().getHours();
    if (h < 12) return 'Good morning';
    if (h < 18) return 'Good afternoon';
    return 'Good evening';
  }

  /** First word of `me.name` (falls back to email's local-part, or
   *  "there" so the greeting still reads like a sentence). */
  private firstName(): string {
    if (this.me?.name) {
      const w = this.me.name.trim().split(/\s+/)[0];
      if (w) return w;
    }
    if (this.me?.email) return this.me.email.split('@')[0];
    return 'there';
  }

  /** Send a prefilled message — used by the welcome chips. Synthesizes
   *  the same `send` CustomEvent the message-editor would dispatch. */
  private sendChip(text: string): void {
    void this.handleSend(
      new CustomEvent<{ content: string }>('send', {
        detail: { content: text },
      }),
    );
  }

  private renderEmpty() {
    // No coworker selected — keep the old onboarding hint rather than
    // a half-personalized greeting that lies about state.
    if (!this.activeCoworkerId) {
      return html`
        <div class="rm-chat-empty rm-chat-empty--noop">
          <p>Pick a coworker from Settings → Coworkers to start chatting.</p>
        </div>
      `;
    }
    const coworker = this.activeCoworkerName ?? 'your coworker';
    // Four sample tasks lifted from prototype lines 441-444. They're
    // intentionally generic ad-ops/finance/marketing prompts; a v3
    // chore could swap in coworker-specific suggestions by reading
    // the coworker's system_prompt.
    const chips = [
      "Analyze last week's ad ROAS",
      'Draft a restock plan',
      'Reconcile Q1 ledger',
      'Write 3 listing variants',
    ];
    return html`
      <style>
        .rm-chat-empty {
          flex: 1;
          display: flex;
          flex-direction: column;
          align-items: center;
          justify-content: center;
          text-align: center;
          padding: 24px;
          animation: rm-fade 0.18s ease both;
        }
        .rm-chat-empty h1 {
          font-family: var(--rm-font-display);
          font-weight: 400;
          font-size: 32px;
          letter-spacing: -0.01em;
          margin: 0 0 8px;
          color: var(--rm-ink);
        }
        .rm-chat-empty h1 em {
          font-style: italic;
          color: var(--rm-accent);
        }
        .rm-chat-empty .rm-chat-empty-sub {
          color: var(--rm-ink-3);
          margin: 0 0 22px;
          font-size: 14.5px;
        }
        .rm-chat-empty .rm-chat-empty-sub b {
          color: var(--rm-ink-2);
          font-weight: 500;
        }
        .rm-chat-chips {
          display: flex;
          gap: 8px;
          flex-wrap: wrap;
          justify-content: center;
          max-width: 440px;
        }
        .rm-chat-chip {
          border: 1px solid var(--rm-border);
          background: var(--rm-surface);
          padding: 7px 13px;
          border-radius: 99px;
          font-size: 13px;
          color: var(--rm-ink-2);
          font-family: inherit;
          cursor: pointer;
          transition: var(--rm-transition);
        }
        .rm-chat-chip:hover {
          border-color: var(--rm-accent);
          color: var(--rm-ink);
          transform: translateY(-1px);
        }
        .rm-chat-empty--noop p {
          color: var(--rm-ink-3);
          font-size: 14px;
          margin: 0;
        }
      </style>
      <div class="rm-chat-empty">
        <h1>${this.greetingPrefix()}, <em>${this.firstName()}</em>.</h1>
        <p class="rm-chat-empty-sub">
          What should <b>${coworker}</b> work on?
        </p>
        <div class="rm-chat-chips">
          ${chips.map(
            (t) => html`<button
              type="button"
              class="rm-chat-chip"
              data-testid="welcome-chip"
              @click=${() => this.sendChip(t)}
              ?disabled=${!this.connected}
            >${t}</button>`,
          )}
        </div>
      </div>
    `;
  }
}
