import { LitElement, html } from 'lit';
import { customElement, property } from 'lit/decorators.js';

/**
 * Frontdesk v1.5 delegation sub-chip — rendered beneath the parent agent's
 * status bar to surface a target container's live progress.
 *
 * Ephemeral: removed from the DOM when the delegation terminates
 * (success / error / safety / timeout). Failure information is
 * delivered through the frontdesk LLM's final reply, not via this chip.
 *
 * Single line, indented, non-interactive — same UX shape as the parent
 * chip (RoleMesh chat-panel.ts: agentStatus bar).
 */
@customElement('rm-child-agent-chip')
export class ChildAgentChip extends LitElement {
  @property() childConversationId = '';
  @property() delegationId = '';
  @property() targetName = '';
  @property() targetFolder = '';
  @property() contextMode = '';
  @property() currentLine = '';

  protected override createRenderRoot() {
    return this;
  }

  override render() {
    const isDev =
      typeof window !== 'undefined' &&
      ((window as unknown as { __DEV_MODE__?: boolean }).__DEV_MODE__ ===
        true);
    return html`
      <div
        class="flex items-center gap-2 ml-6 py-1 text-[11.5px] text-ink-3 dark:text-d-ink-3 select-none"
        data-delegation-id="${this.delegationId}"
        data-child-conv-id="${this.childConversationId}"
      >
        <span class="w-1.5 h-1.5 rounded-full bg-brand-light animate-pulse shrink-0"></span>
        <span class="font-medium text-ink-2 dark:text-d-ink-2 shrink-0">
          ⚙ ${this.targetName || this.targetFolder}
        </span>
        <span class="text-[10px] opacity-60 shrink-0">(internal)</span>
        ${isDev && this.contextMode
          ? html`<span class="text-[10px] opacity-50 shrink-0"
              >[${this.contextMode}]</span
            >`
          : ''}
        <span class="truncate">${this.currentLine}</span>
      </div>
    `;
  }
}
