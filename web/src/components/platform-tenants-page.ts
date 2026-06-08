// <rm-platform-tenants-page> — the platform-plane Tenants page (RBAC
// UI spec §4 / §5.x). Lists every tenant on the platform, lets the
// operator provision a new one, and suspend / resume existing ones.
//
// Only reached from <rm-platform-shell> when the caller holds
// `platform.tenant.manage` (gated at the nav level). This page owns no
// authorization of its own — the backend enforces every action; per-row
// buttons are a UX courtesy. The `__platform__` sentinel tenant answers
// 403 on suspend; we surface that error inline rather than special-casing
// it client-side (so the frontend keeps no copy of backend rules).
//
// Reads/writes go through the typed v1 ApiClient (D1: client.ts gained
// listTenants / provisionTenant / suspendTenant / resumeTenant). Look and
// feel reuse the shared settings-pages.css classes (rm-spane / rm-card /
// rm-pill / …) so the page matches credentials-page / safety-rules-page.
// `--rm-` tokens only — no literal colors or fonts.

import { LitElement, html, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';

import { ApiError, getApiClient } from '../api/client.js';
import type {
  PlatformTenantProvision,
  PlatformTenantResponse,
} from '../api/client.js';
import './dialog.js';
import { iconPlus } from './icons.js';

@customElement('rm-platform-tenants-page')
export class PlatformTenantsPage extends LitElement {
  @state() private rows: PlatformTenantResponse[] = [];
  @state() private loading = true;
  @state() private listError: string | null = null;

  /** Per-tenant suspend/resume in-flight + error, keyed by tenant id so
   *  one row's failure never blocks the others. */
  @state() private rowBusy: Record<string, boolean> = {};
  @state() private rowError: Record<string, string> = {};

  /** Provision dialog state. */
  @state() private dialogOpen = false;
  @state() private form: PlatformTenantProvision = { name: '', slug: '' };
  @state() private provisionBusy = false;
  @state() private provisionError: string | null = null;

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
    this.listError = null;
    try {
      this.rows = await this.api.listTenants();
    } catch (err) {
      this.rows = [];
      this.listError = this.errMessage(err);
    } finally {
      this.loading = false;
    }
  }

  private errMessage(err: unknown): string {
    if (err instanceof ApiError) {
      return err.body?.message ?? `HTTP ${err.status}`;
    }
    return (err as Error)?.message ?? 'unknown error';
  }

  /** Suspend or resume a single tenant, then refresh the list. The verb
   *  is chosen from the row's CURRENT status by the caller; we never
   *  infer it from a role. A 403 (e.g. the `__platform__` sentinel)
   *  surfaces inline on the row. */
  private async toggleStatus(row: PlatformTenantResponse): Promise<void> {
    if (this.rowBusy[row.id]) return;
    this.rowBusy = { ...this.rowBusy, [row.id]: true };
    this.rowError = { ...this.rowError, [row.id]: '' };
    try {
      if (row.status === 'active') {
        await this.api.suspendTenant(row.id);
      } else {
        await this.api.resumeTenant(row.id);
      }
      await this.refresh();
    } catch (err) {
      this.rowError = { ...this.rowError, [row.id]: this.errMessage(err) };
    } finally {
      this.rowBusy = { ...this.rowBusy, [row.id]: false };
    }
  }

  private openProvision(): void {
    this.form = { name: '', slug: '' };
    this.provisionError = null;
    this.provisionBusy = false;
    this.dialogOpen = true;
  }

  private closeProvision = (): void => {
    if (this.provisionBusy) return;
    this.dialogOpen = false;
  };

  private async submitProvision(): Promise<void> {
    const name = this.form.name.trim();
    if (!name) {
      this.provisionError = 'Name is required.';
      return;
    }
    // Slug is optional; send it only when the operator typed one so the
    // server can derive a default. An empty string is omitted (not sent
    // as ''), matching the `slug?` shape of PlatformTenantProvision.
    const slug = (this.form.slug ?? '').trim();
    const body: PlatformTenantProvision = slug ? { name, slug } : { name };
    this.provisionBusy = true;
    this.provisionError = null;
    try {
      await this.api.provisionTenant(body);
      this.dialogOpen = false;
      await this.refresh();
    } catch (err) {
      this.provisionError = this.errMessage(err);
    } finally {
      this.provisionBusy = false;
    }
  }

  override render() {
    return html`
      <div class="rm-spane">
        <div class="rm-ch">
          <h2>Tenants</h2>
          <button
            type="button"
            class="rm-add"
            data-testid="provision-tenant"
            @click=${() => this.openProvision()}
          >
            ${iconPlus(14)}
            Provision tenant
          </button>
        </div>
        <p class="rm-sub">
          Every tenant on the platform. Provision a new one, or suspend /
          resume existing tenants. Suspended tenants cannot run agents.
        </p>

        ${this.loading
          ? html`<div class="rm-banner-loading" data-testid="tenants-loading">
              Loading…
            </div>`
          : this.listError
            ? html`<div class="rm-banner-err" data-testid="tenants-error">
                ${this.listError}
              </div>`
            : this.rows.length === 0
              ? html`<div class="rm-empty" data-testid="tenants-empty">
                  <div class="rm-empty-title">No tenants yet</div>
                  <div>Provision the first tenant to get started.</div>
                </div>`
              : html`${this.rows.map((r) => this.renderRow(r))}`}

        ${this.renderProvisionDialog()}
      </div>
    `;
  }

  private renderRow(row: PlatformTenantResponse) {
    const active = row.status === 'active';
    const busy = !!this.rowBusy[row.id];
    const err = this.rowError[row.id] || '';
    const initials = row.name.slice(0, 2).toUpperCase();
    return html`
      <div class="rm-card" data-tenant-id=${row.id} data-testid="tenant-row">
        <span class="rm-ic">${initials}</span>
        <span class="rm-mn">
          <b>${row.name}</b>
          <span>${row.slug ? row.slug : row.id}</span>
        </span>
        <span
          class=${`rm-pill ${active ? 'rm-pill-on' : 'rm-pill-bad'}`}
          data-testid="tenant-status"
          >${row.status}</span
        >
        <span class="rm-row-acts">
          <button
            type="button"
            class=${`rm-btn ${active ? 'rm-btn--danger' : 'rm-btn--primary'}`}
            data-testid=${active ? 'tenant-suspend' : 'tenant-resume'}
            ?disabled=${busy}
            @click=${() => void this.toggleStatus(row)}
          >
            ${busy
              ? active
                ? 'Suspending…'
                : 'Resuming…'
              : active
                ? 'Suspend'
                : 'Resume'}
          </button>
        </span>
        ${err
          ? html`<div class="rm-row-error" data-testid="tenant-row-error">
              ${err}
            </div>`
          : nothing}
      </div>
    `;
  }

  private renderProvisionDialog() {
    return html`
      <rm-dialog
        title="Provision tenant"
        ?open=${this.dialogOpen}
        ?close-on-backdrop=${!this.provisionBusy}
        ?close-on-esc=${!this.provisionBusy}
        width="440px"
        @close=${this.closeProvision}
      >
        <div class="mb-3">
          <label class="block text-[12.5px] font-medium mb-1">Name</label>
          <input
            type="text"
            data-testid="provision-name"
            class="w-full text-[13.5px] px-3 py-2 rounded-md border border-surface-3
              dark:border-d-surface-3 bg-surface-1 dark:bg-d-surface-1
              text-ink-0 dark:text-d-ink-0 focus:outline-none focus:ring-2 focus:ring-brand"
            placeholder="e.g. Acme Corp"
            .value=${this.form.name}
            @input=${(e: Event) => {
              this.form = {
                ...this.form,
                name: (e.target as HTMLInputElement).value,
              };
            }}
            ?disabled=${this.provisionBusy}
          />
        </div>
        <div class="mb-3">
          <label class="block text-[12.5px] font-medium mb-1"
            >Slug <span class="text-ink-3 dark:text-d-ink-3">(optional)</span></label
          >
          <input
            type="text"
            data-testid="provision-slug"
            class="w-full text-[13.5px] px-3 py-2 rounded-md border border-surface-3
              dark:border-d-surface-3 bg-surface-1 dark:bg-d-surface-1
              text-ink-0 dark:text-d-ink-0 focus:outline-none focus:ring-2 focus:ring-brand
              font-mono"
            placeholder="auto-derived from name if blank"
            .value=${this.form.slug ?? ''}
            @input=${(e: Event) => {
              this.form = {
                ...this.form,
                slug: (e.target as HTMLInputElement).value,
              };
            }}
            ?disabled=${this.provisionBusy}
          />
        </div>

        ${this.provisionError
          ? html`<div
              class="text-[12.5px] text-red-600 dark:text-red-300 mt-2"
              role="alert"
              data-testid="provision-error"
            >
              ${this.provisionError}
            </div>`
          : nothing}

        <div
          slot="footer"
          style="display: flex; gap: 8px; justify-content: flex-end;"
        >
          <button
            type="button"
            class="rm-btn rm-btn--secondary"
            ?disabled=${this.provisionBusy}
            @click=${this.closeProvision}
          >
            Cancel
          </button>
          <button
            type="button"
            class="rm-btn rm-btn--primary"
            data-testid="provision-submit"
            ?disabled=${this.provisionBusy}
            @click=${() => void this.submitProvision()}
          >
            ${this.provisionBusy ? 'Provisioning…' : 'Provision'}
          </button>
        </div>
      </rm-dialog>
    `;
  }
}
