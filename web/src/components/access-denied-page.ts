// <rm-access-denied> — the friendly 403 fallback (spec §7.7).
//
// Rendered in the settings-shell main pane when a user URL-jumps to a
// slug their capabilities don't allow (e.g. a member opening
// `#/manage/safety`). The nav rail stays visible around it — the user
// learns *why* the page is gated and is offered a one-click way back to
// somewhere they CAN see, rather than a silent redirect that reads like
// a glitch.
//
// This component owns NO authorization. It is presentation only — the
// shell decides when to mount it and which capability/label to pass in.
// Light DOM + `--rm-` tokens, matching the other settings pages
// (appearance-page.ts is the closest sibling for visual weight).

import { LitElement, html, nothing } from 'lit';
import { customElement, property } from 'lit/decorators.js';

@customElement('rm-access-denied')
export class RmAccessDenied extends LitElement {
  /** The capability string the gated page requires (e.g.
   *  `safety.read`). Named in the copy so the user knows exactly what
   *  they lack — and so a support conversation has a precise term. */
  @property({ type: String }) capability = '';

  /** Human-readable name of the page that was gated (e.g.
   *  `Safety rules`). Optional; falls back to a generic phrasing when
   *  the shell can't name the page. */
  @property({ type: String }) pageLabel = '';

  /** Slug to return to — the first nav entry this user CAN see (usually
   *  `coworkers`). The shell computes it from the filtered nav set and
   *  passes it in. */
  @property({ type: String }) backSlug = 'coworkers';

  /** Display label for the back link's target (e.g. `Coworkers`). */
  @property({ type: String }) backLabel = 'Coworkers';

  protected override createRenderRoot() {
    // Light DOM — matches the other settings pages so the `--rm-` tokens
    // resolve at the document root. The styles are inlined as a `<style>`
    // in render() rather than `static styles` (which only applies to a
    // shadow root and breaks light-DOM rendering).
    return this;
  }

  override render() {
    const pageName = this.pageLabel ? `The ${this.pageLabel} page` : 'This page';
    return html`
      <style>
        rm-access-denied {
          display: flex;
          align-items: center;
          justify-content: center;
          height: 100%;
          width: 100%;
          padding: 32px 24px;
          font-family: var(--rm-font-body);
          color: var(--rm-ink);
        }
        rm-access-denied .ad-card {
          max-width: 440px;
          text-align: center;
        }
        rm-access-denied .ad-glyph {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          width: 52px;
          height: 52px;
          border-radius: var(--rm-r-lg);
          background: var(--rm-surface-2);
          border: 1px solid var(--rm-border);
          color: var(--rm-ink-3);
          margin-bottom: 18px;
        }
        rm-access-denied h2 {
          margin: 0 0 10px;
          font-size: var(--rm-text-lg);
          font-weight: 600;
          letter-spacing: -0.01em;
        }
        rm-access-denied p {
          margin: 0 0 22px;
          font-size: var(--rm-text-sm);
          line-height: 1.6;
          color: var(--rm-ink-2);
        }
        rm-access-denied p code {
          font-family: var(--rm-font-mono);
          font-size: 0.92em;
          padding: 1px 5px;
          border-radius: var(--rm-radius-sm);
          background: var(--rm-accent-subtle);
          color: var(--rm-accent-2);
        }
        rm-access-denied .ad-back {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          padding: 8px 16px;
          border-radius: var(--rm-radius-sm);
          border: 1px solid var(--rm-border);
          background: var(--rm-surface);
          color: var(--rm-ink);
          font-size: var(--rm-text-sm);
          font-weight: 500;
          font-family: inherit;
          text-decoration: none;
          cursor: pointer;
          transition: 0.12s;
        }
        rm-access-denied .ad-back:hover {
          background: var(--rm-surface-2);
          border-color: var(--rm-border-2);
        }
      </style>
      <div class="ad-card" data-testid="access-denied">
        <div class="ad-glyph" aria-hidden="true">
          <svg
            xmlns="http://www.w3.org/2000/svg"
            width="24"
            height="24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
            viewBox="0 0 24 24"
          >
            <rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect>
            <path d="M7 11V7a5 5 0 0 1 10 0v4"></path>
          </svg>
        </div>
        <h2>Access denied</h2>
        <p data-testid="access-denied-copy">
          ${pageName} requires the
          ${this.capability
            ? html`<code data-testid="access-denied-capability"
                >${this.capability}</code
              >`
            : nothing}
          capability, which your current role doesn't have. Ask an admin or
          owner if you need access.
        </p>
        <a
          class="ad-back"
          data-testid="access-denied-back"
          data-slug=${this.backSlug}
          href=${`#/manage/${this.backSlug}`}
        >
          ← Back to ${this.backLabel}
        </a>
      </div>
    `;
  }
}
