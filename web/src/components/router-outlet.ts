import { LitElement } from 'lit';
import { customElement, state } from 'lit/decorators.js';

import { matchRoute, type RouteDef } from '../router.js';

// Listens to `hashchange` and re-renders the matched route's body.
// Lit's reactive update cycle is enough on its own — no need for a
// router library at this scale (design §6.1 decision).
@customElement('rm-router-outlet')
export class RouterOutlet extends LitElement {
  @state() private route: RouteDef = matchRoute(location.hash);

  protected override createRenderRoot() {
    return this;
  }

  override connectedCallback() {
    super.connectedCallback();
    window.addEventListener('hashchange', this.onHashChange);
    // Re-resolve in case the URL changed between SSR-like mount and
    // first paint (e.g. token-handoff sequences updating location).
    this.route = matchRoute(location.hash);
  }

  override disconnectedCallback() {
    super.disconnectedCallback();
    window.removeEventListener('hashchange', this.onHashChange);
  }

  private onHashChange = (): void => {
    this.route = matchRoute(location.hash);
  };

  override render() {
    return this.route.render();
  }
}
