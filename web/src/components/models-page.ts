// Read-only Models catalog (#/models).
//
// Lists `GET /api/v1/models` grouped by provider, augmented with
// the tenant's `GET /credentials` so each provider header
// shows ready / needs-credential status. The grouping logic is in
// `services/models-grouping.ts` so the v2-B coworker wizard and
// this page share one source of truth.

import { LitElement, html } from 'lit';
import { customElement, state } from 'lit/decorators.js';

import { ApiError, getApiClient } from '../api/client.js';
import type {
  CredentialResponse,
  Model,
  ModelProvider,
} from '../api/client.js';
import {
  groupModelsByProvider,
  type ProviderGroup,
} from '../services/models-grouping.js';
import './credential-dialog.js';

@customElement('rm-models-page')
export class ModelsPage extends LitElement {
  @state() private rows: Model[] = [];
  @state() private credentials: CredentialResponse[] = [];
  @state() private loading = true;
  @state() private error: string | null = null;
  @state() private credDialogOpen = false;
  @state() private credDialogProvider: ModelProvider | null = null;
  private readonly api = getApiClient();

  protected override createRenderRoot() {
    return this;
  }

  override connectedCallback() {
    super.connectedCallback();
    void this.refresh();
  }

  private async refresh(): Promise<void> {
    this.loading = true;
    this.error = null;
    try {
      // Credentials are tenant-scoped metadata only; failing to
      // load them should not blank the catalog — degrade gracefully.
      const [models, creds] = await Promise.all([
        this.api.listModels(),
        this.api.listCredentials().catch(() => [] as CredentialResponse[]),
      ]);
      this.rows = models;
      this.credentials = creds;
    } catch (err) {
      this.rows = [];
      this.credentials = [];
      this.error =
        err instanceof ApiError
          ? `${err.status} — ${err.message}`
          : (err as Error).message ?? 'unknown error';
    } finally {
      this.loading = false;
    }
  }

  override render() {
    return html`
      <div class="rm-spane">
        <div class="rm-ch">
          <h2>Models</h2>
          <button
            type="button"
            class="rm-add"
            @click=${() => {
              this.credDialogProvider = null;
              this.credDialogOpen = true;
            }}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
              stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="M12 5v14M5 12h14"/>
            </svg>
            Add credential
          </button>
        </div>
        <p class="rm-sub">
          Models are grouped by provider. A provider's models become
          usable once its credential is set.
        </p>

        ${this.loading
          ? html`<div class="rm-banner-loading">Loading…</div>`
          : this.error
            ? html`<div class="rm-banner-err">${this.error}</div>`
            : this.rows.length === 0
              ? this.renderEmpty()
              : this.renderGroups()}

        <rm-credential-dialog
          ?open=${this.credDialogOpen}
          .provider=${this.credDialogProvider}
          @close=${() => { this.credDialogOpen = false; }}
          @credential-saved=${() => { void this.refresh(); }}
        ></rm-credential-dialog>
      </div>
    `;
  }

  private renderEmpty() {
    return html`
      <div class="rm-empty">
        <span class="rm-empty-title">No models available</span>
        The platform catalog appears empty — check the
        <code>models</code> table.
      </div>
    `;
  }

  private renderGroups() {
    const groups = groupModelsByProvider(this.rows, this.credentials);
    return html`
      ${groups.map((g) => this.renderProviderGroup(g))}
    `;
  }

  private renderProviderGroup(group: ProviderGroup) {
    return html`
      <div class="rm-provgrp">
        <div class="rm-provhd">
          <b>${group.provider}</b>
          ${group.hasCredential
            ? html`<span class="rm-credstate rm-credstate--ok">credential set</span>`
            : html`<span class="rm-credstate rm-credstate--miss">no credential</span>
                   <button
                     type="button"
                     class="rm-connectbtn"
                     @click=${() => {
                       this.credDialogProvider = group.provider;
                       this.credDialogOpen = true;
                     }}
                   >Connect</button>`}
        </div>
        ${group.models.map((m) => this.renderModelCard(m, group.hasCredential))}
      </div>
    `;
  }

  private renderModelCard(m: Model, hasCredential: boolean): unknown {
    const dim = !m.is_active || !hasCredential;
    const initials = m.display_name
      .replace(/[^A-Za-z0-9 ]/g, '')
      .split(/\s+/)
      .map((w) => w[0])
      .join('')
      .slice(0, 2)
      .toUpperCase() || '?';
    return html`
      <div class=${`rm-card ${dim ? 'rm-card--dim' : ''}`}>
        <span class="rm-ic">${initials}</span>
        <span class="rm-mn">
          <b>${m.display_name}</b>
          <span>${m.model_id} · ${m.model_family}</span>
        </span>
        ${!m.is_active
          ? html`<span class="rm-pill rm-pill-warn">inactive</span>`
          : hasCredential
            ? html`<span class="rm-pill rm-pill-on">ready</span>`
            : html`<span class="rm-pill rm-pill-off">needs credential</span>`}
      </div>
    `;
  }
}
