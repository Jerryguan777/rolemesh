// Appearance — read-only display of the detected system theme.
//
// Locked decision (plan §13 / v2-A prompt): v2 does NOT introduce a
// theme toggle. The cream/terracotta light palette and the dark
// palette in tokens.css both live under `prefers-color-scheme`. This
// page surfaces that fact so the user knows the UI is following the
// OS, rather than mistaking the lack of a toggle for a missing
// feature.
//
// The component listens to the media query so the line updates live
// when the OS theme changes mid-session. No state is persisted.

import { LitElement, css, html } from 'lit';
import { customElement, state } from 'lit/decorators.js';

@customElement('rm-appearance-page')
export class AppearancePage extends LitElement {
  @state() private prefersDark = false;
  private mql: MediaQueryList | null = null;

  static styles = css`
    :host {
      display: block;
      font-family: var(--rm-font-body);
      color: var(--rm-ink);
      /* Center the card within the settings-shell .ss-body. Padding
       * gives breathing room above/below; margin auto on .card
       * pulls the fixed-width box to the horizontal center. */
      padding: 28px 24px;
    }
    .card {
      border: 1px solid var(--rm-border);
      border-radius: var(--rm-r);
      background: var(--rm-surface);
      padding: 22px 24px;
      max-width: 560px;
      margin: 0 auto;
    }
    h3 {
      margin: 0 0 4px;
      font-size: var(--rm-text-md);
      font-weight: 600;
    }
    p.sub {
      margin: 0 0 12px;
      font-size: var(--rm-text-sm);
      color: var(--rm-ink-3);
    }
    .row {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 10px 12px;
      border: 1px solid var(--rm-border);
      border-radius: var(--rm-radius-sm);
      background: var(--rm-surface-2);
    }
    .row b {
      font-size: 13.5px;
      font-weight: 500;
    }
    .row .pill {
      margin-left: auto;
      font-size: 12px;
      padding: 3px 10px;
      border-radius: 99px;
      background: var(--rm-accent-subtle);
      color: var(--rm-accent-2);
      font-weight: 500;
    }
    .swatch {
      width: 20px;
      height: 20px;
      border-radius: 6px;
      border: 1px solid var(--rm-border);
    }
  `;

  override connectedCallback() {
    super.connectedCallback();
    if (typeof window !== 'undefined' && window.matchMedia) {
      this.mql = window.matchMedia('(prefers-color-scheme: dark)');
      this.prefersDark = this.mql.matches;
      this.mql.addEventListener('change', this.onChange);
    }
  }

  override disconnectedCallback() {
    super.disconnectedCallback();
    if (this.mql) {
      this.mql.removeEventListener('change', this.onChange);
      this.mql = null;
    }
  }

  private onChange = (e: MediaQueryListEvent) => {
    this.prefersDark = e.matches;
  };

  override render() {
    const themeLabel = this.prefersDark ? 'Dark' : 'Light';
    return html`
      <div class="card">
        <h3>Theme</h3>
        <p class="sub">
          RoleMesh follows your operating-system theme. Switch the
          system setting to flip the UI; there is no in-app toggle.
        </p>
        <div class="row">
          <span class="swatch" style="background: var(--rm-accent);"></span>
          <b>${themeLabel} — auto</b>
          <span class="pill">prefers-color-scheme</span>
        </div>
      </div>
    `;
  }
}
