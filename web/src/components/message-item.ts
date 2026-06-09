import { LitElement, html } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import { unsafeHTML } from 'lit/directives/unsafe-html.js';
import { renderMarkdown } from '../utils/markdown.js';
import type { ChatMessage } from './chat-panel.js';

@customElement('rm-message-item')
export class MessageItem extends LitElement {
  @property({ attribute: false, hasChanged: () => true }) message!: ChatMessage;

  protected override createRenderRoot() { return this; }

  override render() {
    if (this.message.role === 'user') return this.renderUser();
    if (this.message.role === 'safety') return this.renderSafety();
    return this.renderAssistant();
  }

  private renderSafety() {
    const stage = this.message.safetyStage ?? 'unknown';
    return html`
      <div class="mb-5 anim-enter">
        <div class="flex items-start gap-3">
          <div class="shrink-0 w-7 h-7 rounded-full bg-red-50 dark:bg-red-900/30 border border-red-200 dark:border-red-800 flex items-center justify-center mt-0.5">
            <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24" class="text-red-600 dark:text-red-400">
              <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
              <line x1="12" y1="8" x2="12" y2="12"/>
              <line x1="12" y1="16" x2="12.01" y2="16"/>
            </svg>
          </div>
          <div class="min-w-0 flex-1 pt-0.5">
            <div class="text-[11.5px] font-semibold text-red-600 dark:text-red-400 uppercase tracking-wide mb-1">
              Safety blocked · ${stage}
            </div>
            <div class="text-[13.5px] text-ink-1 dark:text-d-ink-1 leading-relaxed whitespace-pre-wrap">${this.message.content}</div>
          </div>
        </div>
      </div>
    `;
  }

  private renderUser() {
    return html`
      <div class="mb-5 anim-enter">
        <div class="flex items-start gap-3">
          <div class="shrink-0 w-7 h-7 rounded-full bg-surface-2 dark:bg-d-surface-3 flex items-center justify-center mt-0.5">
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24" class="text-ink-2 dark:text-d-ink-2">
              <circle cx="12" cy="8" r="5"/><path d="M20 21a8 8 0 0 0-16 0"/>
            </svg>
          </div>
          <div class="min-w-0 pt-0.5">
            <div class="text-[11.5px] font-semibold text-ink-2 dark:text-d-ink-2 uppercase tracking-wide mb-1">You</div>
            <div class="text-[13.5px] text-ink-0 dark:text-d-ink-0 leading-relaxed whitespace-pre-wrap">${this.message.content}</div>
          </div>
        </div>
      </div>
    `;
  }

  private renderAssistant() {
    const hasContent = this.message.content.trim().length > 0;
    const via = this.message.viaTargetName;

    return html`
      <div class="mb-5 anim-enter">
        <div class="flex items-start gap-3">
          <div
            class="shrink-0 w-7 h-7 rounded-full flex items-center justify-center mt-0.5"
            style="background: var(--rm-accent); box-shadow: var(--rm-shadow-sm);"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" fill="none" stroke="white" stroke-width="2.5" viewBox="0 0 24 24">
              <path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/>
            </svg>
          </div>

          <div class="min-w-0 flex-1 pt-0.5">
            <div class="flex items-center gap-1.5 mb-1">
              <span
                class="text-[11.5px] font-semibold uppercase tracking-wide"
                style="color: var(--rm-accent);"
              >Assistant</span>
              ${via ? html`<span
                data-testid="via-badge"
                class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full bg-surface-2 dark:bg-d-surface-3 text-[10px] font-medium normal-case tracking-normal text-ink-2 dark:text-d-ink-2 border border-surface-3 dark:border-d-surface-3"
                title="Delivered from a delegated specialist"
              >via ${via}</span>` : ''}
            </div>

            ${hasContent ? html`
              <div class="md text-[13.5px] text-ink-1 dark:text-d-ink-1">
                ${unsafeHTML(renderMarkdown(this.message.content))}
                ${this.message.streaming ? html`<span
                  class="inline-block w-[2.5px] h-[15px] rounded-full ml-0.5 align-text-bottom"
                  style="background: var(--rm-accent); animation:blink 1s step-end infinite"
                ></span>` : ''}
              </div>
            ` : ''}

            ${this.message.streaming && !hasContent ? html`
              <div class="flex gap-1 py-1">
                <span class="w-1.5 h-1.5 rounded-full bg-ink-4 dark:bg-d-ink-3" style="animation:dot-bounce 1.4s infinite ease-in-out"></span>
                <span class="w-1.5 h-1.5 rounded-full bg-ink-4 dark:bg-d-ink-3" style="animation:dot-bounce 1.4s infinite ease-in-out 0.2s"></span>
                <span class="w-1.5 h-1.5 rounded-full bg-ink-4 dark:bg-d-ink-3" style="animation:dot-bounce 1.4s infinite ease-in-out 0.4s"></span>
              </div>
            ` : ''}
          </div>
        </div>
      </div>
    `;
  }
}
