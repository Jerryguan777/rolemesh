import { LitElement, html } from 'lit';
import { customElement } from 'lit/decorators.js';
import './components/chat-panel.js';
import './components/message-list.js';
import './components/message-item.js';
import './components/message-editor.js';
import './components/sidebar.js';

@customElement('rm-app')
export class RmApp extends LitElement {
  protected override createRenderRoot() { return this; }

  override connectedCallback() {
    super.connectedCallback();
    this.style.display = 'block';
    this.style.height = '100%';
  }

  override render() {
    return html`
      <div class="h-full flex flex-col bg-surface-0 dark:bg-d-surface-0">
        <rm-chat-panel class="flex-1 min-h-0"></rm-chat-panel>
      </div>
    `;
  }
}

// Mount
const app = document.getElementById('app');
if (app) {
  app.innerHTML = '<rm-app></rm-app>';
}
