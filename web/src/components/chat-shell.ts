// <rm-chat-shell> — v2 outer chrome for the chat experience.
//
// Layout (per docs/webui-ui-redesign-v2-prototype.html):
//
//   ┌──────────────┬──────────────────────────────────────────┐
//   │ brand        │ topbar: 3 icons + tenant pill            │
//   │ coworker ▾   ├──────────────────────────────────────────┤
//   │ + new chat   │                                          │
//   │ search…      │   <rm-chat-panel>                        │
//   │ history list │   (v1.1 component, slotted unchanged)    │
//   │ user pill    │                                          │
//   └──────────────┴──────────────────────────────────────────┘
//
// Slot contract: the shell slots the EXACT v1.1 `<rm-chat-panel>`
// element with no internal modifications. Two consequences:
//   1. The chat-panel still has its own `<rm-sidebar>` child. To
//      avoid two side-by-side sidebars we set the localStorage
//      flag that chat-panel reads in its constructor — its inner
//      sidebar mounts collapsed. User can still toggle it via the
//      panel's hamburger; that is an accepted v2-A edge.
//   2. Coworker / conversation switching from the shell is done by
//      navigating to a new URL (`location.href`). chat-panel reads
//      `?agent_id` + `?chat_id` from the search string only in its
//      constructor, so a full reload is the simplest way to swap
//      without touching it. The v1.1 coworkers-page uses exactly
//      the same pattern (`location.href = '?agent_id=…#/'`).
//
// State scope:
//   - shell-owned: coworker list, conversation list (fetched once),
//     menu/popover open flags, current user (`me`)
//   - URL-owned: active coworker id, active conversation id
//   - panel-owned: messages, runs, approvals, WebSocket lifecycle
//
// Approvals badge is hard-coded to 0 here; v2-C wires it to the
// `/api/v1/approvals` count so we do not ship a placeholder + real
// implementation side by side.

import { LitElement, html, nothing, type TemplateResult } from 'lit';
import { customElement, state } from 'lit/decorators.js';

import {
  ApiError,
  getApiClient,
  type ApprovalRequest,
  type Conversation,
  type Coworker,
  type Me,
} from '../api/client.js';
import { UserApprovalsClient, type UserApprovalsStatus } from '../ws/user_approvals_client.js';
import './chat-panel.js';
import './reauth-banner.js';
import './approvals-popover.js';
import {
  iconActivity,
  iconApprovals,
  iconChevronDown,
  iconLogout,
  iconPlus,
  iconSearch,
  iconSettings,
} from './icons.js';

interface ConversationGroup {
  label: string;
  items: Conversation[];
}

// Coworker avatar palette. The prototype assigns palette swatches by
// role; we approximate by hashing the coworker name. The exact dot
// colour is decorative — the avatar's letter is the real identifier.
const AVATAR_COLOURS = [
  '#C2613F', // terracotta (matches accent)
  '#3F7DC2', // blue
  '#2F7D5B', // green
  '#8A5BC2', // purple
  '#C29A3F', // mustard
  '#C23F77', // pink
];

function colourForCoworker(coworker: Coworker | null): string {
  if (!coworker) return AVATAR_COLOURS[0];
  let hash = 0;
  for (let i = 0; i < coworker.id.length; i += 1) {
    hash = (hash * 31 + coworker.id.charCodeAt(i)) | 0;
  }
  return AVATAR_COLOURS[Math.abs(hash) % AVATAR_COLOURS.length];
}

function initialsFor(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return '?';
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}

/**
 * Group conversations into Today / Yesterday / Earlier buckets
 * based on the user's local timezone. We use `created_at` because
 * the Conversation schema does not expose `updated_at`; v3 can swap
 * in updated_at when the API surfaces it.
 *
 * Exported for unit testing.
 */
export function groupConversations(
  conversations: readonly Conversation[],
  now: Date = new Date(),
): ConversationGroup[] {
  const startOfToday = new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate(),
  );
  const startOfYesterday = new Date(startOfToday.getTime() - 86_400_000);
  const today: Conversation[] = [];
  const yesterday: Conversation[] = [];
  const earlier: Conversation[] = [];
  for (const c of conversations) {
    const created = new Date(c.created_at);
    if (created >= startOfToday) today.push(c);
    else if (created >= startOfYesterday) yesterday.push(c);
    else earlier.push(c);
  }
  return [
    { label: 'Today', items: today },
    { label: 'Yesterday', items: yesterday },
    { label: 'Earlier', items: earlier },
  ].filter((g) => g.items.length > 0);
}

@customElement('rm-chat-shell')
export class RmChatShell extends LitElement {
  @state() private coworkers: Coworker[] = [];
  @state() private conversations: Conversation[] = [];
  @state() private me: Me | null = null;
  @state() private activeCoworkerId: string | null = null;
  @state() private activeConversationId: string | null = null;
  /** conversation_id → preview text derived from the first user
   *  message. Computed lazily on sidebar refresh; the Conversation
   *  schema's optional `name` field still wins when present (a user
   *  who explicitly renames a chat shouldn't see the preview clobber
   *  their label). */
  @state() private convPreviews = new Map<string, string>();
  /** Mirror of `<rm-message-editor>.connected`, which itself mirrors
   *  the legacy AgentClient socket state in chat-panel. We absorb
   *  the `agent-connection` event so the tenant pill can render the
   *  green/red dot the way chat-panel's top bar used to. */
  @state() private agentConnected = false;
  /** Flipped to true once bootstrap finishes resolving (or creating)
   *  a chat_id for the active coworker. chat-panel is mounted only
   *  AFTER this flips so it reads the post-resolution URL params in
   *  its constructor and lands on an immediately-connected state
   *  instead of an empty "Disconnected" page. */
  @state() private bootstrapped = false;
  /** Which popover, if any, is open. Only one at a time to keep
   *  keyboard handling simple. */
  @state() private openMenu: '' | 'coworker' | 'user' | 'approvals' = '';
  /** Pending approvals where the signed-in user is an approver. Owned
   *  here (not in the popover) because the badge needs the same count
   *  whether the popover is open or not. Sorted newest-first. */
  @state() private pendingApprovals: ApprovalRequest[] = [];
  @state() private approvalsLoading = true;
  /** Most recent UserApprovalsClient connection status — surfaced to
   *  the popover so it can render a "stale" hint when WS is down. */
  @state() private approvalsConn: UserApprovalsStatus = 'idle';

  private readonly api = getApiClient();
  private approvalsClient: UserApprovalsClient | null = null;
  /** Unsubscribe handles from the approvals client. Set on mount,
   *  cleared on unmount so the WS doesn't leak past the shell. */
  private approvalsUnsubs: Array<() => void> = [];

  protected override createRenderRoot() {
    // Light DOM so the chat-panel's contenteditable composer + the
    // <rm-reauth-banner> custom event bus keep working without
    // shadow-boundary plumbing.
    return this;
  }

  /** Test seam — pass a fake UserApprovalsClient in unit tests so we
   *  don't have to stub `WebSocket` + `fetch` at the same time. The
   *  production code path uses the default constructor. */
  setApprovalsClient(client: UserApprovalsClient): void {
    if (this.approvalsClient) {
      this.teardownApprovals();
    }
    this.approvalsClient = client;
    this.wireApprovalsClient();
  }

  override connectedCallback() {
    super.connectedCallback();
    // Inline styles ship before the first render's <style> block has
    // a chance to apply, and override the stylesheet rule (higher
    // specificity). Set display:flex inline so the banner +
    // sidebar/main layout is correct on first paint.
    this.style.display = 'flex';
    this.style.flexDirection = 'column';
    this.style.height = '100%';
    // Force chat-panel's internal sidebar collapsed so we render
    // ONE sidebar (the v2 shell's), not two. chat-panel reads this
    // in its constructor, so we must set it before chat-panel
    // mounts inside the slot. The CSS rules below also hide the
    // inner sidebar visually, but the localStorage flag remains as
    // belt-and-braces so a user toggling at the chat-panel level
    // doesn't suddenly re-expand a hidden element. v3 will rip out
    // the inner sidebar entirely and drop this workaround.
    localStorage.setItem('rm-sidebar-collapsed', 'true');

    const params = new URLSearchParams(location.search);
    this.activeCoworkerId = params.get('agent_id');
    this.activeConversationId = params.get('chat_id');
    void this.bootstrap();
    document.addEventListener('click', this.onDocumentClick, true);
    this.addEventListener('agent-connection', this.onAgentConnection);
    // Tests inject the client via setApprovalsClient() BEFORE attach;
    // production code path lazily mints one here so we don't churn the
    // unit-test stub.
    if (!this.approvalsClient) {
      this.approvalsClient = new UserApprovalsClient({
        getToken: () =>
          sessionStorage.getItem('rm_id_token') ??
          localStorage.getItem('rm_id_token'),
      });
      this.wireApprovalsClient();
    }
  }

  override disconnectedCallback() {
    super.disconnectedCallback();
    document.removeEventListener('click', this.onDocumentClick, true);
    this.removeEventListener('agent-connection', this.onAgentConnection);
    this.teardownApprovals();
  }

  private onAgentConnection = (e: Event) => {
    const detail = (e as CustomEvent<{ connected: boolean }>).detail;
    if (detail && typeof detail.connected === 'boolean') {
      this.agentConnected = detail.connected;
    }
  };

  private wireApprovalsClient(): void {
    const c = this.approvalsClient;
    if (!c) return;
    this.approvalsUnsubs.push(
      c.onStatus((s) => {
        this.approvalsConn = s;
      }),
    );
    this.approvalsUnsubs.push(
      c.onRequired((e) => {
        // The WS event lacks the full ApprovalRequest shape — when a
        // new approval lands, refetch the list so we keep the same
        // schema everywhere instead of half-populating rows from the
        // event payload.
        void this.refreshPendingApprovals();
      }),
    );
    this.approvalsUnsubs.push(
      c.onResolved((e) => {
        const id = e.approval_id;
        if (!id) return;
        const next = this.pendingApprovals.filter((r) => r.id !== id);
        // Only refetch when something actually moved — saves the round
        // trip for events targeting approvals we never displayed.
        if (next.length !== this.pendingApprovals.length) {
          this.pendingApprovals = next;
        }
      }),
    );
    void c.start();
    // REST fetch establishes the initial set; the WS only carries
    // deltas after the socket opens. Without this we'd render an
    // empty list until the first .required event fires.
    void this.refreshPendingApprovals();
  }

  private teardownApprovals(): void {
    for (const off of this.approvalsUnsubs) off();
    this.approvalsUnsubs = [];
    this.approvalsClient?.stop();
    this.approvalsClient = null;
  }

  private async refreshPendingApprovals(): Promise<void> {
    try {
      const rows = await this.api.listApprovals({
        scope: 'mine',
        status: 'pending',
      });
      // Newest-first so the popover shows the most recent landings
      // at the top. requested_at is ISO-8601 lexicographic.
      rows.sort((a, b) => (a.requested_at < b.requested_at ? 1 : -1));
      this.pendingApprovals = rows;
    } catch (err) {
      // Don't blow away an existing list on a transient failure — log
      // and let the next event-triggered refresh recover.
      console.warn('chat-shell: refreshPendingApprovals failed', err);
    } finally {
      this.approvalsLoading = false;
    }
  }

  private async bootstrap(): Promise<void> {
    // All three are independent — fire in parallel so first paint
    // does not wait on the slowest endpoint.
    const [coworkersResult, meResult] = await Promise.allSettled([
      this.api.listCoworkers(),
      this.api.getMe(),
    ]);
    if (coworkersResult.status === 'fulfilled') {
      this.coworkers = coworkersResult.value;
      // If the URL did not pin a coworker, default to the first one
      // (matches the prototype's "always show a chat" intent).
      if (!this.activeCoworkerId && this.coworkers.length > 0) {
        this.activeCoworkerId = this.coworkers[0].id;
      }
    } else if (coworkersResult.reason instanceof ApiError) {
      console.warn('chat-shell: listCoworkers failed', coworkersResult.reason);
    }
    if (meResult.status === 'fulfilled') this.me = meResult.value;
    if (this.activeCoworkerId) {
      await this.refreshConversations(this.activeCoworkerId);
      // Land on a CONNECTED chat by default: if the URL doesn't pin
      // a chat_id, pick the coworker's most recent conversation (or
      // POST a fresh one when the coworker has none). Without this
      // step the user lands on chat-panel's empty state showing
      // "Disconnected" until they click into a history row.
      if (!this.activeConversationId) {
        const chatId = await this.resolveDefaultChatId(this.activeCoworkerId);
        if (chatId) {
          this.activeConversationId = chatId;
          // history.replaceState (NOT a full assign) — chat-panel
          // reads URL params in its constructor; we update the URL
          // *before* chat-panel mounts (gated on `bootstrapped`) so
          // it picks up chat_id on its first paint. replaceState
          // keeps the back button from bouncing through the dead
          // intermediate URL. Wrapped because happy-dom + some
          // browser configurations refuse cross-path replaceState;
          // the missing URL update is recoverable (chat-panel just
          // won't see chat_id and renders Disconnected — strictly
          // worse than the happy path, not broken).
          try {
            const next = this.buildHref(this.activeCoworkerId, chatId);
            history.replaceState(null, '', next);
          } catch (err) {
            console.warn('chat-shell: replaceState rejected', err);
          }
        }
      }
    }
    this.bootstrapped = true;
  }

  /** Find the most recent conversation for `coworkerId`, or POST a
   *  fresh one when the coworker has no history yet. Returns null
   *  only on outright failure (both list AND create rejected) — the
   *  caller renders the empty state in that case.
   *
   *  Sort key: `created_at` ISO-8601 string; lexicographic compare
   *  matches chronological order. The Conversation schema doesn't
   *  expose `updated_at` yet, so "most recent" is "most recently
   *  created" today. */
  private async resolveDefaultChatId(
    coworkerId: string,
  ): Promise<string | null> {
    const existing = [...this.conversations];
    if (existing.length > 0) {
      existing.sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
      return existing[0].id;
    }
    try {
      const fresh = await this.api.createCoworkerConversation(coworkerId);
      // Splice the new row into our local list so the sidebar shows
      // it immediately without a follow-up refetch.
      this.conversations = [fresh, ...this.conversations];
      return fresh.id;
    } catch (err) {
      console.warn('chat-shell: createCoworkerConversation failed', err);
      return null;
    }
  }

  private async refreshConversations(coworkerId: string): Promise<void> {
    try {
      this.conversations =
        await this.api.listCoworkerConversations(coworkerId);
    } catch (err) {
      console.warn('chat-shell: listCoworkerConversations failed', err);
      this.conversations = [];
    }
    // Kick off preview loads. We do not await — the sidebar paints
    // immediately with whatever label is available; each preview
    // streams in and triggers a re-render via the @state Map. A
    // failure for one conversation leaves the row showing the
    // `name` (or "New chat") fallback without dragging the whole
    // sidebar into an error state.
    void this.loadConversationPreviews();
  }

  /** Truncate to a sidebar-friendly width. The visible rail is
   *  ~240px wide; ~48 chars fits one line at the body font size with
   *  some margin. Single ellipsis at the cut point.
   *
   *  Strips leading/trailing whitespace and collapses newlines so a
   *  multi-line first message doesn't expand the row. */
  private static formatPreview(raw: string, max = 48): string {
    const cleaned = raw.replace(/\s+/g, ' ').trim();
    if (cleaned.length <= max) return cleaned;
    return cleaned.slice(0, max - 1).trimEnd() + '…';
  }

  /** Fetch the first user message for each conversation lacking a
   *  preview, in parallel. Conversations the user explicitly named
   *  via `Conversation.name` keep their name; previews are a
   *  fallback for unnamed rows. */
  private async loadConversationPreviews(): Promise<void> {
    const needed = this.conversations.filter(
      (c) => !this.convPreviews.has(c.id),
    );
    if (needed.length === 0) return;
    const results = await Promise.allSettled(
      needed.map((c) => this.api.listMessages(c.id)),
    );
    const next = new Map(this.convPreviews);
    for (let i = 0; i < needed.length; i += 1) {
      const r = results[i];
      if (r.status !== 'fulfilled') continue;
      const firstUser = r.value.find((m) => m.role === 'user');
      const source = firstUser ?? r.value[0];
      if (!source || !source.content) continue;
      next.set(needed[i].id, RmChatShell.formatPreview(source.content));
    }
    this.convPreviews = next;
  }

  // Document-level click closes whichever popover is open. We attach
  // in capture phase so we run BEFORE the button's own toggle handler
  // re-opens what we just closed.
  private onDocumentClick = (e: MouseEvent) => {
    if (!this.openMenu) return;
    const target = e.target as Node | null;
    if (!target) return;
    // The popover button itself toggles via @click — let it through.
    const menuNode = this.querySelector(`[data-menu="${this.openMenu}"]`);
    const triggerNode = this.querySelector(`[data-menu-trigger="${this.openMenu}"]`);
    if (menuNode?.contains(target) || triggerNode?.contains(target)) {
      return;
    }
    this.openMenu = '';
  };

  private get activeCoworker(): Coworker | null {
    return this.coworkers.find((c) => c.id === this.activeCoworkerId) ?? null;
  }

  /** Build the search-string suffix for a navigation, keeping any
   *  unrelated params the chat-panel may rely on. */
  private buildHref(coworkerId: string | null, chatId: string | null): string {
    const params = new URLSearchParams(location.search);
    if (coworkerId) params.set('agent_id', coworkerId);
    else params.delete('agent_id');
    if (chatId) params.set('chat_id', chatId);
    else params.delete('chat_id');
    const qs = params.toString();
    return `${location.pathname}${qs ? '?' + qs : ''}#/`;
  }

  private async navigateCoworker(coworkerId: string): Promise<void> {
    if (coworkerId === this.activeCoworkerId) {
      this.openMenu = '';
      return;
    }
    this.openMenu = '';
    // Resolve a chat_id BEFORE navigating so the next page lands
    // connected. Fall back to a no-chat URL if both listing AND
    // creation fail — the post-reload bootstrap retry will surface
    // any persistent issue rather than us leaving the user stuck.
    let chatId: string | null = null;
    try {
      const convs = await this.api.listCoworkerConversations(coworkerId);
      if (convs.length > 0) {
        const sorted = [...convs].sort((a, b) =>
          a.created_at < b.created_at ? 1 : -1,
        );
        chatId = sorted[0].id;
      } else {
        const fresh = await this.api.createCoworkerConversation(coworkerId);
        chatId = fresh.id;
      }
    } catch (err) {
      console.warn('chat-shell: navigateCoworker resolve failed', err);
    }
    location.href = this.buildHref(coworkerId, chatId);
  }

  private navigateConversation(chatId: string): void {
    if (!this.activeCoworkerId) return;
    if (chatId === this.activeConversationId) return;
    location.href = this.buildHref(this.activeCoworkerId, chatId);
  }

  private startNewChat = async (): Promise<void> => {
    if (!this.activeCoworkerId) return;
    // + New chat is always an explicit "fresh conversation" intent —
    // create the row up front instead of letting chat-panel lazy-
    // create on first send. The user lands on a connected empty
    // composer, not on a Disconnected placeholder.
    try {
      const fresh = await this.api.createCoworkerConversation(
        this.activeCoworkerId,
      );
      location.href = this.buildHref(this.activeCoworkerId, fresh.id);
    } catch (err) {
      console.warn('chat-shell: startNewChat create failed', err);
      // Fall back: navigate without a chat_id; bootstrap on the next
      // page will pick up an existing conv or retry the create.
      location.href = this.buildHref(this.activeCoworkerId, null);
    }
  };

  private openActivity = () => {
    location.hash = '#/activity';
  };

  private toggleApprovals = () => {
    this.openMenu = this.openMenu === 'approvals' ? '' : 'approvals';
  };

  /** A row's decide button posts directly, then `rm-inline-approval`
   *  emits `rm-approval-decided` which bubbles to us. The WS resolved
   *  event will also fire and remove the row, but doing the refresh
   *  here keeps the UI honest when WS is degraded. */
  private onApprovalDecided = () => {
    void this.refreshPendingApprovals();
  };

  /** Popover footer "View all" link wants the popover closed before
   *  the route changes. The popover dispatches `rm-popover-navigate`
   *  so we can clear `openMenu` without poking into its DOM. */
  private onPopoverNavigate = () => {
    this.openMenu = '';
  };

  private openSettings = () => {
    location.hash = '#/manage/coworkers';
  };

  private toggleCoworkerMenu = () => {
    this.openMenu = this.openMenu === 'coworker' ? '' : 'coworker';
  };

  private toggleUserMenu = () => {
    this.openMenu = this.openMenu === 'user' ? '' : 'user';
  };

  private logout = () => {
    sessionStorage.removeItem('rm_id_token');
    localStorage.removeItem('rm_id_token');
    // Hand the auth state machine back the wheel.
    window.dispatchEvent(new CustomEvent('rm-auth-failed'));
  };

  private goToManageCoworkers = () => {
    this.openMenu = '';
    location.hash = '#/manage/coworkers';
  };

  override render(): TemplateResult {
    const active = this.activeCoworker;
    const groups = groupConversations(this.conversations);
    const tenantLabel = this.me?.tenant_id
      ? `${this.me.tenant_id.slice(0, 12)} · prod`
      : 'workspace · prod';
    return html`
      <style>
        /* Scoped via parent attribute selector so these rules only
         * touch the shell and not the v1.1 settings shell or login.
         * The host is flex-column so the reauth banner can sit
         * above a fixed-height layout; the inner .cs-layout owns
         * the grid that splits sidebar + main. */
        rm-chat-shell {
          display: flex;
          flex-direction: column;
          height: 100%;
          min-height: 0;
          background: var(--rm-bg);
          color: var(--rm-ink);
          font-family: var(--rm-font-body);
        }
        rm-chat-shell .cs-layout {
          flex: 1;
          min-height: 0;
          display: grid;
          grid-template-columns: 272px 1fr;
        }
        rm-chat-shell .cs-sidebar {
          background: var(--rm-surface-2);
          border-right: 1px solid var(--rm-border);
          display: flex;
          flex-direction: column;
          min-height: 0;
        }
        rm-chat-shell .cs-brand {
          display: flex;
          align-items: center;
          padding: 18px 16px 12px;
        }
        /* Wordmark — Space Grotesk (var(--rm-font-logo)) with a
         * 2-tone "Role" / "Mesh" split (ink + accent terracotta).
         * Geometric tech-sans idiom (Vercel/Linear); a tighter
         * letter-spacing (-0.02em) sharpens the silhouette at logo
         * scale. */
        rm-chat-shell .cs-brand-wm {
          display: inline-flex;
          align-items: baseline;
          font-family: var(--rm-font-logo);
          font-size: 27px;
          line-height: 1;
          letter-spacing: -0.02em;
          font-weight: 500;
        }
        rm-chat-shell .cs-brand-pri {
          color: var(--rm-ink);
        }
        rm-chat-shell .cs-brand-sec {
          color: var(--rm-accent);
          font-weight: 600;
        }
        rm-chat-shell .coswitch {
          margin: 2px 8px 8px;
          display: flex;
          align-items: center;
          gap: 9px;
          padding: 7px 9px;
          border-radius: var(--rm-r);
          border: none;
          background: none;
          cursor: pointer;
          text-align: left;
          width: calc(100% - 16px);
          color: inherit;
          font-family: inherit;
          transition: 0.13s;
        }
        rm-chat-shell .coswitch:hover { background: var(--rm-surface); }
        rm-chat-shell .cav {
          width: 30px;
          height: 30px;
          border-radius: 8px;
          display: grid;
          place-items: center;
          font-size: 11.5px;
          font-weight: 600;
          color: #fff;
          flex-shrink: 0;
        }
        rm-chat-shell .csw-txt { flex: 1; min-width: 0; }
        rm-chat-shell .csw-txt b {
          display: block;
          font-size: 13.5px;
          font-weight: 600;
        }
        rm-chat-shell .csw-txt span {
          font-size: 11px;
          color: var(--rm-ink-3);
          display: block;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }
        rm-chat-shell .newchat {
          margin: 4px 12px 8px;
          display: flex;
          align-items: center;
          gap: 9px;
          padding: 9px 11px;
          border-radius: var(--rm-r);
          border: 1px solid var(--rm-border-2);
          background: var(--rm-surface);
          font-weight: 500;
          font-size: 13.5px;
          cursor: pointer;
          color: inherit;
          font-family: inherit;
          transition: 0.15s;
        }
        rm-chat-shell .newchat:hover {
          background: var(--rm-bg);
          border-color: var(--rm-ink-3);
        }
        rm-chat-shell .newchat svg { color: var(--rm-accent); }
        rm-chat-shell .cs-search {
          margin: 0 12px 8px;
          display: flex;
          align-items: center;
          gap: 8px;
          padding: 7px 10px;
          border-radius: var(--rm-r);
          color: var(--rm-ink-3);
          font-size: 13px;
          background: none;
          border: none;
          font-family: inherit;
          cursor: pointer;
          text-align: left;
          width: calc(100% - 24px);
        }
        rm-chat-shell .cs-search:hover { background: var(--rm-surface); }
        rm-chat-shell .histscroll {
          flex: 1;
          overflow-y: auto;
          padding: 8px;
        }
        rm-chat-shell .grouplabel {
          font-size: 11px;
          font-weight: 600;
          color: var(--rm-ink-3);
          letter-spacing: 0.04em;
          text-transform: uppercase;
          padding: 8px 8px 5px;
        }
        rm-chat-shell .conv {
          display: flex;
          align-items: center;
          gap: 9px;
          padding: 7px 9px;
          border-radius: 9px;
          font-size: 13.5px;
          color: var(--rm-ink-2);
          cursor: pointer;
          white-space: nowrap;
          overflow: hidden;
          transition: 0.13s;
          position: relative;
          background: none;
          border: none;
          font-family: inherit;
          width: 100%;
          text-align: left;
        }
        rm-chat-shell .conv:hover { background: var(--rm-surface); color: var(--rm-ink); }
        rm-chat-shell .conv.active {
          background: var(--rm-surface);
          color: var(--rm-ink);
          font-weight: 500;
        }
        rm-chat-shell .conv.active::before {
          content: "";
          position: absolute;
          left: 0;
          top: 8px;
          bottom: 8px;
          width: 2.5px;
          border-radius: 2px;
          background: var(--rm-accent);
        }
        rm-chat-shell .conv .t {
          overflow: hidden;
          text-overflow: ellipsis;
        }
        rm-chat-shell .userbar {
          border-top: 1px solid var(--rm-border);
          padding: 9px 10px;
          display: flex;
          align-items: center;
          gap: 9px;
          cursor: pointer;
          background: none;
          border-left: none;
          border-right: none;
          border-bottom: none;
          width: 100%;
          color: inherit;
          font-family: inherit;
          text-align: left;
          transition: 0.13s;
        }
        rm-chat-shell .userbar:hover { background: var(--rm-surface); }
        rm-chat-shell .avatar {
          width: 28px;
          height: 28px;
          border-radius: 50%;
          background: var(--rm-accent-subtle);
          color: var(--rm-accent-2);
          display: grid;
          place-items: center;
          font-size: 12px;
          font-weight: 600;
          flex-shrink: 0;
        }
        rm-chat-shell .userbar .nm { flex: 1; min-width: 0; }
        rm-chat-shell .userbar .nm b {
          display: block;
          font-size: 13.5px;
          font-weight: 500;
        }
        rm-chat-shell .userbar .nm span {
          display: block;
          font-size: 11px;
          color: var(--rm-ink-3);
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }
        rm-chat-shell .cs-main {
          display: flex;
          flex-direction: column;
          min-width: 0;
          /* min-height: 0 is load-bearing — grid items default to
           * min-height:auto, which sizes the cell to its content.
           * Without this, a long chat overflows .cs-main upward into
           * .cs-layout, the entire shell scrolls (taking the sidebar
           * with it), and the composer at the bottom gets pushed off
           * screen. Pin it so chat-panel's internal scroll surface
           * is the only thing that actually scrolls. */
          min-height: 0;
          overflow: hidden;
        }
        rm-chat-shell .cs-tbar {
          height: 52px;
          display: flex;
          align-items: center;
          gap: 6px;
          padding: 0 14px 0 20px;
          border-bottom: 1px solid var(--rm-border);
        }
        rm-chat-shell .cs-tbar .ti {
          font-weight: 500;
          font-size: 14.5px;
          color: var(--rm-ink);
        }
        rm-chat-shell .cs-tbar .with {
          display: flex;
          align-items: center;
          gap: 6px;
          font-size: 12.5px;
          color: var(--rm-ink-3);
        }
        rm-chat-shell .cs-tbar .spacer { flex: 1; }
        rm-chat-shell .cs-iconbtn {
          position: relative;
          display: grid;
          place-items: center;
          width: 32px;
          height: 32px;
          border-radius: 8px;
          background: none;
          border: none;
          color: var(--rm-ink-2);
          cursor: pointer;
          font-family: inherit;
          transition: 0.13s;
        }
        rm-chat-shell .cs-iconbtn:hover {
          background: var(--rm-surface-2);
          color: var(--rm-ink);
        }
        rm-chat-shell .cs-iconbtn .bdg {
          position: absolute;
          top: -3px;
          right: -3px;
          min-width: 16px;
          height: 16px;
          padding: 0 4px;
          border-radius: 99px;
          background: var(--rm-accent);
          color: #fff;
          font-size: 10px;
          font-weight: 600;
          display: grid;
          place-items: center;
        }
        rm-chat-shell .cs-tenant {
          display: flex;
          align-items: center;
          gap: 7px;
          font-size: 12px;
          color: var(--rm-ink-3);
          border: 1px solid var(--rm-border);
          padding: 4px 10px;
          border-radius: 99px;
          margin-left: 6px;
        }
        rm-chat-shell .cs-tenant .grn {
          width: 6px;
          height: 6px;
          border-radius: 50%;
          background: var(--rm-good);
        }
        /* Connection dot doubles as the chat-panel "Connected" /
         * "Disconnected" indicator since v2-C dropped chat-panel's
         * top bar. Red while the legacy AgentClient socket is down. */
        rm-chat-shell .cs-tenant .grn.off {
          background: var(--rm-bad);
        }
        rm-chat-shell .cs-slot {
          flex: 1;
          min-height: 0;
          display: flex;
          flex-direction: column;
          overflow: hidden;
        }
        rm-chat-shell .cs-boot {
          flex: 1;
          display: grid;
          place-items: center;
          color: var(--rm-ink-3);
          font-size: 13px;
        }
        /* v2-A polish backlog: the slotted v1.1 <rm-chat-panel>
         * renders its own <rm-sidebar> + brand+hamburger header.
         * The v2 chat-shell already provides both, so we hide the
         * duplicates here without touching chat-panel internals.
         *
         * Two visual elements to hide:
         *   1. The whole inner <rm-sidebar> (left rail).
         *   2. The first column of chat-panel's top bar (hamburger
         *      + RoleMesh brand). We deliberately keep the right
         *      column (Cancel / Connected indicator) — that affordance
         *      has no v2 replacement yet.
         *
         * Selector for #2 is structural rather than class-based
         * because chat-panel's top bar uses raw tailwind classes.
         * If chat-panel reorders its top bar this rule no-ops; the
         * worst case is the v2-A look returns (one extra brand
         * mark). chat-panel.test.ts catches gross regressions. */
        rm-chat-shell rm-chat-panel rm-sidebar { display: none; }
        /* Hide chat-panel's whole top bar — its only payload was
         * (left) duplicate hamburger + RoleMesh brand and (right)
         * Cancel + Connected indicator. Both halves are now replaced
         * by v2 surfaces: the v2 sidebar / topbar covers the left,
         * the composer kebab carries Cancel, and the tenant pill
         * doubles as the connection indicator. ":first-child" is
         * still load-bearing — chat-panel has THREE .shrink-0
         * children (top bar, agent status, composer); we only want
         * the first one gone. */
        rm-chat-shell rm-chat-panel
          > div:first-child
          > div.flex-1
          > div.shrink-0:first-child {
          display: none;
        }
        rm-chat-shell .cs-menu {
          position: absolute;
          z-index: 40;
          background: var(--rm-surface);
          border: 1px solid var(--rm-border-2);
          border-radius: var(--rm-r);
          box-shadow: var(--rm-shadow-md);
          padding: 5px;
          min-width: 220px;
          animation: rm-pop 0.12s ease both;
        }
        rm-chat-shell .cs-menu .mi {
          display: flex;
          align-items: center;
          gap: 9px;
          padding: 8px 10px;
          border-radius: 8px;
          font-size: 13.5px;
          background: none;
          border: none;
          width: 100%;
          text-align: left;
          color: inherit;
          font-family: inherit;
          cursor: pointer;
        }
        rm-chat-shell .cs-menu .mi:hover { background: var(--rm-surface-2); }
        rm-chat-shell .cs-menu .mi .dot {
          width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
        }
        rm-chat-shell .cs-menu .mi small {
          color: var(--rm-ink-3);
          font-size: 11.5px;
          margin-left: auto;
        }
        rm-chat-shell .cs-menu .mlabel {
          font-size: 11px;
          font-weight: 600;
          color: var(--rm-ink-3);
          text-transform: uppercase;
          letter-spacing: 0.04em;
          padding: 8px 10px 3px;
        }
        rm-chat-shell .cs-menu .sep {
          height: 1px;
          background: var(--rm-border);
          margin: 5px 4px;
        }
        rm-chat-shell .coswitch-wrap,
        rm-chat-shell .userbar-wrap,
        rm-chat-shell .approvals-wrap {
          position: relative;
        }
        rm-chat-shell .cs-menu.coworker { top: 0; left: 100%; margin-left: 6px; }
        rm-chat-shell .cs-menu.user { bottom: 100%; left: 8px; right: 8px; min-width: auto; margin-bottom: 4px; }
        rm-chat-shell .cs-menu.approvals {
          top: 100%; right: 0; margin-top: 4px;
          width: 360px;
          padding: 0;
        }
        rm-chat-shell .appr-empty {
          padding: 24px 14px;
          text-align: center;
          color: var(--rm-ink-3);
          font-size: 12.5px;
        }
        rm-chat-shell .appr-hd {
          padding: 11px 14px;
          border-bottom: 1px solid var(--rm-border);
          font-size: 13px;
          font-weight: 600;
          display: flex;
          align-items: center;
          justify-content: space-between;
        }
        rm-chat-shell .appr-hd small {
          font-weight: 400;
          color: var(--rm-ink-3);
        }
        rm-chat-shell .appr-body {
          max-height: 360px;
          overflow-y: auto;
        }
        rm-chat-shell .appr-rows {
          display: flex;
          flex-direction: column;
          gap: 8px;
          padding: 10px;
        }
        rm-chat-shell .appr-row {
          /* Inline-approval already carries its own border + padding;
           * we just keep wrapper hooks so the popover can target it. */
        }
        rm-chat-shell .appr-ft {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 8px;
          padding: 9px 14px;
          border-top: 1px solid var(--rm-border);
          font-size: 12px;
          color: var(--rm-ink-3);
        }
        rm-chat-shell .appr-overflow { color: var(--rm-ink-3); }
        rm-chat-shell .appr-link {
          color: var(--rm-accent);
          text-decoration: none;
          font-weight: 500;
        }
        rm-chat-shell .appr-link:hover { text-decoration: underline; }
        rm-chat-shell .appr-stale {
          padding: 8px 14px;
          font-size: 11.5px;
          color: var(--rm-warn-ink, var(--rm-ink-3));
          background: var(--rm-warn-subtle, var(--rm-surface-2));
          border-top: 1px solid var(--rm-border);
        }
        @keyframes rm-pop {
          from { opacity: 0; transform: translateY(4px); }
          to   { opacity: 1; transform: none; }
        }
      </style>
      <rm-reauth-banner></rm-reauth-banner>
      <div class="cs-layout">
      <aside class="cs-sidebar">
        <div class="cs-brand">
          <span class="cs-brand-wm" data-testid="brand-wordmark">
            <span class="cs-brand-pri">Role</span><span
              class="cs-brand-sec">Mesh</span>
          </span>
        </div>

        <div class="coswitch-wrap">
          <button
            class="coswitch"
            data-testid="coworker-switcher"
            data-menu-trigger="coworker"
            aria-haspopup="menu"
            aria-expanded=${this.openMenu === 'coworker'}
            @click=${this.toggleCoworkerMenu}
          >
            <span
              class="cav"
              style=${`background:${colourForCoworker(active)};`}
            >${initialsFor(active?.name ?? '?')}</span>
            <span class="csw-txt">
              <b>${active?.name ?? 'No coworker'}</b>
              <span>${active?.agent_role ?? '—'}</span>
            </span>
            ${iconChevronDown(15)}
          </button>
          ${this.openMenu === 'coworker' ? this.renderCoworkerMenu() : nothing}
        </div>

        <button
          class="newchat"
          data-testid="new-chat"
          ?disabled=${!this.activeCoworkerId}
          @click=${this.startNewChat}
        >
          ${iconPlus(16)}
          New chat
        </button>
        <button class="cs-search" data-testid="search-conversations">
          ${iconSearch(15)}
          Search conversations
        </button>

        <div class="histscroll" data-testid="history-scroll">
          ${groups.length === 0
            ? html`<div class="appr-empty">No conversations yet</div>`
            : groups.map((g) => this.renderConvGroup(g))}
        </div>

        <div class="userbar-wrap">
          <button
            class="userbar"
            data-testid="user-pill"
            data-menu-trigger="user"
            aria-haspopup="menu"
            aria-expanded=${this.openMenu === 'user'}
            @click=${this.toggleUserMenu}
          >
            <div class="avatar">${initialsFor(this.me?.name ?? this.me?.email ?? '?')}</div>
            <div class="nm">
              <b>${this.me?.name ?? this.me?.email ?? 'Signed in'}</b>
              <span>${tenantLabel}</span>
            </div>
            ${iconChevronDown(16)}
          </button>
          ${this.openMenu === 'user' ? this.renderUserMenu() : nothing}
        </div>
      </aside>

      <div class="cs-main">
        <div class="cs-tbar">
          <span class="ti">${active?.name ?? 'Chat'}</span>
          ${active
            ? html`
                <span class="with">
                  <span>${active.agent_role}</span>
                </span>
              `
            : nothing}
          <span class="spacer"></span>
          <button
            class="cs-iconbtn"
            data-testid="topbar-activity"
            aria-label="Activity"
            title="Activity"
            @click=${this.openActivity}
          >${iconActivity(19)}</button>
          <div class="approvals-wrap">
            <button
              class="cs-iconbtn"
              data-testid="topbar-approvals"
              data-menu-trigger="approvals"
              aria-label="Pending approvals"
              title="Pending approvals"
              @click=${this.toggleApprovals}
            >
              ${iconApprovals(19)}
              ${this.pendingApprovals.length > 0
                ? html`<span class="bdg" data-testid="approvals-badge"
                    >${this.pendingApprovals.length}</span>`
                : nothing}
            </button>
            ${this.openMenu === 'approvals' ? this.renderApprovalsPanel() : nothing}
          </div>
          <button
            class="cs-iconbtn"
            data-testid="topbar-settings"
            aria-label="Settings"
            title="Settings"
            @click=${this.openSettings}
          >${iconSettings(19)}</button>
          <span
            class="cs-tenant"
            data-testid="tenant-pill"
            title=${this.agentConnected
              ? `${tenantLabel} · connected`
              : `${tenantLabel} · disconnected`}
          >
            <span
              class=${`grn ${this.agentConnected ? '' : 'off'}`}
              data-testid="connection-dot"
              data-connected=${this.agentConnected ? 'true' : 'false'}
            ></span>${tenantLabel}
          </span>
        </div>
        <div class="cs-slot">
          ${this.bootstrapped
            ? html`<rm-chat-panel class="flex-1 min-h-0"></rm-chat-panel>`
            : html`<div
                class="cs-boot"
                data-testid="chat-bootstrapping"
              >Loading…</div>`}
        </div>
      </div>
      </div>
    `;
  }

  private renderConvGroup(group: ConversationGroup): TemplateResult {
    return html`
      <div class="cgroup">
        <div class="grouplabel">${group.label}</div>
        ${group.items.map((c) => {
          const label =
            (c.name && c.name.trim())
              ? c.name
              : (this.convPreviews.get(c.id) ?? 'New chat');
          return html`
            <button
              class=${`conv ${c.id === this.activeConversationId ? 'active' : ''}`}
              data-testid="conversation-row"
              data-conv-id=${c.id}
              title=${label}
              @click=${() => this.navigateConversation(c.id)}
            >
              <span class="t">${label}</span>
            </button>
          `;
        })}
      </div>
    `;
  }

  private renderCoworkerMenu(): TemplateResult {
    return html`
      <div class="cs-menu coworker" role="menu" data-menu="coworker">
        <div class="mlabel">Switch coworker</div>
        ${this.coworkers.length === 0
          ? html`<div class="appr-empty">No coworkers configured</div>`
          : this.coworkers.map(
              (c) => html`
                <button
                  class="mi"
                  role="menuitem"
                  data-testid="coworker-option"
                  data-coworker-id=${c.id}
                  @click=${() => this.navigateCoworker(c.id)}
                >
                  <span
                    class="dot"
                    style=${`background:${colourForCoworker(c)};`}
                  ></span>
                  ${c.name}
                  <small>${c.agent_role}</small>
                </button>
              `,
            )}
        <div class="sep"></div>
        <button
          class="mi"
          role="menuitem"
          data-testid="manage-coworkers"
          @click=${this.goToManageCoworkers}
          style="color: var(--rm-ink-3);"
        >Manage coworkers…</button>
      </div>
    `;
  }

  private renderUserMenu(): TemplateResult {
    return html`
      <div class="cs-menu user" role="menu" data-menu="user">
        <button
          class="mi"
          role="menuitem"
          data-testid="user-menu-settings"
          @click=${this.openSettings}
        >${iconSettings(15)} Settings</button>
        <div class="sep"></div>
        <button
          class="mi"
          role="menuitem"
          data-testid="user-menu-logout"
          @click=${this.logout}
          style="color: var(--rm-bad);"
        >${iconLogout(15)} Log out</button>
      </div>
    `;
  }

  private renderApprovalsPanel(): TemplateResult {
    return html`
      <div class="cs-menu approvals" role="menu" data-menu="approvals">
        <rm-approvals-popover
          data-testid="approvals-popover"
          .rows=${this.pendingApprovals}
          .me=${this.me}
          .loading=${this.approvalsLoading}
          .connectionStatus=${this.approvalsConn}
          @rm-approval-decided=${this.onApprovalDecided}
          @rm-popover-navigate=${this.onPopoverNavigate}
        ></rm-approvals-popover>
      </div>
    `;
  }
}
