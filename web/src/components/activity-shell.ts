// <rm-activity-shell> — v2 activity surface.
//
// Routes (hash-based, owned by this shell):
//
//   #/activity                     → index (card links out)
//   #/activity/safety-decisions    → v1.1 <rm-safety-decisions-page>
//
// "Runs" tab is deliberately absent — locked decision #3 from the v2
// session brief: per-run timeline is parked for v3. The card on
// the index covers the audit surface operators actually ask for.
//
// The shell is full-overlay (covers the chat under it). The header
// owns the back-to-chat X and a tab bar that mirrors the active hash.
// We avoid client-side state for the tab — the URL is the source of
// truth so deep links / refresh land on the right tab.
//
// Why `location.hash = '#/'` for X (and the index cards' navigations):
// the chat-shell reads URL params in its constructor only (locked v2-A
// pattern), so a hash change alone wouldn't reload the chat panel.
// The activity overlay is sibling to the chat shell in the app graph,
// so a hash flip on its own is fine here — the app shell re-evaluates
// `topLevelShell(hash)` on hashchange and swaps custom elements.

import { LitElement, html, type TemplateResult } from 'lit';
import { customElement, state } from 'lit/decorators.js';

import './safety-decisions-page.js';
import {
  iconActivity,
  iconChevronRight,
  iconClose,
} from './icons.js';

type ActivityTab = 'index' | 'safety-decisions';

function tabFromHash(hash: string): ActivityTab {
  if (hash.startsWith('#/activity/safety-decisions')) return 'safety-decisions';
  return 'index';
}

@customElement('rm-activity-shell')
export class RmActivityShell extends LitElement {
  @state() private hash: string = location.hash;

  protected override createRenderRoot() {
    return this;
  }

  override connectedCallback() {
    super.connectedCallback();
    // Set the inline display the stylesheet wants — inline styles
    // beat the rendered <style> block on specificity, so leaving it
    // as a bare `block` would collapse the flex layout the body
    // depends on.
    this.style.display = 'flex';
    this.style.flexDirection = 'column';
    this.style.height = '100%';
    window.addEventListener('hashchange', this.onHashChange);
  }

  override disconnectedCallback() {
    super.disconnectedCallback();
    window.removeEventListener('hashchange', this.onHashChange);
  }

  private onHashChange = () => {
    this.hash = location.hash;
  };

  private backToChat = () => {
    location.hash = '#/';
  };

  private goTab(tab: ActivityTab): void {
    const next =
      tab === 'safety-decisions'
        ? '#/activity/safety-decisions'
        : '#/activity';
    if (location.hash === next) return;
    location.hash = next;
  }

  private renderTabs(active: ActivityTab): TemplateResult {
    const items: { id: ActivityTab; label: string }[] = [
      { id: 'index', label: 'Overview' },
      { id: 'safety-decisions', label: 'Safety decisions' },
    ];
    return html`
      <div class="as-tabs" role="tablist" aria-label="Activity sections">
        ${items.map(
          (t) => html`
            <button
              class=${`as-tab ${active === t.id ? 'active' : ''}`}
              role="tab"
              aria-selected=${active === t.id}
              data-testid=${`activity-tab-${t.id}`}
              data-tab=${t.id}
              @click=${() => this.goTab(t.id)}
            >${t.label}</button>
          `,
        )}
      </div>
    `;
  }

  private renderIndex(): TemplateResult {
    return html`
      <div class="as-index">
        <button
          class="as-card"
          data-testid="activity-card-safety-decisions"
          @click=${() => this.goTab('safety-decisions')}
        >
          <span class="as-card-icon">${iconActivity(22)}</span>
          <span class="as-card-body">
            <span class="as-card-title">Safety decisions</span>
            <span class="as-card-sub">
              Live audit of allow / block / redact / warn verdicts across
              all conversations on this tenant.
            </span>
          </span>
          <span class="as-card-arrow">${iconChevronRight(18)}</span>
        </button>
      </div>
    `;
  }

  override render(): TemplateResult {
    const active = tabFromHash(this.hash);
    const body =
      active === 'safety-decisions'
        ? html`<rm-safety-decisions-page></rm-safety-decisions-page>`
        : this.renderIndex();
    return html`
      <style>
        rm-activity-shell {
          display: flex;
          flex-direction: column;
          height: 100%;
          min-height: 0;
          background: var(--rm-bg);
          color: var(--rm-ink);
          font-family: var(--rm-font-body);
        }
        rm-activity-shell .as-hd {
          height: 52px;
          display: flex;
          align-items: center;
          padding: 0 22px;
          border-bottom: 1px solid var(--rm-border);
          gap: var(--rm-space-3);
        }
        rm-activity-shell .as-hd h2 {
          font-size: 16px;
          font-weight: 600;
          margin: 0;
        }
        rm-activity-shell .as-hd .spacer { flex: 1; }
        rm-activity-shell .as-tabs {
          display: flex;
          gap: 4px;
          padding: 0 22px;
          border-bottom: 1px solid var(--rm-border);
          background: var(--rm-bg);
        }
        rm-activity-shell .as-tab {
          background: none;
          border: none;
          padding: 11px 12px 10px;
          font-size: var(--rm-text-sm);
          font-family: inherit;
          color: var(--rm-ink-3);
          cursor: pointer;
          border-bottom: 2px solid transparent;
          margin-bottom: -1px;
          transition: 0.13s;
        }
        rm-activity-shell .as-tab:hover { color: var(--rm-ink); }
        rm-activity-shell .as-tab.active {
          color: var(--rm-ink);
          border-bottom-color: var(--rm-accent);
          font-weight: 500;
        }
        rm-activity-shell .as-close {
          width: 28px;
          height: 28px;
          border-radius: 7px;
          display: grid;
          place-items: center;
          color: var(--rm-ink-3);
          background: none;
          border: none;
          cursor: pointer;
          font-family: inherit;
        }
        rm-activity-shell .as-close:hover {
          background: var(--rm-surface-3);
          color: var(--rm-ink);
        }
        rm-activity-shell .as-body {
          flex: 1;
          min-height: 0;
          display: flex;
          flex-direction: column;
          overflow: hidden;
        }
        rm-activity-shell .as-index {
          padding: 28px 26px;
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
          gap: 14px;
          max-width: 940px;
          width: 100%;
          margin: 0 auto;
        }
        rm-activity-shell .as-card {
          display: flex;
          align-items: center;
          gap: 14px;
          padding: 16px 18px;
          background: var(--rm-surface);
          border: 1px solid var(--rm-border);
          border-radius: var(--rm-r-lg);
          cursor: pointer;
          font-family: inherit;
          color: inherit;
          text-align: left;
          transition: 0.15s;
        }
        rm-activity-shell .as-card:hover {
          border-color: var(--rm-ink-3);
          background: var(--rm-surface-2);
        }
        rm-activity-shell .as-card-icon {
          width: 38px;
          height: 38px;
          border-radius: 9px;
          background: var(--rm-accent-subtle);
          color: var(--rm-accent-2);
          display: grid;
          place-items: center;
          flex-shrink: 0;
        }
        rm-activity-shell .as-card-body {
          display: flex;
          flex-direction: column;
          gap: 3px;
          flex: 1;
          min-width: 0;
        }
        rm-activity-shell .as-card-title {
          font-size: 14px;
          font-weight: 600;
        }
        rm-activity-shell .as-card-sub {
          font-size: 12.5px;
          color: var(--rm-ink-3);
          line-height: 1.4;
        }
        rm-activity-shell .as-card-arrow {
          color: var(--rm-ink-3);
          flex-shrink: 0;
        }
      </style>
      <div class="as-hd">
        <h2>Activity</h2>
        <span class="spacer"></span>
        <button
          class="as-close"
          data-testid="activity-back"
          aria-label="Back to chat"
          @click=${this.backToChat}
        >${iconClose(16)}</button>
      </div>
      ${this.renderTabs(active)}
      <div class="as-body" data-testid="activity-body" data-tab=${active}>
        ${body}
      </div>
    `;
  }
}
