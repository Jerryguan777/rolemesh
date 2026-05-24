// <rm-coworker-edit-dialog> — compact PATCH dialog for an existing
// coworker. Backend exposes 5 mutable fields via CoworkerUpdate:
//
//   * name           — human-readable label
//   * system_prompt  — optional free-form override
//   * model_id       — must reference an active row in the models
//                      catalogue (or stay unchanged)
//   * status         — "active" | "paused" | "disabled"
//   * max_concurrent — 1..20
//
// Why a dedicated dialog instead of an "edit mode" on the 6-step
// creation wizard:
//   1. Edit doesn't need engine selection, slug derivation, partial-
//      commit retry, MCP bindings step — all of which are wizard
//      machinery that doesn't apply post-create.
//   2. A small focused dialog gives the user a fast in-and-out
//      experience; the wizard's modal sized at 860×640 felt like
//      overkill for renaming a coworker.
//   3. Keeps the wizard primitive untouched (locked v2 decision).
//
// On submit: PATCH /api/v1/coworkers/{id}, then emit `coworker-saved`
// so the parent list can refresh. Errors render inline; the dialog
// stays open so the user can retry without re-typing.

import { LitElement, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';

import './dialog.js';
import { ApiError, getApiClient } from '../api/client.js';
import type {
  Coworker,
  CoworkerUpdate,
  Model,
} from '../api/client.js';

type Status = 'active' | 'paused' | 'disabled';

@customElement('rm-coworker-edit-dialog')
export class CoworkerEditDialog extends LitElement {
  @property({ type: Boolean }) open = false;
  /** The coworker being edited. Required when `open=true`; the dialog
   *  reads field values off this object on each open. */
  @property({ attribute: false }) coworker: Coworker | null = null;
  /** Tenant model catalogue, passed in by the parent to avoid a
   *  separate fetch from inside the dialog. Empty array = "no model
   *  selector" (the dialog still renders the other fields). */
  @property({ attribute: false }) models: readonly Model[] = [];

  @state() private form: {
    name: string;
    system_prompt: string;
    model_id: string;
    status: Status;
    max_concurrent: number;
  } = this.emptyForm();
  @state() private busy = false;
  @state() private err: string | null = null;

  private readonly api = getApiClient();

  protected override createRenderRoot() {
    return this;
  }

  override willUpdate(changed: Map<string, unknown>): void {
    // Re-seed the form when transitioning closed → open or when the
    // target coworker changes mid-open. Both can happen if the list
    // hot-refreshes; pinning the seed on the open boundary prevents
    // the user's in-flight typing from being clobbered.
    if (changed.has('open') && this.open && this.coworker) {
      this.form = {
        name: this.coworker.name,
        system_prompt: this.coworker.system_prompt ?? '',
        model_id: this.coworker.model_id ?? '',
        status: this.coworker.status as Status,
        max_concurrent: this.coworker.max_concurrent,
      };
      this.err = null;
      this.busy = false;
    }
  }

  private emptyForm() {
    return {
      name: '',
      system_prompt: '',
      model_id: '',
      status: 'active' as Status,
      max_concurrent: 1,
    };
  }

  private close = () => {
    this.open = false;
    this.dispatchEvent(
      new CustomEvent('close', { bubbles: true, composed: true }),
    );
  };

  /** Build the PATCH body from the form. Only include fields that
   *  ACTUALLY changed — the backend's "absent = leave alone" semantics
   *  let us skip unchanged values and avoid surprising downstream side
   *  effects (e.g. model_id change triggers an orchestrator restart). */
  private buildPatch(): CoworkerUpdate | null {
    if (!this.coworker) return null;
    const patch: CoworkerUpdate = {};
    if (this.form.name.trim() && this.form.name !== this.coworker.name) {
      patch.name = this.form.name.trim();
    }
    const sp = this.form.system_prompt.trim();
    const currentSp = this.coworker.system_prompt ?? '';
    if (sp !== currentSp) {
      patch.system_prompt = sp || null;
    }
    if (this.form.model_id && this.form.model_id !== this.coworker.model_id) {
      patch.model_id = this.form.model_id;
    }
    if (this.form.status !== this.coworker.status) {
      patch.status = this.form.status;
    }
    if (this.form.max_concurrent !== this.coworker.max_concurrent) {
      patch.max_concurrent = this.form.max_concurrent;
    }
    return patch;
  }

  private async save(): Promise<void> {
    if (!this.coworker) return;
    if (!this.form.name.trim()) {
      this.err = 'Name is required.';
      return;
    }
    const patch = this.buildPatch();
    if (!patch || Object.keys(patch).length === 0) {
      // Nothing changed — close without round-tripping the server.
      this.close();
      return;
    }
    this.busy = true;
    this.err = null;
    try {
      const updated = await this.api.updateCoworker(this.coworker.id, patch);
      this.dispatchEvent(
        new CustomEvent<{ coworker: Coworker }>('coworker-saved', {
          detail: { coworker: updated },
          bubbles: true,
          composed: true,
        }),
      );
      this.open = false;
      this.dispatchEvent(
        new CustomEvent('close', { bubbles: true, composed: true }),
      );
    } catch (err) {
      this.err =
        err instanceof ApiError
          ? err.body?.message ?? `${err.status}`
          : (err as Error).message;
    } finally {
      this.busy = false;
    }
  }

  override render() {
    if (!this.coworker) return nothing;
    const title = `Edit coworker: ${this.coworker.name}`;
    return html`
      <rm-dialog
        title=${title}
        ?open=${this.open}
        ?close-on-backdrop=${!this.busy}
        ?close-on-esc=${!this.busy}
        width="480px"
        @close=${this.close}
      >
        <div class="mb-3">
          <label class="block text-[12.5px] font-medium mb-1">Name</label>
          <input
            type="text"
            class="w-full text-[13.5px] px-3 py-2 rounded-md border border-surface-3
              dark:border-d-surface-3 bg-surface-1 dark:bg-d-surface-1
              text-ink-0 dark:text-d-ink-0 focus:outline-none focus:ring-2 focus:ring-brand"
            .value=${this.form.name}
            ?disabled=${this.busy}
            @input=${(e: Event) => {
              this.form = {
                ...this.form,
                name: (e.target as HTMLInputElement).value,
              };
            }}
            data-testid="coworker-edit-name"
          />
        </div>

        <div class="mb-3">
          <label class="block text-[12.5px] font-medium mb-1">
            System prompt
            <span class="font-normal text-ink-3 dark:text-d-ink-3">(optional)</span>
          </label>
          <textarea
            class="w-full text-[13px] px-3 py-2 rounded-md border border-surface-3
              dark:border-d-surface-3 bg-surface-1 dark:bg-d-surface-1
              text-ink-0 dark:text-d-ink-0 focus:outline-none focus:ring-2 focus:ring-brand
              font-mono leading-relaxed resize-y"
            rows="3"
            .value=${this.form.system_prompt}
            ?disabled=${this.busy}
            @input=${(e: Event) => {
              this.form = {
                ...this.form,
                system_prompt: (e.target as HTMLTextAreaElement).value,
              };
            }}
            data-testid="coworker-edit-prompt"
          ></textarea>
        </div>

        ${this.models.length > 0
          ? html`<div class="mb-3">
              <label class="block text-[12.5px] font-medium mb-1">Model</label>
              <select
                class="w-full text-[13.5px] px-3 py-2 rounded-md border border-surface-3
                  dark:border-d-surface-3 bg-surface-1 dark:bg-d-surface-1
                  text-ink-0 dark:text-d-ink-0"
                .value=${this.form.model_id}
                ?disabled=${this.busy}
                @change=${(e: Event) => {
                  this.form = {
                    ...this.form,
                    model_id: (e.target as HTMLSelectElement).value,
                  };
                }}
                data-testid="coworker-edit-model"
              >
                ${this.models.map(
                  (m) => html`<option value=${m.id}>${m.display_name}</option>`,
                )}
              </select>
            </div>`
          : nothing}

        <div class="mb-3 flex items-center gap-3">
          <div class="flex-1">
            <label class="block text-[12.5px] font-medium mb-1">Status</label>
            <select
              class="w-full text-[13.5px] px-3 py-2 rounded-md border border-surface-3
                dark:border-d-surface-3 bg-surface-1 dark:bg-d-surface-1
                text-ink-0 dark:text-d-ink-0"
              .value=${this.form.status}
              ?disabled=${this.busy}
              @change=${(e: Event) => {
                this.form = {
                  ...this.form,
                  status: (e.target as HTMLSelectElement).value as Status,
                };
              }}
              data-testid="coworker-edit-status"
            >
              <option value="active">active</option>
              <option value="paused">paused</option>
              <option value="disabled">disabled</option>
            </select>
          </div>
          <div class="w-32">
            <label class="block text-[12.5px] font-medium mb-1">
              Max concurrent
            </label>
            <input
              type="number"
              min="1"
              max="20"
              class="w-full text-[13.5px] px-3 py-2 rounded-md border border-surface-3
                dark:border-d-surface-3 bg-surface-1 dark:bg-d-surface-1
                text-ink-0 dark:text-d-ink-0 focus:outline-none focus:ring-2 focus:ring-brand"
              .value=${String(this.form.max_concurrent)}
              ?disabled=${this.busy}
              @input=${(e: Event) => {
                const n = Number((e.target as HTMLInputElement).value);
                if (!Number.isNaN(n)) {
                  this.form = { ...this.form, max_concurrent: n };
                }
              }}
              data-testid="coworker-edit-max-concurrent"
            />
          </div>
        </div>

        ${this.err
          ? html`<div
              class="text-[12.5px] text-red-600 dark:text-red-300 mt-2"
              role="alert"
            >${this.err}</div>`
          : nothing}

        <div slot="footer" class="flex items-center gap-2">
          <button
            type="button"
            class="text-[12.5px] px-3 py-1.5 rounded-md border border-surface-3 dark:border-d-surface-3
              text-ink-2 dark:text-d-ink-2 hover:bg-surface-2 dark:hover:bg-d-surface-2 cursor-pointer
              disabled:opacity-60"
            ?disabled=${this.busy}
            @click=${this.close}
          >Cancel</button>
          <button
            type="button"
            class="text-[12.5px] px-3 py-1.5 rounded-md bg-brand text-white
              hover:bg-brand-dark transition-colors cursor-pointer
              disabled:opacity-60"
            ?disabled=${this.busy}
            @click=${() => void this.save()}
            data-testid="coworker-edit-save"
          >${this.busy ? 'Saving…' : 'Save changes'}</button>
        </div>
      </rm-dialog>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'rm-coworker-edit-dialog': CoworkerEditDialog;
  }
}
