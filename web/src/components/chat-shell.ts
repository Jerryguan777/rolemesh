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
  type Conversation,
  type Coworker,
  type Me,
} from '../api/client.js';
import './chat-panel.js';
import './reauth-banner.js';
import {
  iconActivity,
  iconApprovals,
  iconChevronDown,
  iconClose,
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
  /** Which popover, if any, is open. Only one at a time to keep
   *  keyboard handling simple. */
  @state() private openMenu: '' | 'coworker' | 'user' | 'approvals' = '';

  private readonly api = getApiClient();
  /** Hard-coded until v2-C wires the live count. */
  private readonly approvalsBadge = 0;

  protected override createRenderRoot() {
    // Light DOM so the chat-panel's contenteditable composer + the
    // <rm-reauth-banner> custom event bus keep working without
    // shadow-boundary plumbing.
    return this;
  }

  override connectedCallback() {
    super.connectedCallback();
    this.style.display = 'block';
    this.style.height = '100%';
    // Force chat-panel's internal sidebar collapsed so we render
    // ONE sidebar (the v2 shell's), not two. chat-panel reads this
    // in its constructor, so we must set it before chat-panel
    // mounts inside the slot.
    localStorage.setItem('rm-sidebar-collapsed', 'true');

    const params = new URLSearchParams(location.search);
    this.activeCoworkerId = params.get('agent_id');
    this.activeConversationId = params.get('chat_id');
    void this.bootstrap();
    document.addEventListener('click', this.onDocumentClick, true);
  }

  override disconnectedCallback() {
    super.disconnectedCallback();
    document.removeEventListener('click', this.onDocumentClick, true);
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

  private navigateCoworker(coworkerId: string): void {
    if (coworkerId === this.activeCoworkerId) {
      this.openMenu = '';
      return;
    }
    // Switching coworker resets chat — drop the conversation id so
    // chat-panel starts fresh. Reload picks up the new agent_id.
    location.href = this.buildHref(coworkerId, null);
  }

  private navigateConversation(chatId: string): void {
    if (!this.activeCoworkerId) return;
    if (chatId === this.activeConversationId) return;
    location.href = this.buildHref(this.activeCoworkerId, chatId);
  }

  private startNewChat = () => {
    if (!this.activeCoworkerId) return;
    location.href = this.buildHref(this.activeCoworkerId, null);
  };

  private openActivity = () => {
    location.hash = '#/activity';
  };

  private toggleApprovals = () => {
    this.openMenu = this.openMenu === 'approvals' ? '' : 'approvals';
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
         * touch the shell and not the v1.1 settings shell or login. */
        rm-chat-shell {
          display: grid;
          grid-template-columns: 272px 1fr;
          height: 100%;
          min-height: 0;
          background: var(--rm-bg);
          color: var(--rm-ink);
          font-family: var(--rm-font-body);
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
          gap: 9px;
          padding: 14px 14px 10px;
        }
        rm-chat-shell .cs-brand .mark {
          width: 26px;
          height: 26px;
          border-radius: 8px;
          background: var(--rm-accent);
          color: var(--rm-accent-ink);
          display: grid;
          place-items: center;
          font-weight: 600;
          font-size: 13px;
          flex-shrink: 0;
        }
        rm-chat-shell .cs-brand b { font-weight: 600; font-size: 14.5px; }
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
        rm-chat-shell .cs-slot {
          flex: 1;
          min-height: 0;
          display: flex;
          flex-direction: column;
          overflow: hidden;
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
          width: 320px;
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
        @keyframes rm-pop {
          from { opacity: 0; transform: translateY(4px); }
          to   { opacity: 1; transform: none; }
        }
      </style>
      <rm-reauth-banner></rm-reauth-banner>
      <aside class="cs-sidebar">
        <div class="cs-brand">
          <div class="mark">R</div>
          <div><b>RoleMesh</b></div>
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
              ${this.approvalsBadge > 0
                ? html`<span class="bdg">${this.approvalsBadge}</span>`
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
          <span class="cs-tenant" data-testid="tenant-pill">
            <span class="grn"></span>${tenantLabel}
          </span>
        </div>
        <div class="cs-slot">
          <rm-chat-panel class="flex-1 min-h-0"></rm-chat-panel>
        </div>
      </div>
    `;
  }

  private renderConvGroup(group: ConversationGroup): TemplateResult {
    return html`
      <div class="cgroup">
        <div class="grouplabel">${group.label}</div>
        ${group.items.map(
          (c) => html`
            <button
              class=${`conv ${c.id === this.activeConversationId ? 'active' : ''}`}
              data-testid="conversation-row"
              data-conv-id=${c.id}
              @click=${() => this.navigateConversation(c.id)}
            >
              <span class="t">${c.name ?? 'Conversation'}</span>
            </button>
          `,
        )}
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
        <div class="appr-hd">
          Pending approvals
          <small>v2-C wires the live feed</small>
        </div>
        <div class="appr-empty">
          Approvals popover is a placeholder until v2-C.<br />
          Resolve approvals from the
          <a
            href="#/manage/approval-policies"
            style="color: var(--rm-accent);"
            @click=${() => { this.openMenu = ''; }}
          >approvals page</a>.
        </div>
      </div>
    `;
  }
}
