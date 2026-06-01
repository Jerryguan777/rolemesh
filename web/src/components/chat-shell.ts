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
//   - panel-owned: messages, runs, WebSocket lifecycle

import { LitElement, html, nothing, type TemplateResult } from 'lit';
import { customElement, state } from 'lit/decorators.js';

import {
  ApiError,
  getApiClient,
  type Conversation,
  type Coworker,
  type Me,
  type Model,
} from '../api/client.js';
import {
  coworkerSubtitle,
  modelsByIdMap,
} from '../services/coworker-label.js';
import { connectionState } from '../ws/connection-state.js';
import { clearToken } from '../services/oidc-auth.js';
import './chat-panel.js';
import './approvals-inbox.js';
import type {
  ApprovalsCountDetail,
  ApprovalsInbox,
} from './approvals-inbox.js';
import './reauth-banner.js';
import {
  iconActivity,
  iconChevronDown,
  iconClose,
  iconInbox,
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
 * Within each bucket, rows are sorted **newest-first** by
 * `created_at`. The backend's list ordering is not contractual, so
 * relying on it would mean a freshly-created chat (via + New chat)
 * could land mid-list instead of at the top — exactly the bug a
 * user just reported. Pin the sort here.
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
  // created_at is ISO-8601 — lexicographic compare matches chronology,
  // so we can sort on the raw string without parsing a Date per row.
  const newestFirst = (a: Conversation, b: Conversation) =>
    a.created_at < b.created_at ? 1 : a.created_at > b.created_at ? -1 : 0;
  today.sort(newestFirst);
  yesterday.sort(newestFirst);
  earlier.sort(newestFirst);
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
  /** conversation_ids known to have zero messages on the server
   *  (i.e. listMessages returned []). Populated by
   *  `loadConversationPreviews` after each request completes. Used
   *  by `renderConvGroup` to hide history rows that would otherwise
   *  read "New chat" — the row is meaningless to the user since
   *  they have not asked anything yet. The currently active
   *  conversation is exempted: bootstrap may have just auto-created
   *  it to land on a connected page, and hiding it would leave the
   *  user inside an invisible row. */
  @state() private emptyConvIds = new Set<string>();
  /** Mirror of the aggregate `ConnectionState` — true iff any of the
   *  registered WS clients (V1WsClient for the active conversation,
   *  legacy AgentClient for Stop) currently report an open socket.
   *  We also keep the legacy `agent-connection` event listener wired
   *  as a belt-and-braces backup so a not-yet-migrated socket source
   *  can still flip the dot. */
  @state() private agentConnected = false;
  /** Tenant model catalogue — used to render the "Backend · Model"
   *  subtitle for each coworker. Lookup map keyed by Model.id. A
   *  failed listModels call leaves the map empty; coworker subtitles
   *  then degrade to just the backend label (see coworkerSubtitle). */
  @state() private modelsById: Map<string, Model> = new Map();
  /** Flipped to true once bootstrap finishes resolving (or creating)
   *  a chat_id for the active coworker. chat-panel is mounted only
   *  AFTER this flips so it reads the post-resolution URL params in
   *  its constructor and lands on an immediately-connected state
   *  instead of an empty "Disconnected" page. */
  @state() private bootstrapped = false;
  /** Which popover, if any, is open. Only one at a time to keep
   *  keyboard handling simple. */
  @state() private openMenu: '' | 'coworker' | 'user' | 'approvals' = '';
  /** Pending-approval count for the top-bar badge, fed by the inbox's
   *  `approvals-count` event (the inbox owns the store — §4.8). `urgent`
   *  flags ≥1 item under the 5-minute expiry line so the badge deepens. */
  @state() private approvalTotal = 0;
  @state() private approvalUrgent = false;
  /** Sidebar conversation-list search. Toggled by the "Search
   *  conversations" button; clearing or closing restores the full
   *  list. Filtering is purely client-side over the already-fetched
   *  conversations + previews. */
  @state() private searchOpen = false;
  @state() private searchQuery = '';

  private readonly api = getApiClient();
  /** Unsubscribe handle for the ConnectionState subscription so the
   *  shell doesn't leak listeners across mount/unmount cycles. */
  private connStateUnsub: (() => void) | null = null;

  protected override createRenderRoot() {
    // Light DOM so the chat-panel's contenteditable composer + the
    // <rm-reauth-banner> custom event bus keep working without
    // shadow-boundary plumbing.
    return this;
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
    // Approvals inbox plumbing (§4): the inbox owns its own store and
    // bubbles {total,urgent} for the badge; it asks us to close the
    // popover on a row jump; and the chat-panel bubbles `approval-activity`
    // whenever a card is requested/resolved so we re-pull the inbox.
    this.addEventListener('approvals-count', this.onApprovalsCount as EventListener);
    this.addEventListener('inbox-close', this.onInboxClose);
    this.addEventListener('approval-activity', this.onApprovalActivity);
    // Subscribe to the aggregate ConnectionState so any WS client
    // flipping open/closed directly drives the top-bar dot — even if
    // the message-editor's agent-connection relay misses an edge.
    this.agentConnected = connectionState.connected;
    this.connStateUnsub = connectionState.subscribe((c) => {
      this.agentConnected = c;
    });
  }

  override disconnectedCallback() {
    super.disconnectedCallback();
    document.removeEventListener('click', this.onDocumentClick, true);
    this.removeEventListener('agent-connection', this.onAgentConnection);
    this.removeEventListener('approvals-count', this.onApprovalsCount as EventListener);
    this.removeEventListener('inbox-close', this.onInboxClose);
    this.removeEventListener('approval-activity', this.onApprovalActivity);
    this.connStateUnsub?.();
    this.connStateUnsub = null;
  }

  private onAgentConnection = (e: Event) => {
    const detail = (e as CustomEvent<{ connected: boolean }>).detail;
    if (detail && typeof detail.connected === 'boolean') {
      this.agentConnected = detail.connected;
    }
  };

  private onApprovalsCount = (e: CustomEvent<ApprovalsCountDetail>): void => {
    this.approvalTotal = e.detail.total;
    this.approvalUrgent = e.detail.urgent > 0;
  };

  private onInboxClose = (): void => {
    if (this.openMenu === 'approvals') this.openMenu = '';
  };

  /** A chat-panel approval card was requested/resolved — re-pull the
   *  inbox so the badge + list reflect the change without waiting for the
   *  next open or poll. */
  private onApprovalActivity = (): void => {
    void this.querySelector<ApprovalsInbox>('rm-approvals-inbox')?.refresh();
  };

  private toggleApprovals = (): void => {
    this.openMenu = this.openMenu === 'approvals' ? '' : 'approvals';
  };

  /** Wired into the inbox: switch sidebar coworker + open the gated
   *  conversation. Cross-coworker / cross-conversation jumps full-reload
   *  via the existing navigation (chat-panel reads URL params in its
   *  constructor); a jump to the already-active conversation is a no-op
   *  here and the inbox just scrolls to the card in place (§4.7). */
  private jumpToConversation = async (
    conversationId: string | null,
    coworkerId: string | null,
  ): Promise<void> => {
    if (coworkerId && coworkerId !== this.activeCoworkerId) {
      location.href = this.buildHref(coworkerId, conversationId);
      return;
    }
    if (conversationId && conversationId !== this.activeConversationId) {
      this.navigateConversation(conversationId);
    }
  };

  private async bootstrap(): Promise<void> {
    // All three are independent — fire in parallel so first paint
    // does not wait on the slowest endpoint.
    const [coworkersResult, meResult, modelsResult] = await Promise.allSettled([
      this.api.listCoworkers(),
      this.api.getMe(),
      this.api.listModels(),
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
    if (modelsResult.status === 'fulfilled') {
      this.modelsById = modelsByIdMap(modelsResult.value);
    } else {
      // Subtitle degrades to just the backend label — non-fatal.
      console.warn('chat-shell: listModels failed', modelsResult.reason);
    }
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
   *  fallback for unnamed rows.
   *
   *  Also tracks which conversations have ZERO messages on the
   *  server: those land in `emptyConvIds` so the sidebar can hide
   *  the otherwise meaningless "New chat" rows (the empty state is
   *  invariably a user-clicked-+New-chat-then-didn't-send, or our
   *  own bootstrap auto-create).
   */
  private async loadConversationPreviews(): Promise<void> {
    const needed = this.conversations.filter(
      (c) => !this.convPreviews.has(c.id),
    );
    if (needed.length === 0) return;
    const results = await Promise.allSettled(
      needed.map((c) => this.api.listMessages(c.id)),
    );
    const next = new Map(this.convPreviews);
    const nextEmpty = new Set(this.emptyConvIds);
    for (let i = 0; i < needed.length; i += 1) {
      const r = results[i];
      if (r.status !== 'fulfilled') continue;
      if (r.value.length === 0) {
        // Truly empty — mark for hiding (active conv is exempted at
        // render time).
        nextEmpty.add(needed[i].id);
        continue;
      }
      // Conversation has at least one message — defensively remove
      // it from the empty set in case a previous load thought it
      // was empty and a refresh now sees content.
      nextEmpty.delete(needed[i].id);
      const firstUser = r.value.find((m) => m.role === 'user');
      const source = firstUser ?? r.value[0];
      if (!source || !source.content) continue;
      next.set(needed[i].id, RmChatShell.formatPreview(source.content));
    }
    this.convPreviews = next;
    this.emptyConvIds = nextEmpty;
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
    clearToken();
    // Hand the auth state machine back the wheel.
    window.dispatchEvent(new CustomEvent('rm-auth-failed'));
  };

  private goToManageCoworkers = () => {
    this.openMenu = '';
    location.hash = '#/manage/coworkers';
  };

  private openSearch = async () => {
    this.searchOpen = true;
    await this.updateComplete;
    this.querySelector<HTMLInputElement>('[data-testid="search-input"]')?.focus();
  };

  private closeSearch = () => {
    this.searchOpen = false;
    this.searchQuery = '';
  };

  private onSearchInput = (e: Event) => {
    this.searchQuery = (e.target as HTMLInputElement).value;
  };

  private onSearchKeydown = (e: KeyboardEvent) => {
    if (e.key === 'Escape') {
      this.closeSearch();
    }
  };

  override render(): TemplateResult {
    const active = this.activeCoworker;
    // Hide empty-and-unnamed conversation rows from the history list.
    // The active conversation is always kept — bootstrap may have
    // just auto-created it, and the user is sitting in it right now.
    // A conversation the user explicitly named via `Conversation.name`
    // also stays, even if no messages have landed yet.
    const visibleConversations = this.conversations.filter((c) => {
      if (c.id === this.activeConversationId) return true;
      if (c.name && c.name.trim()) return true;
      return !this.emptyConvIds.has(c.id);
    });
    // Case-insensitive substring match on the row's displayed label
    // (name → preview → "New chat"). Active conversation is kept so
    // the user can never lose the page they're currently on by typing
    // a filter; the empty-history line then explains "no matches".
    const q = this.searchQuery.trim().toLowerCase();
    const filteredConversations = q
      ? visibleConversations.filter((c) => {
          if (c.id === this.activeConversationId) return true;
          const label = (c.name && c.name.trim())
            ? c.name
            : (this.convPreviews.get(c.id) ?? 'New chat');
          return label.toLowerCase().includes(q);
        })
      : visibleConversations;
    const groups = groupConversations(filteredConversations);
    // Environment suffix is build-time configurable via `VITE_RM_ENV`
    // (e.g. `prod`, `staging`, `dev`). When unset we render the
    // tenant id alone — better than inventing a wrong label.
    const env = (import.meta.env.VITE_RM_ENV ?? '').toString().trim();
    const tenantLabel = this.me?.tenant_id
      ? (env
          ? `${this.me.tenant_id.slice(0, 12)} · ${env}`
          : this.me.tenant_id.slice(0, 12))
      : (env ? `workspace · ${env}` : 'workspace');
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
          border-radius: var(--rm-radius-sm);
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
          font-size: var(--rm-text-xs);
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
          gap: var(--rm-space-2);
          padding: 7px 10px;
          border-radius: var(--rm-r);
          color: var(--rm-ink-3);
          font-size: var(--rm-text-sm);
          background: none;
          border: none;
          font-family: inherit;
          cursor: pointer;
          text-align: left;
          width: calc(100% - 24px);
        }
        rm-chat-shell .cs-search:hover { background: var(--rm-surface); }
        rm-chat-shell .cs-search-input {
          margin: 0 12px 8px;
          display: flex;
          align-items: center;
          gap: var(--rm-space-2);
          padding: 6px 8px 6px 10px;
          border-radius: var(--rm-r);
          background: var(--rm-surface);
          border: 1px solid var(--rm-border);
          color: var(--rm-ink);
        }
        rm-chat-shell .cs-search-input input {
          flex: 1;
          min-width: 0;
          border: none;
          outline: none;
          background: transparent;
          font: inherit;
          color: inherit;
          padding: 0;
        }
        rm-chat-shell .cs-search-close {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          padding: 2px;
          background: none;
          border: none;
          color: var(--rm-ink-3);
          cursor: pointer;
          border-radius: 6px;
        }
        rm-chat-shell .cs-search-close:hover {
          color: var(--rm-ink);
          background: var(--rm-bg);
        }
        rm-chat-shell .histscroll {
          flex: 1;
          overflow-y: auto;
          padding: var(--rm-space-2);
        }
        rm-chat-shell .grouplabel {
          font-size: var(--rm-text-xs);
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
          font-size: var(--rm-text-xs);
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
          border-radius: var(--rm-radius-sm);
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
        /* Deeper red when any pending approval is < 5 minutes from expiry
         * (spec §4.1) — the dot draws the eye before the deadline lapses. */
        rm-chat-shell .cs-iconbtn .bdg.urgent {
          background: var(--rm-bad);
        }
        /* The approvals popover anchors to this relatively-positioned
         * wrapper so the inbox's absolutely-positioned panel hangs under
         * the trigger button. */
        rm-chat-shell .appr-anchor {
          position: relative;
          display: inline-flex;
        }
        /* Amber halo pulse on the card the inbox jumps to (§4.7). Lives
         * here (not in the inbox component) so it is present in the global
         * stylesheet even when the popover has already closed; the target
         * card lives in the slotted chat-panel. */
        rm-chat-shell .rm-appr-highlight {
          animation: rm-appr-highlight 1.8s ease-out;
        }
        @keyframes rm-appr-highlight {
          0% {
            box-shadow: 0 0 0 0 var(--rm-accent);
          }
          25% {
            box-shadow: 0 0 0 4px var(--rm-accent-subtle, rgba(194, 97, 63, 0.3));
          }
          100% {
            box-shadow: 0 0 0 0 rgba(194, 97, 63, 0);
          }
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
          font-size: var(--rm-text-sm);
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
          border-radius: var(--rm-radius-sm);
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
          /* The Backend · Model subtitle can run long
           * ("Claude · Claude Sonnet 4.6 (Bedrock)"). Keep it on one
           * line so it doesn't wrap under the coworker name and
           * push the row to two lines. */
          white-space: nowrap;
        }
        rm-chat-shell .cs-menu .mlabel {
          font-size: var(--rm-text-xs);
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
        rm-chat-shell .userbar-wrap {
          position: relative;
        }
        /* Coworker menu pops out to the right of the switcher button
         * (top:0; left:100%). The min-width override is load-bearing:
         * the default .cs-menu min-width is 220px which used to fit
         * the old "operations" / "super_agent" subtitle, but the new
         * "Backend · Model display_name" can hit ~38 characters and
         * was forcing a 2-line wrap. */
        rm-chat-shell .cs-menu.coworker {
          top: 0;
          left: 100%;
          margin-left: 6px;
          min-width: 340px;
        }
        rm-chat-shell .cs-menu.user { bottom: 100%; left: 8px; right: 8px; min-width: auto; margin-bottom: 4px; }
        rm-chat-shell .appr-empty {
          padding: 24px 14px;
          text-align: center;
          color: var(--rm-ink-3);
          font-size: 12.5px;
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
              <span>${active
                ? coworkerSubtitle(active, this.modelsById)
                : '—'}</span>
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
        ${this.searchOpen
          ? html`
              <div class="cs-search-input" data-testid="search-input-wrap">
                ${iconSearch(15)}
                <input
                  type="text"
                  data-testid="search-input"
                  placeholder="Search conversations"
                  .value=${this.searchQuery}
                  @input=${this.onSearchInput}
                  @keydown=${this.onSearchKeydown}
                />
                <button
                  class="cs-search-close"
                  data-testid="search-close"
                  aria-label="Close search"
                  @click=${this.closeSearch}
                >${iconClose(14)}</button>
              </div>
            `
          : html`
              <button
                class="cs-search"
                data-testid="search-conversations"
                @click=${this.openSearch}
              >
                ${iconSearch(15)}
                Search conversations
              </button>
            `}

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
                  <span>${coworkerSubtitle(active, this.modelsById)}</span>
                </span>
              `
            : nothing}
          <span class="spacer"></span>
          <span class="appr-anchor">
            <button
              class="cs-iconbtn"
              data-testid="topbar-approvals"
              data-menu-trigger="approvals"
              aria-label="Approvals"
              aria-haspopup="dialog"
              aria-expanded=${this.openMenu === 'approvals'}
              title="Approvals"
              @click=${this.toggleApprovals}
            >
              ${iconInbox(19)}
              ${this.approvalTotal > 0
                ? html`<span
                    class=${`bdg ${this.approvalUrgent ? 'urgent' : ''}`}
                    data-testid="approvals-badge"
                    data-urgent=${this.approvalUrgent ? 'true' : 'false'}
                    >${this.approvalTotal}</span
                  >`
                : nothing}
            </button>
            <rm-approvals-inbox
              data-testid="approvals-inbox"
              .open=${this.openMenu === 'approvals'}
              .activeConversationId=${this.activeConversationId}
              .coworkers=${this.coworkers}
              .conversations=${this.conversations}
              .jumpHandler=${this.jumpToConversation}
            ></rm-approvals-inbox>
          </span>
          <button
            class="cs-iconbtn"
            data-testid="topbar-activity"
            aria-label="Activity"
            title="Activity"
            @click=${this.openActivity}
          >${iconActivity(19)}</button>
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
                  <small>${coworkerSubtitle(c, this.modelsById)}</small>
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
}
