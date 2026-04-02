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
    return this.message.role === 'user' ? this.renderUser() : this.renderAssistant();
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

    return html`
      <div class="mb-5 anim-enter">
        <div class="flex items-start gap-3">
          <div class="shrink-0 w-7 h-7 rounded-full bg-gradient-to-br from-brand-light to-brand flex items-center justify-center mt-0.5 shadow-[0_2px_8px_-2px_rgba(99,102,241,0.3)]">
            <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" fill="none" stroke="white" stroke-width="2.5" viewBox="0 0 24 24">
              <path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/>
            </svg>
          </div>

          <div class="min-w-0 flex-1 pt-0.5">
            <div class="text-[11.5px] font-semibold text-brand uppercase tracking-wide mb-1">Assistant</div>

            ${hasContent ? html`
              <div class="md text-[13.5px] text-ink-1 dark:text-d-ink-1">
                ${unsafeHTML(renderMarkdown(this.message.content))}
                ${this.message.streaming ? html`<span class="inline-block w-[2.5px] h-[15px] bg-brand rounded-full ml-0.5 align-text-bottom" style="animation:blink 1s step-end infinite"></span>` : ''}
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
