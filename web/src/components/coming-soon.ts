import { LitElement, html } from 'lit';
import { customElement, property } from 'lit/decorators.js';

// Placeholder for not-yet-implemented v1.1 pages. The phase tag is
// rendered explicitly so a dev running the local UI can see roughly
// *when* the page is expected to land — not just "coming soon".
//
// Why a separate component instead of inline HTML in the router map:
// the placeholder needs its own DOM hooks (e.g. a future "subscribe
// to release notes" CTA) and centralising it means we change one
// file when the styling drifts.
@customElement('rm-coming-soon')
export class ComingSoon extends LitElement {
  @property({ type: String }) label = '';
  @property({ type: Number }) phase = 1;

  protected override createRenderRoot() {
    return this;
  }

  override render() {
    return html`
      <div class="h-full w-full flex items-center justify-center text-center px-6">
        <div class="max-w-md anim-fade">
          <div
            class="inline-flex items-center justify-center w-12 h-12 rounded-2xl
              bg-gradient-to-br from-brand-light to-brand mb-4 shadow-[0_6px_18px_-6px_rgba(99,102,241,0.4)]"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" fill="none"
              stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
              viewBox="0 0 24 24">
              <circle cx="12" cy="12" r="10"/>
              <polyline points="12 6 12 12 16 14"/>
            </svg>
          </div>
          <h2
            class="text-[18px] font-semibold text-ink-0 dark:text-d-ink-0 tracking-[-0.02em] mb-1.5"
          >${this.label}</h2>
          <p class="text-[13px] text-ink-2 dark:text-d-ink-2 leading-relaxed">
            Coming soon — Phase ${this.phase}.
          </p>
        </div>
      </div>
    `;
  }
}
