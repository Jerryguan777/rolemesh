import { LitElement, html } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import type { ConversationSummary } from '../services/agent-client.js';

interface GroupedConversations {
  label: string;
  items: ConversationSummary[];
}

function groupByDate(conversations: ConversationSummary[]): GroupedConversations[] {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const yesterday = new Date(today.getTime() - 86400000);
  const week = new Date(today.getTime() - 7 * 86400000);

  const groups: Record<string, ConversationSummary[]> = {
    Today: [],
    Yesterday: [],
    'Previous 7 days': [],
    Older: [],
  };

  for (const c of conversations) {
    if (!c.updatedAt) {
      groups['Older'].push(c);
      continue;
    }
    const d = new Date(c.updatedAt);
    if (d >= today) groups['Today'].push(c);
    else if (d >= yesterday) groups['Yesterday'].push(c);
    else if (d >= week) groups['Previous 7 days'].push(c);
    else groups['Older'].push(c);
  }

  return Object.entries(groups)
    .filter(([, items]) => items.length > 0)
    .map(([label, items]) => ({ label, items }));
}

@customElement('rm-sidebar')
export class Sidebar extends LitElement {
  @property({ attribute: false }) conversations: ConversationSummary[] = [];
  @property() activeChatId: string | null = null;
  @property({ type: Boolean }) collapsed = false;
  @state() private settingsOpen = false;

  protected override createRenderRoot() { return this; }

  override connectedCallback() {
    super.connectedCallback();
    document.addEventListener('click', this.handleOutsideClick);
  }

  override disconnectedCallback() {
    super.disconnectedCallback();
    document.removeEventListener('click', this.handleOutsideClick);
  }

  // Close the settings popover when the user clicks outside of it.
  // Bound as an arrow to keep `this` stable for add/remove listener.
  private handleOutsideClick = (e: Event) => {
    if (!this.settingsOpen) return;
    const target = e.target as Node;
    if (!this.contains(target)) {
      this.settingsOpen = false;
    }
  };

  private handleSelect(chatId: string) {
    this.dispatchEvent(new CustomEvent('select-conversation', {
      detail: { chatId },
      bubbles: true, composed: true,
    }));
  }

  private handleNewChat() {
    this.dispatchEvent(new CustomEvent('new-chat', {
      bubbles: true, composed: true,
    }));
  }

  private handleToggle() {
    this.dispatchEvent(new CustomEvent('toggle-sidebar', {
      bubbles: true, composed: true,
    }));
  }

  private toggleSettings(e: Event) {
    // Stop propagation so the document-level outside-click handler
    // doesn't immediately close the popover we just opened.
    e.stopPropagation();
    this.settingsOpen = !this.settingsOpen;
  }

  private navigateHash(hash: string) {
    location.hash = hash;
    this.settingsOpen = false;
  }

  override render() {
    if (this.collapsed) return html``;

    const groups = groupByDate(this.conversations);

    return html`
      <div class="w-64 shrink-0 h-full flex flex-col bg-surface-1 dark:bg-d-surface-1 border-r border-surface-3 dark:border-d-surface-3 overflow-hidden">
        <!-- New chat button -->
        <div class="p-3">
          <button
            class="w-full flex items-center gap-2 px-3 py-2 rounded-lg text-[13px] font-medium
              text-ink-1 dark:text-d-ink-1 border border-surface-3 dark:border-d-surface-3
              hover:bg-surface-2 dark:hover:bg-d-surface-2 transition-colors cursor-pointer"
            @click=${this.handleNewChat}
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" viewBox="0 0 24 24"><path d="M12 5v14"/><path d="M5 12h14"/></svg>
            New chat
          </button>
        </div>

        <!-- Conversation list -->
        <div class="flex-1 overflow-y-auto px-2 pb-3">
          ${groups.map((g) => html`
            <div class="mb-2">
              <div class="px-2 py-1.5 text-[11px] font-semibold text-ink-3 dark:text-d-ink-3 uppercase tracking-wider">${g.label}</div>
              ${g.items.map((c) => html`
                <button
                  class="w-full text-left px-3 py-2 rounded-lg text-[13px] truncate transition-colors cursor-pointer
                    ${c.chatId === this.activeChatId
                      ? 'bg-brand/10 text-brand dark:text-brand-light font-medium'
                      : 'text-ink-1 dark:text-d-ink-1 hover:bg-surface-2 dark:hover:bg-d-surface-2'}"
                  @click=${() => this.handleSelect(c.chatId)}
                  title=${c.title}
                >${c.title}</button>
              `)}
            </div>
          `)}
          ${this.conversations.length === 0 ? html`
            <div class="px-3 py-6 text-[12px] text-ink-3 dark:text-d-ink-3 text-center">
              No conversations yet
            </div>
          ` : ''}
        </div>

        <!-- Settings (pinned at bottom; does not scroll with conversation list) -->
        <div class="shrink-0 border-t border-surface-3 dark:border-d-surface-3 p-2 relative">
          ${this.settingsOpen ? html`
            <div class="absolute bottom-full left-2 right-2 mb-1 bg-surface-0 dark:bg-d-surface-0 border border-surface-3 dark:border-d-surface-3 rounded-lg shadow-lg py-1 z-10">
              <button
                class="w-full text-left px-3 py-2 text-[13px] text-ink-1 dark:text-d-ink-1 hover:bg-surface-2 dark:hover:bg-d-surface-2 cursor-pointer"
                @click=${() => this.navigateHash('#/admin/safety/rules')}
              >Safety rules</button>
              <button
                class="w-full text-left px-3 py-2 text-[13px] text-ink-1 dark:text-d-ink-1 hover:bg-surface-2 dark:hover:bg-d-surface-2 cursor-pointer"
                @click=${() => this.navigateHash('#/admin/safety/decisions')}
              >Safety decisions</button>
            </div>
          ` : ''}
          <button
            class="w-full flex items-center gap-2 px-3 py-2 rounded-lg text-[13px] font-medium text-ink-1 dark:text-d-ink-1 hover:bg-surface-2 dark:hover:bg-d-surface-2 transition-colors cursor-pointer"
            @click=${(e: Event) => this.toggleSettings(e)}
            aria-haspopup="menu"
            aria-expanded=${this.settingsOpen}
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24">
              <circle cx="12" cy="12" r="3"/>
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>
            </svg>
            Settings
          </button>
        </div>
      </div>
    `;
  }
}
