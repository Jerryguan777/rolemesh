// <rm-combobox> — a text input with a styled suggestion dropdown that still
// accepts free text. Mirrors the prototype's policy server/tool pickers
// (.hitl-ui/prototype.html §830): a bordered box with a chevron and a clean
// popup panel — NOT a native <select> (which would force a choice) nor a
// <datalist> (whose OS-native popup looks out of place). Typing filters the
// suggestions; picking one fills the field; but any value can be typed, so a
// server/tool that isn't connected yet can still be entered.
//
// Light DOM (createRenderRoot → this) so the host dialog's Tailwind utilities
// apply, matching the surrounding form fields' palette.

import { LitElement, html, nothing, type TemplateResult } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';

/** Fired on every value change (typing or picking a suggestion). */
export interface ComboboxChangeDetail {
  value: string;
}

@customElement('rm-combobox')
export class Combobox extends LitElement {
  @property() value = '';
  /** Suggestion list. Suggestions only — never a constraint on the value. */
  @property({ attribute: false }) options: string[] = [];
  @property() placeholder = '';
  @property({ type: Boolean }) disabled = false;
  /** Monospace the field + options (used for the tool-name picker). */
  @property({ type: Boolean }) mono = false;
  /** Applied as the inner <input>'s data-testid so tests/automation target it. */
  @property() testid = '';

  @state() private open = false;
  @state() private highlight = -1;
  /** Set while we programmatically refocus the input after a pick, so the
   *  focus handler doesn't immediately reopen the menu we just closed. */
  private suppressFocusOpen = false;

  protected override createRenderRoot() {
    return this;
  }

  override disconnectedCallback(): void {
    super.disconnectedCallback();
    document.removeEventListener('pointerdown', this.onDocPointerDown, true);
  }

  /** Close when a pointer goes down outside this element. */
  private readonly onDocPointerDown = (e: PointerEvent): void => {
    if (!this.contains(e.target as Node)) this.closeMenu();
  };

  private emit(value: string): void {
    this.value = value;
    this.dispatchEvent(
      new CustomEvent<ComboboxChangeDetail>('change', {
        detail: { value },
        bubbles: true,
        composed: true,
      }),
    );
  }

  /** Visible suggestions: de-duped, case-insensitive substring match on the
   *  current value. An empty value shows everything. */
  private filtered(): string[] {
    const seen = new Set<string>();
    const uniq = this.options.filter((o) => {
      if (!o || seen.has(o)) return false;
      seen.add(o);
      return true;
    });
    const q = this.value.trim().toLowerCase();
    if (!q) return uniq;
    return uniq.filter((o) => o.toLowerCase().includes(q));
  }

  private openMenu(): void {
    if (this.disabled || this.open) return;
    this.open = true;
    document.addEventListener('pointerdown', this.onDocPointerDown, true);
  }

  private closeMenu(): void {
    this.open = false;
    this.highlight = -1;
    document.removeEventListener('pointerdown', this.onDocPointerDown, true);
  }

  private input(): HTMLInputElement | null {
    return this.querySelector('input');
  }

  private onInput(e: Event): void {
    this.emit((e.target as HTMLInputElement).value);
    this.highlight = -1;
    this.openMenu();
  }

  private pick(opt: string): void {
    this.emit(opt);
    this.closeMenu();
    // Keep focus for continued keyboard use, but don't let the focus handler
    // reopen the menu we just closed.
    this.suppressFocusOpen = true;
    this.input()?.focus();
    this.suppressFocusOpen = false;
  }

  private onFocus(): void {
    if (this.suppressFocusOpen) return;
    this.openMenu();
  }

  private onKeydown(e: KeyboardEvent): void {
    const opts = this.filtered();
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      this.openMenu();
      this.highlight = Math.min(this.highlight + 1, opts.length - 1);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      this.highlight = Math.max(this.highlight - 1, 0);
    } else if (e.key === 'Enter') {
      if (this.open && this.highlight >= 0 && opts[this.highlight]) {
        e.preventDefault();
        this.pick(opts[this.highlight]);
      }
    } else if (e.key === 'Escape') {
      if (this.open) {
        e.preventDefault();
        this.closeMenu();
      }
    }
  }

  override render(): TemplateResult {
    const opts = this.open ? this.filtered() : [];
    return html`
      <div class="relative">
        <input
          type="text"
          class="w-full text-[13.5px] pl-3 pr-8 py-2 rounded-md border
            border-surface-3 dark:border-d-surface-3 bg-surface-1 dark:bg-d-surface-1
            text-ink-0 dark:text-d-ink-0 focus:outline-none focus:ring-2
            focus:ring-brand ${this.mono ? 'font-mono' : ''}"
          placeholder=${this.placeholder}
          autocomplete="off"
          .value=${this.value}
          ?disabled=${this.disabled}
          data-testid=${this.testid || nothing}
          @input=${this.onInput}
          @focus=${this.onFocus}
          @keydown=${this.onKeydown}
        />
        <button
          type="button"
          tabindex="-1"
          class="absolute right-2 top-1/2 -translate-y-1/2 text-ink-3
            dark:text-d-ink-3 cursor-pointer disabled:cursor-not-allowed"
          aria-label="Toggle suggestions"
          ?disabled=${this.disabled}
          @click=${() => {
            if (this.open) this.closeMenu();
            else {
              this.openMenu();
              this.input()?.focus();
            }
          }}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
            stroke="currentColor" stroke-width="2" stroke-linecap="round"
            stroke-linejoin="round"><path d="M6 9l6 6 6-6" /></svg>
        </button>
        ${this.open && opts.length
          ? html`<div
              class="absolute z-50 left-0 right-0 mt-1 max-h-56 overflow-auto
                rounded-md border border-surface-3 dark:border-d-surface-3
                bg-surface-1 dark:bg-d-surface-1 shadow-lg py-1"
              role="listbox"
              data-testid=${this.testid ? `${this.testid}-menu` : nothing}
            >
              ${opts.map(
                (o, i) => html`<div
                  class="px-3 py-1.5 text-[13px] cursor-pointer text-ink-1
                    dark:text-d-ink-1 ${this.mono ? 'font-mono' : ''} ${i ===
                  this.highlight
                    ? 'bg-surface-2 dark:bg-d-surface-2'
                    : 'hover:bg-surface-2 dark:hover:bg-d-surface-2'}"
                  role="option"
                  data-combobox-option=${o}
                  @mousedown=${(e: Event) => {
                    // mousedown (not click) so it fires before the input blur.
                    e.preventDefault();
                    this.pick(o);
                  }}
                >
                  ${o}
                </div>`,
              )}
            </div>`
          : nothing}
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'rm-combobox': Combobox;
  }
}
