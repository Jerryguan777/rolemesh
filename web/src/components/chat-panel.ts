// Chat panel — wired to the v1.1 protocol (session 01c).
//
// Owns a single WS client (`V1WsClient`) that handles streaming,
// reconnect, Cancel, and **Stop**. PR-B (2026-05-31) collapsed the
// Stop path into the v1 `request.stop` client frame — the
// orchestrator-side handler stayed on the legacy
// `web.stop.{binding}.{chat}` NATS subject (subject reuse means
// zero orch changes), but the SPA no longer maintains the second
// WebSocket to the legacy `/ws/chat` endpoint. Design §4.1's Stop
// vs Cancel hard split still holds — they remain semantically
// distinct (Stop = soft interrupt, container stays warm; Cancel =
// hard terminate). The split is now expressed inside the v1
// protocol rather than across two endpoints.
//
// REST surface goes through the typed `ApiClient` — no admin-prefix
// URL literals in this file (the `lint:no-admin-chat` script
// enforces this).

import { LitElement, html } from 'lit';
import { customElement, state } from 'lit/decorators.js';

import { getApiClient, ApiError } from '../api/client.js';
import type { Conversation, Coworker, Me, Message } from '../api/client.js';
import { getStoredToken } from '../services/oidc-auth.js';
import {
  V1WsClient,
  type ServerEvent,
  type ConnectionStatus,
  type ApprovalRequestedEvent,
  type ApprovalResolvedEvent,
  type DelegationStartedEvent,
  type DelegationProgressEvent,
  type DelegationToolUseEvent,
  type DelegationCompletedEvent,
} from '../ws/v1_client.js';
import { beautifyToolName } from '../utils/tool-name.js';
import './approval-card.js';
import './child-agent-chip.js';
import type { ApprovalDecisionDetail } from './approval-card.js';
import {
  type ApprovalCard,
  applyResolved,
  cardsFromConversation,
  upsertRequested,
} from './approval-store.js';

interface AgentStatusState {
  /** Mirrored from the legacy status frame so the progress line keeps
   *  rendering even though the new protocol does not carry the same
   *  tool-use detail (yet). The v1 stream only surfaces token / done /
   *  error frames; future granularity lands when the orchestrator
   *  emits richer events. */
  label: string;
}

/** Frontdesk v1.5 delegation sub-chip state (docs §1.5).
 *
 *  Keyed by `child_conv_id` from the orchestrator so two concurrent
 *  delegations render as two distinct chips. The whole map is cleared
 *  whenever the parent run lands terminal (completed / error / stop /
 *  cancel) — defensive: a dropped `event.delegation.completed` frame
 *  must not strand a chip on screen forever.
 *
 *  `startedAt` is per-chip (epoch ms) so the elapsed-seconds tail in
 *  the rendered line ticks independently for each delegation. */
interface ChildChipState {
  delegationId: string;
  targetName: string;
  targetFolder: string;
  contextMode: string;
  /** Status / tool line WITHOUT the elapsed tail; the renderer appends
   *  the `(Ns)` suffix from `startedAt` so the 1Hz ticker advances it
   *  without rewriting state. */
  baseLine: string;
  startedAt: number;
}

export interface ChatMessage {
  role: 'user' | 'assistant' | 'safety';
  content: string;
  streaming?: boolean;
  safetyStage?: string;
  safetyRuleId?: string;
  /** Frontdesk v1.2: when the orchestrator fans a delegation child's
   *  reply up to the parent conversation it prepends `[via <target>] `
   *  so the user knows the message came from a specialist they don't
   *  normally see. `extractViaPrefix` splits the marker off the body so
   *  the renderer can show it as a small badge next to "Assistant"
   *  rather than as literal text in the bubble. */
  viaTargetName?: string;
  /** Ordering key (epoch ms) for chronological interleave with approval cards.
   *  Stamped with the wall clock when the bubble first gets *content* (live), or
   *  parsed from the server `timestamp` on reload. An empty streaming
   *  placeholder is stamped at creation and re-stamped on its first token, so a
   *  post-approval confirmation sorts after the approval card that gated it
   *  rather than before (the placeholder is born before the card, but its
   *  content arrives after). Mirrors `ApprovalCard.orderTs`. */
  timestamp: number;
}

type RunState = 'idle' | 'running' | 'stopping' | 'cancelling';

/** Match a leading `[via Trading Desk] body...` marker — captures the
 *  target name and the body separately. The name allows letters,
 *  digits, spaces, dashes, underscores, dots and slashes (typical
 *  `coworker.name` characters), bounded to 80 chars; the closing
 *  bracket terminates it. Anchored at start-of-string so it only fires
 *  on the leading marker, never on bracketed text mid-body. */
const VIA_PREFIX_RE = /^\[via ([^\]]{1,80})\]\s+/;

/** Split a leading `[via <name>] ` marker off an assistant message.
 *  Returns the parsed `viaTargetName` (when present) plus the body with
 *  the marker stripped. Exported for the unit test. */
export function extractViaPrefix(
  text: string,
): { viaTargetName?: string; content: string } {
  const m = text.match(VIA_PREFIX_RE);
  if (!m) return { content: text };
  return { viaTargetName: m[1], content: text.slice(m[0].length) };
}

@customElement('rm-chat-panel')
export class ChatPanel extends LitElement {
  // v1 client: streaming + Cancel
  private v1: V1WsClient | null = null;
  private v1Unsubscribers: Array<() => void> = [];
  private tokenRefreshHandler?: (e: Event) => void;
  private readonly api = getApiClient();

  @state() messages: ChatMessage[] = [];
  @state() connected = false;
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
  // In-flight HITL approval cards for the active conversation. Driven by
  // `event.approval.requested` / `event.approval.resolved` and refreshed
  // from REST on (re)connect (docs/21-hitl-approval-plan.md §10 S5).
  @state() private approvals: ApprovalCard[] = [];
  // Frontdesk v1.5: live delegation sub-chips, keyed by child_conv_id.
  // Mounted on event.delegation.started, updated on progress/tool_use,
  // unmounted on completed — and cleared wholesale when the parent run
  // lands terminal so a dropped completed frame can't strand a chip.
  @state() private activeChildChips: Map<string, ChildChipState> = new Map();
  // 1Hz ticker that re-renders the chips so their elapsed-seconds tail
  // advances. Started when the first chip mounts; cleared when the last
  // one unmounts (or on any terminal run transition).
  private durationTickTimer: ReturnType<typeof setInterval> | null = null;
  // request_ids whose decision was just sent and is awaiting the resolved
  // echo — disables the card buttons so a double-tap can't fire twice.
  private approvalInflight = new Set<string>();
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
    this.clearDurationTick();
    this.activeChildChips = new Map();
    this.teardownV1();
    if (this.tokenRefreshHandler) {
      window.removeEventListener('rm-token-refreshed', this.tokenRefreshHandler);
    }
  }

  private async bootstrap(): Promise<void> {
    // Load identity early so the welcome state can greet the user by
    // name. A failure here just falls back to a generic greeting.
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
        .map((m) => {
          const isAssistant = m.role === 'assistant';
          // Only assistant rows carry the `[via <name>]` delegation
          // marker; a user message that happens to start with `[via …]`
          // is the user's own text and must render verbatim.
          const { viaTargetName, content } = isAssistant
            ? extractViaPrefix(m.content)
            : { viaTargetName: undefined, content: m.content };
          return {
            role: isAssistant ? ('assistant' as const) : ('user' as const),
            content,
            viaTargetName,
            // Server timestamp is the ordering key on reload so persisted
            // messages interleave correctly with the conversation's resolved
            // approval cards (both server-clock). Falls back to 0 for the rare
            // row with no timestamp, which keeps it at the very top.
            timestamp: m.timestamp ? Date.parse(m.timestamp) : 0,
          };
        });
    } catch (err) {
      console.warn('listMessages failed', err);
      this.messages = [];
    }
  }

  private async openConversation(conversationId: string): Promise<void> {
    if (!this.activeCoworkerId) return;
    this.teardownV1();
    this.activeConversationId = conversationId;
    this.runState = 'idle';
    this.runTerminal = false;
    this.activeRunId = null;
    // Delegation chips are run-scoped; a conversation switch invalidates them.
    this.clearDurationTick();
    this.activeChildChips = new Map();
    // Approval cards are conversation-scoped; drop the previous chat's.
    this.approvals = [];
    this.approvalInflight.clear();

    // v1 client owns streaming / cancel
    this.v1 = new V1WsClient({
      conversationId,
      getToken: getStoredToken,
    });
    this.v1Unsubscribers.push(
      this.v1.onEvent('*', (e) => this.handleV1Event(e)),
      this.v1.onStatus((s) => this.handleV1Status(s)),
    );
    void this.v1.connect();

    await this.loadMessages(conversationId);
    // Re-render the conversation's full approval record (pending + resolved)
    // inline. Without this, a reload showed messages only — resolved ✅/❌
    // cards vanished — and a fresh load had no cards until the first live push.
    await this.refreshApprovals(conversationId);
  }

  private teardownV1(): void {
    for (const off of this.v1Unsubscribers) off();
    this.v1Unsubscribers = [];
    this.v1?.disconnect();
    this.v1 = null;
  }

  private handleV1Status(s: ConnectionStatus): void {
    const wasConnected = this.connected;
    this.connected = s === 'open';
    // On (re)connect, re-render approval cards from the authoritative DB rows —
    // the live `event.approval.requested`/`resolved` pushes are fire-and-forget,
    // so a card raised (or decided) while the socket was down is only
    // recoverable via this read (§10 S5).
    if (this.connected && !wasConnected) {
      void this.refreshApprovals(this.activeConversationId);
    }
  }

  /** Rebuild the card list from the conversation's full approval record
   *  (pending + resolved, oldest-first). The server is the source of truth for
   *  status / timestamps; we preserve only the client-side rejection `note`
   *  (never persisted) by carrying it over from the matching in-memory card. */
  private async refreshApprovals(conversationId: string | null): Promise<void> {
    if (!conversationId) return;
    try {
      const rows = await this.api.listConversationApprovals(conversationId);
      // Guard against a conversation switch mid-flight.
      if (this.activeConversationId !== conversationId) return;
      const localNotes = new Map(
        this.approvals
          .filter((c) => c.note)
          .map((c) => [c.requestId, c.note] as const),
      );
      this.approvals = cardsFromConversation(rows).map((c) =>
        localNotes.has(c.requestId)
          ? { ...c, note: localNotes.get(c.requestId) ?? null }
          : c,
      );
    } catch (err) {
      console.warn('listConversationApprovals failed', err);
    }
  }

  /** Relay a card tap to the orchestrator. The frame carries only the
   *  request id + verb; the approver identity is stamped server-side from
   *  the verified WS ticket (IDOR guard). We mark the card busy until the
   *  `event.approval.resolved` echo lands so a double-tap can't double-send. */
  private handleApprovalDecision(detail: ApprovalDecisionDetail): void {
    if (!this.v1) return;
    if (this.approvalInflight.has(detail.requestId)) return;
    this.approvalInflight.add(detail.requestId);
    // Stash the rejection note locally so the resolved card can echo it back
    // ("YOUR REASON", §3.6) — the note never returns on the resolved event.
    if (detail.decision === 'reject' && detail.note) {
      const note = detail.note;
      this.approvals = this.approvals.map((c) =>
        c.requestId === detail.requestId ? { ...c, note } : c,
      );
    }
    this.requestUpdate();
    this.v1.sendApprovalDecision(detail.requestId, detail.decision, detail.note);
  }

  /** Bubble a tenant-scope "something changed in approvals" signal so the
   *  top-bar inbox (which keeps its own cross-conversation store) can
   *  re-pull. `composed` lets it cross the shadow boundary if a future
   *  host shadow-roots the panel; today both live in light DOM. */
  private emitApprovalActivity(): void {
    this.dispatchEvent(
      new CustomEvent('approval-activity', { bubbles: true, composed: true }),
    );
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
            // Provisional stamp; re-stamped when the first token lands so a
            // post-approval confirmation sorts after its gating card.
            { role: 'assistant', content: '', streaming: true, timestamp: Date.now() },
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
            {
              ...last,
              content: last.content + delta,
              // First token into an empty placeholder: re-stamp to now so a
              // confirmation gated by an approval sorts after that card.
              timestamp: last.content ? last.timestamp : Date.now(),
            },
          ];
        } else {
          this.messages = [
            ...this.messages,
            { role: 'assistant', content: delta, streaming: true, timestamp: Date.now() },
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
        this.clearChildChips();
        if (this.activeCoworkerId) {
          void this.refreshConversations(this.activeCoworkerId);
        }
        break;
      }
      case 'event.run.progress': {
        // Per-turn progress label (tool_use / running / queued /
        // container_starting). The orchestrator emits these on
        // ``web.stream.* type=status``; legacy /ws/chat forwarded
        // them, the v1 cutover dropped the branch, so until this
        // handler landed the SPA stopped showing "Calling Read…"
        // and similar phase indicators between event.run.started
        // and the first event.run.token.
        //
        // Scope to the active run so a redelivered stale frame
        // doesn't briefly flash a phase that already passed.
        const progressRunId =
          typeof (e as { run_id?: unknown }).run_id === 'string'
            ? (e as { run_id: string }).run_id
            : null;
        if (progressRunId && progressRunId === this.activeRunId) {
          this.agentStatus = { label: this.formatProgressLabel(e) };
        }
        break;
      }
      case 'event.message.appended': {
        // Out-of-band agent push (today: scheduled-task reminder).
        // Append as a new assistant bubble, mirroring how messages
        // fetched from GET /messages on reload are rendered. Does
        // NOT touch run state — these arrive outside any user run.
        const content =
          typeof (e as { content?: unknown }).content === 'string'
            ? (e as { content: string }).content
            : '';
        // The side-channel push carries its own server timestamp; honour it so
        // an out-of-band reminder lands in chronological position rather than
        // always at "now" (matters on reload, where it sits among server-
        // stamped history).
        const appendedAt = (e as { timestamp?: unknown }).timestamp;
        const ts =
          typeof appendedAt === 'string' && appendedAt
            ? Date.parse(appendedAt)
            : Date.now();
        if (content) {
          // Strip a leading `[via <target>]` delegation marker into a
          // badge rather than rendering it as literal text.
          const { viaTargetName, content: body } = extractViaPrefix(content);
          this.messages = [
            ...this.messages,
            { role: 'assistant', content: body, viaTargetName, timestamp: ts },
          ];
        }
        break;
      }
      case 'event.approval.requested': {
        // A blocked MCP tool call needs a human ✅/❌. Out-of-band, like
        // event.message.appended — does NOT touch run state.
        this.approvals = upsertRequested(
          this.approvals,
          e as ApprovalRequestedEvent,
        );
        // Nudge the cross-conversation inbox to re-pull (§4.8 trigger C).
        this.emitApprovalActivity();
        break;
      }
      case 'event.approval.resolved': {
        const ev = e as ApprovalResolvedEvent;
        this.approvals = applyResolved(this.approvals, ev);
        this.approvalInflight.delete(ev.request_id);
        this.emitApprovalActivity();
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
            timestamp: Date.now(),
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
              {
                ...last,
                content: `**Error:** ${message}`,
                streaming: false,
                timestamp: Date.now(),
              },
            ];
          } else {
            this.messages = [
              ...this.messages,
              { role: 'assistant', content: `**Error:** ${message}`, timestamp: Date.now() },
            ];
          }
        }
        this.runState = 'idle';
        this.runTerminal = true;
        this.agentStatus = null;
        this.clearStoppingTimer();
        this.clearCancellingTimer();
        this.clearRunningWatchdog();
        this.clearChildChips();
        break;
      }
      case 'event.delegation.started':
      case 'event.delegation.progress':
      case 'event.delegation.tool_use':
      case 'event.delegation.completed':
        this.handleDelegationEvent(e);
        break;
      default:
        // PR23 removed the `event.run.requires_reauth` case — the
        // backend never emitted it (the user-mode MCP path that would
        // produce it remains gated on the OIDC branch). The reauth
        // banner is still listenable via window.__forceReauth for
        // dev/QA and will be re-wired when the engine starts emitting
        // the event. Forward-compat: unknown event types are ignored.
        break;
    }
  }

  /** Map an ``event.run.progress`` payload to the human-readable
   *  string shown in the progress line. Unknown statuses fall back
   *  to a "Working…" label rather than the raw kind so a new orch
   *  progress type doesn't surface as e.g. ``compaction_started``
   *  to end users. Keeping the map here (not in the protocol) lets
   *  copy iterate without bumping the wire schema. */
  private formatProgressLabel(e: ServerEvent): string {
    const ev = e as {
      status?: unknown;
      tool?: unknown;
    };
    const status = typeof ev.status === 'string' ? ev.status : '';
    const tool = typeof ev.tool === 'string' && ev.tool ? ev.tool : null;
    switch (status) {
      case 'running':
        return 'Thinking…';
      case 'tool_use':
        return tool ? `Calling ${tool}…` : 'Calling tool…';
      case 'container_starting':
        return 'Starting container…';
      case 'queued':
        return 'Queued…';
      default:
        return 'Working…';
    }
  }

  // --- Frontdesk v1.5 delegation sub-chips ---------------------------------

  /** Apply one `event.delegation.*` frame to the chip map. Narrowed on
   *  `e.type` so each branch sees its fully-typed member shape — no
   *  `any`, the generated union is the source of truth for field names.
   *
   *  - started: mount a chip keyed by child_conv_id (idempotent — a
   *    redelivered started keeps the original startedAt so the elapsed
   *    tail doesn't reset).
   *  - progress: replace the status line (ignored if no chip — a frame
   *    racing ahead of started shouldn't conjure a phantom chip).
   *  - tool_use: replace the line with the beautified tool name + a
   *    truncated input preview.
   *  - completed: unmount the chip. */
  private handleDelegationEvent(
    e:
      | DelegationStartedEvent
      | DelegationProgressEvent
      | DelegationToolUseEvent
      | DelegationCompletedEvent,
  ): void {
    // A redelivered frame from a previous run must not resurrect a chip
    // for the turn the user already moved past. Once we have an active
    // run, scope chip mutations to it. (Before the first run.started —
    // a delegation can't legitimately precede its parent run — we let
    // it through so tests / edge orderings still mount.)
    if (this.activeRunId && e.run_id !== this.activeRunId) return;

    switch (e.type) {
      case 'event.delegation.started': {
        const existing = this.activeChildChips.get(e.child_conv_id);
        const next = new Map(this.activeChildChips);
        next.set(e.child_conv_id, {
          delegationId: e.delegation_id,
          targetName: e.target_name,
          targetFolder: e.target_folder,
          contextMode: e.context_mode ?? '',
          baseLine: this.mapDelegationStatus(e.initial_status ?? 'queued'),
          // Preserve the original startedAt across a duplicate started so
          // the elapsed counter is monotonic for the delegation's life.
          startedAt: existing?.startedAt ?? Date.now(),
        });
        this.activeChildChips = next;
        this.ensureDurationTick();
        break;
      }
      case 'event.delegation.progress': {
        const existing = this.activeChildChips.get(e.child_conv_id);
        if (!existing) break;
        const next = new Map(this.activeChildChips);
        next.set(e.child_conv_id, {
          ...existing,
          baseLine: this.mapDelegationStatus(e.status),
        });
        this.activeChildChips = next;
        break;
      }
      case 'event.delegation.tool_use': {
        const existing = this.activeChildChips.get(e.child_conv_id);
        if (!existing) break;
        const pretty = beautifyToolName(e.tool_name) || 'Using tool…';
        const preview = e.tool_input_preview ?? '';
        const next = new Map(this.activeChildChips);
        next.set(e.child_conv_id, {
          ...existing,
          baseLine: preview ? `${pretty} · ${preview}` : pretty,
        });
        this.activeChildChips = next;
        break;
      }
      case 'event.delegation.completed': {
        if (!this.activeChildChips.has(e.child_conv_id)) break;
        const next = new Map(this.activeChildChips);
        next.delete(e.child_conv_id);
        this.activeChildChips = next;
        if (next.size === 0) this.clearDurationTick();
        break;
      }
    }
  }

  /** Map a delegation phase string to a human-readable line. Mirrors the
   *  parent run's `formatProgressLabel`; an unknown phase falls back to
   *  the raw string (it carries some signal) rather than a bland
   *  "Working…" so a new orchestrator kind is still legible. */
  private mapDelegationStatus(status: string): string {
    switch (status) {
      case 'queued':
        return 'Queued…';
      case 'container_starting':
        return 'Starting…';
      case 'running':
        return 'Thinking…';
      case 'tool_use':
        // Normally arrives via the tool_use frame with details; this is
        // the defensive fallback when only a generic status lands.
        return 'Using tool…';
      default:
        return status;
    }
  }

  /** Render a chip's line with the elapsed-seconds tail. Suppressed for
   *  the first 2s so a fast delegation doesn't flash "(0s)". */
  private childChipLine(chip: ChildChipState): string {
    const elapsedSec = Math.floor((Date.now() - chip.startedAt) / 1000);
    return elapsedSec >= 2 ? `${chip.baseLine} (${elapsedSec}s)` : chip.baseLine;
  }

  /** Drop every chip and stop the ticker — called on any terminal run
   *  transition so a missed `completed` frame can't strand a chip. */
  private clearChildChips(): void {
    this.clearDurationTick();
    if (this.activeChildChips.size > 0) this.activeChildChips = new Map();
  }

  /** Start the 1Hz re-render ticker if it isn't already running. The
   *  ticker mutates no state — it calls `requestUpdate()` so the render
   *  reads `Date.now()` fresh and the elapsed tail advances. Stops
   *  itself once no chips remain. */
  private ensureDurationTick(): void {
    if (this.durationTickTimer !== null) return;
    this.durationTickTimer = setInterval(() => {
      if (this.activeChildChips.size > 0) {
        this.requestUpdate();
      } else {
        this.clearDurationTick();
      }
    }, 1000);
  }

  private clearDurationTick(): void {
    if (this.durationTickTimer !== null) {
      clearInterval(this.durationTickTimer);
      this.durationTickTimer = null;
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
        this.clearChildChips();
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

  /** Stop = soft interrupt of the current turn via the v1
   *  `request.stop` frame. Container stays alive; the next message
   *  is immediate. Design §4.1 — do NOT redirect this to Cancel. */
  private handleStop(): void {
    if (this.runState !== 'running') return;
    if (!this.v1) return;
    this.runState = 'stopping';
    this.v1.stop();
    this.clearStoppingTimer();
    this.stoppingTimer = setTimeout(() => {
      if (this.runState === 'stopping') {
        // The orchestrator interrupts the container but doesn't
        // currently emit an explicit `event.run.stopped` ack —
        // event.run.error / completed land instead when the abort
        // settles. Best-effort recover to idle so the input isn't
        // trapped if neither frame arrives.
        this.runState = 'idle';
        this.runTerminal = true;
        this.agentStatus = null;
        this.finalizeStreamingBubble();
        this.clearChildChips();
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
        this.clearChildChips();
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
        this.clearChildChips();
        this.clearCancellingTimer();
      }
    } catch (err) {
      console.warn('cancelRun failed', err);
      if (err instanceof ApiError) {
        this.messages = [
          ...this.messages,
          {
            role: 'assistant',
            content: `**Error:** cancel failed (${err.message})`,
            timestamp: Date.now(),
          },
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
          {
            role: 'assistant',
            content: '**Error:** could not create conversation',
            timestamp: Date.now(),
          },
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

    this.messages = [...this.messages, { role: 'user', content: text, timestamp: Date.now() }];
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
    this.clearChildChips();
    this.updateUrl(conversationId);
    await this.openConversation(conversationId);
  }

  private handleNewChat(): void {
    this.teardownV1();
    this.activeConversationId = null;
    this.pendingNewChat = true;
    this.messages = [];
    this.agentStatus = null;
    this.runState = 'idle';
    this.runTerminal = false;
    this.activeRunId = null;
    this.clearStoppingTimer();
    this.clearCancellingTimer();
    this.clearChildChips();
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
              <rm-message-list
                .messages=${this.messages}
                .approvals=${this.approvals}
                .approvalBusy=${this.approvalInflight}
                .coworkerName=${this.activeCoworkerName}
                @approval-decision=${(e: CustomEvent<ApprovalDecisionDetail>) =>
                  this.handleApprovalDecision(e.detail)}
              ></rm-message-list>
              ${this.messages.length > 0 || this.approvals.length > 0
                ? html`<div class="h-8"></div>`
                : ''}
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

          <!-- Frontdesk v1.5 delegation sub-chips: one per active
               delegation, nested under the parent agent's status bar.
               Ephemeral — unmount on event.delegation.completed (or any
               terminal run transition). Renders nothing when idle. -->
          ${this.activeChildChips.size > 0 ? html`
            <div class="shrink-0 px-4">
              <div class="max-w-[720px] mx-auto w-full">
                ${Array.from(this.activeChildChips.entries()).map(
                  ([id, chip]) => html`
                    <rm-child-agent-chip
                      .childConversationId=${id}
                      .delegationId=${chip.delegationId}
                      .targetName=${chip.targetName}
                      .targetFolder=${chip.targetFolder}
                      .contextMode=${chip.contextMode}
                      .currentLine=${this.childChipLine(chip)}
                    ></rm-child-agent-chip>
                  `,
                )}
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
