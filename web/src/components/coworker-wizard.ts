// <rm-coworker-wizard> — 6-step "+ New coworker" creation flow.
//
// Composition (v2-B locked):
//   - Outer chrome: fixed-position overlay + backdrop. We use a
//     hand-rolled overlay instead of <rm-dialog> because <rm-dialog>
//     wraps a small centred dialog, while the wizard is a large
//     content surface with its own header / rail / footer chrome
//     supplied by <rm-wizard>. Stacking with the (sibling) credential
//     dialog falls out of the native <dialog> top-layer on the
//     credential side.
//   - Inner chrome: <rm-wizard> primitive (v2-A). We pass step list,
//     currentStep, canAdvance, busy, submitLabel; we own the draft.
//   - Step body: a `${renderStep()}` switch in the default slot.
//
// Draft state lives here (the primitive is stateless per v2-A). We
// route GETs through the shared <rm-models-page> grouping helper so
// model + credential cross-reference logic is single-source.
//
// Submit order (locked):
//   1. POST /coworkers — fail → keep wizard open, banner with error,
//      let user retry. Nothing left behind.
//   2. Loop POST /coworkers/{id}/mcp-servers — failure does NOT
//      roll back the coworker. We bank the partial success and tell
//      the user "coworker created, N of M tool bindings failed —
//      retry from the coworker page". Same for skills. This matches
//      the locked decision: partial commit is friendlier than
//      silently dropping a half-built coworker.
//   3. Loop POST /coworkers/{id}/skills/{skillId}.
//   4. On success: redirect via `location.href = ?coworker=<id>` to
//      reuse v2-A's full-reload pattern.
//
// Error feedback (locked): per-field inline red text in Identity;
// banner-style for submit failures.

import { LitElement, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';

import './wizard.js';
import { ApiError, getApiClient } from '../api/client.js';
import type {
  Backend,
  Coworker,
  CoworkerCreate,
  CredentialResponse,
  Model,
  ModelProvider,
  SkillSummary,
  MCPServer,
} from '../api/client.js';
import {
  groupModelsByProvider,
  type ProviderGroup,
} from '../services/models-grouping.js';

/** Lowercase + replace non `[a-z0-9_-]` with `-` + collapse runs of
 *  `-` + trim leading non-alphanumeric (the backend regex demands the
 *  first character be `[A-Za-z0-9]`). Returns empty string if nothing
 *  survives — caller treats that as "name required". */
export function slugify(name: string): string {
  const lowered = name.toLowerCase();
  // Replace any char that's not a-z 0-9 _ - with -.
  const replaced = lowered.replace(/[^a-z0-9_-]+/g, '-');
  // Collapse runs of `-`.
  const collapsed = replaced.replace(/-+/g, '-');
  // Trim leading non-alphanumeric (per backend regex). Numbers OK as
  // first char per `^[a-z0-9][a-z0-9_-]{0,63}$`.
  const trimmed = collapsed.replace(/^[^a-z0-9]+/, '');
  // Trim trailing `-` for tidiness (regex allows trailing `-` but it
  // looks weird as a derived slug).
  return trimmed.replace(/-+$/, '').slice(0, 64);
}

/** Pinned by the backend `CoworkerCreate.folder` regex. */
const SLUG_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;

export function isValidSlug(s: string): boolean {
  return SLUG_RE.test(s);
}

interface Draft {
  name: string;
  /** Operator-supplied override of the auto-derived slug. When null
   *  the wizard shows the auto slug; when non-null this wins. */
  folderOverride: string | null;
  instructions: string;
  agentBackend: Backend['name'] | null;
  modelId: string | null;
  mcpServerIds: string[];
  skillIds: string[];
}

interface SubmitFailure {
  /** Whether the POST /coworkers itself failed (no coworker created). */
  coworkerFailed: boolean;
  /** New coworker id when at least the coworker write succeeded. */
  coworkerId: string | null;
  /** Failed mcp server ids w/ reason. */
  mcpFailures: { id: string; reason: string }[];
  /** Failed skill ids w/ reason. */
  skillFailures: { id: string; reason: string }[];
  /** Top-level message — surfaces in the banner. */
  message: string;
}

@customElement('rm-coworker-wizard')
export class CoworkerWizard extends LitElement {
  @property({ type: Boolean, reflect: true }) open = false;
  /** When set, the wizard runs in **edit mode**:
   *
   *  - Form seeds from this row + the coworker's existing MCP and
   *    skill bindings (fetched on open).
   *  - Title and submit-button label switch to "Edit"/"Save changes".
   *  - Slug (folder) and engine (agent_backend) render as read-only
   *    with a "Cannot be changed after creation" hint — both back
   *    durable filesystem / container state that a rename would
   *    orphan.
   *  - Submit calls PATCH /api/v1/coworkers/{id} for the 5 mutable
   *    fields + diffs MCP / skill bindings into bind/unbind calls.
   *
   *  Pass `null` (default) for the original create flow. */
  @property({ attribute: false }) editing: Coworker | null = null;

  @state() private currentStep = 0;
  @state() private creating = false;
  @state() private submitError: SubmitFailure | null = null;
  /** Snapshot of the bindings at edit-mode open. Used by submit to
   *  compute the bind/unbind diff. */
  @state() private originalMcpServerIds: string[] = [];
  @state() private originalSkillIds: string[] = [];

  // Catalogue state — loaded lazily on first open.
  @state() private backends: Backend[] = [];
  @state() private models: Model[] = [];
  @state() private credentials: CredentialResponse[] = [];
  @state() private mcpServers: MCPServer[] = [];
  @state() private skills: SkillSummary[] = [];
  @state() private cataloguesLoaded = false;
  @state() private catalogueError: string | null = null;

  @state() private draft: Draft = this.emptyDraft();
  // Per-field error strings, surfaced inline.
  @state() private fieldErrors: Record<string, string> = {};

  private readonly api = getApiClient();

  protected override createRenderRoot() {
    return this;
  }

  // Steps must stay in this order (Required reading §3 — design doc
  // pinned the 6-step list).
  private readonly steps = [
    { id: 'identity', label: 'Identity' },
    { id: 'engine',   label: 'Engine' },
    { id: 'model',    label: 'Model' },
    { id: 'tools',    label: 'Tools' },
    { id: 'skills',   label: 'Skills' },
    { id: 'review',   label: 'Review' },
  ];

  private emptyDraft(): Draft {
    return {
      name: '',
      folderOverride: null,
      instructions: '',
      agentBackend: null,
      modelId: null,
      mcpServerIds: [],
      skillIds: [],
    };
  }

  override willUpdate(changed: Map<string, unknown>) {
    if (changed.has('open') && this.open) {
      if (!this.cataloguesLoaded) void this.loadCatalogues();
      // Edit mode: seed the draft from the target row + its current
      // MCP / skill bindings. Done on EACH open transition so a stale
      // post-edit row doesn't get reused if the parent reopens with a
      // different target. Falls through to emptyDraft when `editing`
      // is null (create flow).
      if (this.editing) {
        void this.seedFromEditing(this.editing);
      }
    }
    if (changed.has('open') && !this.open) {
      // Closing — reset so the next open starts fresh.
      this.draft = this.emptyDraft();
      this.currentStep = 0;
      this.fieldErrors = {};
      this.submitError = null;
      this.originalMcpServerIds = [];
      this.originalSkillIds = [];
    }
  }

  private async seedFromEditing(cw: Coworker): Promise<void> {
    // Pre-fill the simple fields immediately so the wizard has the
    // right header / step-1 content even before bindings come back.
    this.draft = {
      name: cw.name,
      // The folder string IS the immutable slug — surface it via the
      // override field so renderIdentity shows it; isValidSlug guards
      // already-stored slugs (backend regex was looser pre-v1.1).
      folderOverride: cw.folder,
      instructions: cw.system_prompt ?? '',
      agentBackend: cw.agent_backend,
      modelId: cw.model_id ?? null,
      mcpServerIds: [],
      skillIds: [],
    };
    // Fetch bindings in parallel; failures degrade to empty so the
    // wizard remains usable (the user can still edit identity / model).
    const [mcpResult, skillResult] = await Promise.allSettled([
      this.api.listCoworkerMCPServers(cw.id),
      this.api.listCoworkerSkills(cw.id),
    ]);
    const mcpIds =
      mcpResult.status === 'fulfilled'
        ? mcpResult.value.map((b) => b.mcp_server_id)
        : [];
    const skillIds =
      skillResult.status === 'fulfilled'
        ? skillResult.value
            .filter((b) => (b as { enabled?: boolean }).enabled !== false)
            .map((b) => b.skill_id)
        : [];
    this.draft = {
      ...this.draft,
      mcpServerIds: mcpIds,
      skillIds: skillIds,
    };
    this.originalMcpServerIds = [...mcpIds];
    this.originalSkillIds = [...skillIds];
  }

  private async loadCatalogues(): Promise<void> {
    this.catalogueError = null;
    try {
      const [backends, models, creds, mcp, skills] = await Promise.all([
        this.api.getBackends(),
        this.api.listModels(),
        this.api.listCredentials().catch(() => [] as CredentialResponse[]),
        this.api.listMCPServers().catch(() => [] as MCPServer[]),
        this.api.listSkills().catch(() => [] as SkillSummary[]),
      ]);
      this.backends = backends;
      this.models = models;
      this.credentials = creds;
      this.mcpServers = mcp;
      this.skills = skills;
      this.cataloguesLoaded = true;
    } catch (err) {
      this.catalogueError =
        err instanceof ApiError ? err.message : (err as Error).message;
    }
  }

  /** Public: callers (e.g. credential-dialog) can ask the wizard to
   *  refresh credentials after an inline credential add. */
  async refreshCredentials(): Promise<void> {
    try {
      this.credentials = await this.api.listCredentials();
    } catch {
      // Surface failure inline — the wizard model step header will
      // still show the previous state.
    }
  }

  /** Public: refresh MCP server list (after inline server add). */
  async refreshMCPServers(): Promise<void> {
    try {
      this.mcpServers = await this.api.listMCPServers();
    } catch {
      // ignored — keep previous list
    }
  }

  /** Public: refresh skills (after inline skill add). */
  async refreshSkills(): Promise<void> {
    try {
      this.skills = await this.api.listSkills();
    } catch {
      // ignored
    }
  }

  // -----------------------------------------------------------------
  // Derived values
  // -----------------------------------------------------------------

  private get selectedBackend(): Backend | null {
    return (
      this.backends.find((b) => b.name === this.draft.agentBackend) ?? null
    );
  }

  private get selectedModel(): Model | null {
    if (!this.draft.modelId) return null;
    return this.models.find((m) => m.id === this.draft.modelId) ?? null;
  }

  private get derivedSlug(): string {
    return slugify(this.draft.name);
  }

  private get effectiveSlug(): string {
    return this.draft.folderOverride ?? this.derivedSlug;
  }

  private get modelGroups(): ProviderGroup[] {
    return groupModelsByProvider(
      this.models,
      this.credentials,
      this.selectedBackend,
    );
  }

  private providerHasCredential(provider: ModelProvider): boolean {
    return this.credentials.some((c) => c.provider === provider);
  }

  // -----------------------------------------------------------------
  // canAdvance per step (locked: per-field inline errors elsewhere)
  // -----------------------------------------------------------------

  private isStepValid(step: number): boolean {
    switch (step) {
      case 0: {
        // Identity
        if (this.draft.name.trim() === '') return false;
        if (!isValidSlug(this.effectiveSlug)) return false;
        return true;
      }
      case 1: // Engine
        return this.draft.agentBackend !== null;
      case 2: {
        // Model — model picked + provider has credential + model
        // survives the backend filter (the grouping helper already
        // does that, but we double-check the chosen model is in the
        // visible set).
        const m = this.selectedModel;
        if (!m) return false;
        if (!this.providerHasCredential(m.provider)) return false;
        const allGrouped = this.modelGroups.flatMap((g) => g.models);
        return allGrouped.some((x) => x.id === m.id);
      }
      case 3: // Tools — optional, advance always
        return true;
      case 4: // Skills — optional, advance always
        return true;
      case 5: // Review
        return true;
      default:
        return false;
    }
  }

  private get canAdvance(): boolean {
    return this.isStepValid(this.currentStep);
  }

  // -----------------------------------------------------------------
  // Submit
  // -----------------------------------------------------------------

  private async handleSubmit(): Promise<void> {
    if (this.creating) return;
    this.creating = true;
    this.submitError = null;

    // Branch on mode. Edit goes through PATCH + binding diffs; create
    // goes through the original POST + sequential binding loop.
    let coworker: Coworker | null = null;
    if (this.editing) {
      try {
        coworker = await this.api.updateCoworker(this.editing.id, {
          name: this.draft.name.trim(),
          model_id: this.draft.modelId ?? undefined,
          system_prompt: this.draft.instructions.trim() || null,
        });
      } catch (err) {
        this.submitError = {
          coworkerFailed: true,
          coworkerId: null,
          mcpFailures: [],
          skillFailures: [],
          message:
            err instanceof ApiError
              ? `${err.status} — ${err.message}`
              : (err as Error).message,
        };
        this.creating = false;
        return;
      }
    } else {
      try {
        const body: CoworkerCreate = {
          name: this.draft.name.trim(),
          folder: this.effectiveSlug,
          agent_backend: this.draft.agentBackend!,
          model_id: this.draft.modelId,
          system_prompt: this.draft.instructions.trim() || null,
          // max_concurrent / agent_role intentionally not surfaced
          // (locked decisions #10 / #13 — backend default applies).
          max_concurrent: 2,
        };
        coworker = await this.api.createCoworker(body);
      } catch (err) {
        this.submitError = {
          coworkerFailed: true,
          coworkerId: null,
          mcpFailures: [],
          skillFailures: [],
          message:
            err instanceof ApiError
              ? `${err.status} — ${err.message}`
              : (err as Error).message,
        };
        this.creating = false;
        return;
      }
    }

    // Coworker created OR updated. Compute the binding plan: in edit
    // mode it's a diff (only the deltas), in create mode it's all of
    // the draft's selections. Partial failures DON'T roll back.
    const isEdit = this.editing !== null;
    const oldMcp = new Set(this.originalMcpServerIds);
    const newMcp = new Set(this.draft.mcpServerIds);
    const mcpToAdd = isEdit
      ? this.draft.mcpServerIds.filter((id) => !oldMcp.has(id))
      : this.draft.mcpServerIds;
    const mcpToRemove = isEdit
      ? this.originalMcpServerIds.filter((id) => !newMcp.has(id))
      : [];

    const oldSkill = new Set(this.originalSkillIds);
    const newSkill = new Set(this.draft.skillIds);
    const skillToAdd = isEdit
      ? this.draft.skillIds.filter((id) => !oldSkill.has(id))
      : this.draft.skillIds;
    const skillToRemove = isEdit
      ? this.originalSkillIds.filter((id) => !newSkill.has(id))
      : [];

    const mcpFailures: { id: string; reason: string }[] = [];
    for (const id of mcpToAdd) {
      try {
        await this.api.bindCoworkerMCPServer(coworker.id, {
          mcp_server_id: id,
          // null = all tools enabled (locked decision #9).
          enabled_tools: null,
        });
      } catch (err) {
        mcpFailures.push({
          id,
          reason:
            err instanceof ApiError ? err.message : (err as Error).message,
        });
      }
    }
    for (const id of mcpToRemove) {
      try {
        await this.api.unbindCoworkerMCPServer(coworker.id, id);
      } catch (err) {
        mcpFailures.push({
          id,
          reason:
            err instanceof ApiError ? err.message : (err as Error).message,
        });
      }
    }

    const skillFailures: { id: string; reason: string }[] = [];
    for (const id of skillToAdd) {
      try {
        await this.api.enableCoworkerSkill(coworker.id, id);
      } catch (err) {
        skillFailures.push({
          id,
          reason:
            err instanceof ApiError ? err.message : (err as Error).message,
        });
      }
    }
    for (const id of skillToRemove) {
      try {
        await this.api.disableCoworkerSkill(coworker.id, id);
      } catch (err) {
        skillFailures.push({
          id,
          reason:
            err instanceof ApiError ? err.message : (err as Error).message,
        });
      }
    }

    this.creating = false;

    if (mcpFailures.length || skillFailures.length) {
      // Partial success — keep wizard open so the user can see what
      // happened. They can retry bindings from the coworker page.
      const total = mcpFailures.length + skillFailures.length;
      const verb = isEdit ? 'updated' : 'created';
      this.submitError = {
        coworkerFailed: false,
        coworkerId: coworker.id,
        mcpFailures,
        skillFailures,
        message: `Coworker ${verb}, but ${total} binding${total > 1 ? 's' : ''} failed. You can finish wiring it up from the coworker page.`,
      };
      // Notify so the page list can refresh — same event in both modes
      // (the `partial:true` flag already tells the parent it's recoverable
      // state). The parent reads `editing` itself if it needs to branch.
      this.dispatchEvent(
        new CustomEvent<{ coworkerId: string; partial: true }>('coworker-created', {
          detail: { coworkerId: coworker.id, partial: true },
          bubbles: true,
          composed: true,
        }),
      );
      return;
    }

    // Full success.
    this.dispatchEvent(
      new CustomEvent<{ coworkerId: string; partial: false }>('coworker-created', {
        detail: { coworkerId: coworker.id, partial: false },
        bubbles: true,
        composed: true,
      }),
    );
    if (isEdit) {
      // Edit success: just close the wizard. Parent (coworkers-page)
      // refreshes its list via @coworker-created. No need to navigate
      // away because the user is already looking at the right page.
      this.open = false;
      this.dispatchEvent(
        new CustomEvent('close', { bubbles: true, composed: true }),
      );
      return;
    }
    // Create success — redirect to the new coworker chat. Reuses
    // v2-A's location.href reload pattern (acceptable per refresh
    // notes; full fix is a v3 chore).
    const params = new URLSearchParams(location.search);
    params.set('agent_id', coworker.id);
    params.delete('chat_id');
    location.href = `${location.pathname}?${params.toString()}#/`;
  }

  // -----------------------------------------------------------------
  // Render
  // -----------------------------------------------------------------

  private onStepChange(e: CustomEvent<{ step: number }>) {
    this.currentStep = e.detail.step;
  }

  private close() {
    this.open = false;
    this.dispatchEvent(
      new CustomEvent('close', { bubbles: true, composed: true }),
    );
  }

  override render() {
    if (!this.open) return nothing;
    return html`
      <div
        class="fixed inset-0 z-[80] bg-black/45 flex items-center justify-center p-4"
        @click=${(e: MouseEvent) => {
          if (e.target === e.currentTarget) this.close();
        }}
      >
        <div
          class="w-full max-w-[860px] h-[640px] max-h-[90vh] bg-[var(--rm-surface)]
            rounded-2xl shadow-2xl overflow-hidden flex flex-col"
          role="dialog"
          aria-modal="true"
          aria-label="New coworker wizard"
        >
          <!-- v2-B Finding moved the partial-commit banner here. It
               used to render inside the step body, which meant a user
               who fixed the failure and re-navigated could scroll the
               banner off-screen. Pinning it above the wizard primitive
               keeps it visible regardless of step. The <rm-wizard>
               primitive API stays untouched (locked decision). -->
          ${this.submitError ? this.renderSubmitError() : nothing}
          <rm-wizard
            title=${this.editing
              ? `Edit coworker: ${this.editing.name}`
              : 'New coworker'}
            .steps=${this.steps}
            .currentStep=${this.currentStep}
            .canAdvance=${this.canAdvance}
            .busy=${this.creating}
            submit-label=${this.creating
              ? (this.editing ? 'Saving…' : 'Creating…')
              : (this.editing ? 'Save changes' : 'Create coworker')}
            @step-change=${this.onStepChange}
            @submit=${this.handleSubmit}
            @close=${this.close}
          >
            ${this.renderBody()}
          </rm-wizard>
        </div>
      </div>
    `;
  }

  private renderBody() {
    if (this.catalogueError) {
      return html`<div
        class="border border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-900/20
          text-red-700 dark:text-red-300 text-[13px] px-3 py-2 rounded-lg"
      >Failed to load catalogues: ${this.catalogueError}</div>`;
    }
    if (!this.cataloguesLoaded) {
      return html`<div class="text-[13px] text-ink-3 dark:text-d-ink-3">
        Loading…
      </div>`;
    }
    return this.renderStepBody();
  }

  private renderSubmitError() {
    const err = this.submitError!;
    // Sits ABOVE <rm-wizard> (see render()) — no top margin needed.
    // Rounded only at the bottom because the dialog wrapper already
    // owns the top corners.
    return html`
      <div
        class="border-b border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-900/20
          text-red-700 dark:text-red-300 text-[13px] px-4 py-2"
        role="alert"
        data-testid="coworker-wizard-submit-error"
      >
        <div class="font-medium mb-1">${err.message}</div>
        ${err.mcpFailures.length
          ? html`<div class="text-[12px] mt-1">
              Tool bindings failed: ${err.mcpFailures.map((f) => f.id).join(', ')}
            </div>`
          : nothing}
        ${err.skillFailures.length
          ? html`<div class="text-[12px] mt-1">
              Skills failed: ${err.skillFailures.map((f) => f.id).join(', ')}
            </div>`
          : nothing}
      </div>
    `;
  }

  private renderStepBody() {
    switch (this.currentStep) {
      case 0: return this.renderIdentity();
      case 1: return this.renderEngine();
      case 2: return this.renderModel();
      case 3: return this.renderTools();
      case 4: return this.renderSkills();
      case 5: return this.renderReview();
      default: return nothing;
    }
  }

  private renderIdentity() {
    const slug = this.effectiveSlug;
    const usingOverride = this.draft.folderOverride !== null;
    const nameErr = this.fieldErrors.name ?? '';
    const slugErr =
      this.draft.name.trim() === ''
        ? ''
        : !isValidSlug(slug)
          ? 'Slug must start with a letter or digit and may contain a–z, 0–9, _ or -.'
          : '';
    return html`
      <h3 class="text-[15px] font-semibold mb-1">Identity</h3>
      <p class="text-[13px] text-ink-3 dark:text-d-ink-3 mb-4">
        Who is this coworker and what's its job?
      </p>
      <div class="mb-3">
        <label class="block text-[12.5px] font-medium mb-1">Name</label>
        <input
          type="text"
          class="w-full text-[13.5px] px-3 py-2 rounded-md border border-surface-3
            dark:border-d-surface-3 bg-surface-1 dark:bg-d-surface-1
            text-ink-0 dark:text-d-ink-0 focus:outline-none focus:ring-2 focus:ring-brand"
          placeholder="e.g. Marketing coworker"
          .value=${this.draft.name}
          @input=${(e: Event) => {
            this.draft = {
              ...this.draft,
              name: (e.target as HTMLInputElement).value,
            };
          }}
        />
        ${nameErr
          ? html`<div class="text-[12px] text-red-600 dark:text-red-300 mt-1">${nameErr}</div>`
          : html`<div class="text-[12px] text-ink-3 dark:text-d-ink-3 mt-1">
              Slug:
              <code class="font-mono">${slug || '(name required)'}</code>
              ${usingOverride
                ? html`<button
                    type="button"
                    class="ml-2 underline text-brand"
                    @click=${() => {
                      this.draft = { ...this.draft, folderOverride: null };
                    }}
                  >reset to auto</button>`
                : nothing}
            </div>`}
        ${slugErr
          ? html`<div class="text-[12px] text-red-600 dark:text-red-300 mt-1">${slugErr}</div>`
          : nothing}
      </div>

      ${this.editing
        ? html`<div class="mb-3 text-[12.5px] text-ink-3 dark:text-d-ink-3">
            <span class="font-medium">Slug:</span>
            <code class="font-mono ml-1">${this.editing.folder}</code>
            <span class="ml-2">— immutable (container folder is tied to this name).</span>
          </div>`
        : html`<details class="mb-3">
            <summary class="text-[12.5px] text-ink-2 dark:text-d-ink-2 cursor-pointer">
              Advanced — override slug
            </summary>
            <div class="mt-2">
              <input
                type="text"
                class="w-full font-mono text-[13px] px-3 py-2 rounded-md border border-surface-3
                  dark:border-d-surface-3 bg-surface-1 dark:bg-d-surface-1
                  text-ink-0 dark:text-d-ink-0 focus:outline-none focus:ring-2 focus:ring-brand"
                placeholder=${this.derivedSlug || 'marketing-helper'}
                .value=${this.draft.folderOverride ?? ''}
                @input=${(e: Event) => {
                  const v = (e.target as HTMLInputElement).value;
                  this.draft = {
                    ...this.draft,
                    folderOverride: v === '' ? null : v,
                  };
                }}
              />
            </div>
          </details>`}

      <div>
        <label class="block text-[12.5px] font-medium mb-1">Instructions</label>
        <textarea
          rows="5"
          class="w-full text-[13.5px] px-3 py-2 rounded-md border border-surface-3
            dark:border-d-surface-3 bg-surface-1 dark:bg-d-surface-1
            text-ink-0 dark:text-d-ink-0 focus:outline-none focus:ring-2 focus:ring-brand"
          placeholder="Describe how this coworker should behave, its goals, its tone…"
          .value=${this.draft.instructions}
          @input=${(e: Event) => {
            this.draft = {
              ...this.draft,
              instructions: (e.target as HTMLTextAreaElement).value,
            };
          }}
        ></textarea>
      </div>
    `;
  }

  private renderEngine() {
    return html`
      <h3 class="text-[15px] font-semibold mb-1">Engine</h3>
      <p class="text-[13px] text-ink-3 dark:text-d-ink-3 mb-4">
        The framework that runs this coworker — it also decides which models you can use.
      </p>
      ${this.editing
        ? html`<div class="mb-3 text-[12.5px] text-ink-3 dark:text-d-ink-3">
            Engine is fixed at <code class="font-mono">${this.editing.agent_backend}</code> for the lifetime
            of a coworker — changing the runtime would orphan its sessions.
            Delete + recreate if you really need a different one.
          </div>`
        : nothing}
      <div class="space-y-3">
        ${this.backends.map((b) => {
          const selected = this.draft.agentBackend === b.name;
          const isLocked = this.editing !== null;
          return html`
            <button
              type="button"
              class="w-full text-left border rounded-lg px-4 py-3 transition
                ${isLocked && !selected ? 'opacity-40 cursor-not-allowed' : 'cursor-pointer'}
                ${selected
                  ? 'border-brand bg-brand/5 dark:bg-brand/10'
                  : 'border-surface-3 dark:border-d-surface-3 hover:bg-surface-2 dark:hover:bg-d-surface-2'}"
              ?disabled=${isLocked}
              @click=${() => {
                if (this.editing) return;
                this.draft = {
                  ...this.draft,
                  agentBackend: b.name,
                  // Reset modelId — the previous model may not fit the
                  // new backend's compatibility matrix.
                  modelId: null,
                };
              }}
            >
              <div class="flex items-center gap-3">
                <span
                  class="w-4 h-4 rounded-full border ${selected
                    ? 'border-brand bg-brand'
                    : 'border-surface-3 dark:border-d-surface-3'}"
                ></span>
                <div class="flex-1 min-w-0">
                  <div class="text-[14px] font-medium capitalize">${b.name}</div>
                  <div class="text-[12px] text-ink-3 dark:text-d-ink-3 mt-0.5">
                    ${b.description}
                  </div>
                  <div class="text-[11.5px] text-ink-4 mt-1">
                    Providers: ${b.supported_providers.join(', ')} ·
                    Families: ${b.supported_model_families
                      ? b.supported_model_families.join(', ')
                      : 'any'}
                  </div>
                </div>
              </div>
            </button>
          `;
        })}
      </div>
    `;
  }

  private renderModel() {
    const groups = this.modelGroups;
    return html`
      <h3 class="text-[15px] font-semibold mb-1">Model</h3>
      <p class="text-[13px] text-ink-3 dark:text-d-ink-3 mb-4">
        The model this coworker thinks with. The list reflects the engine you chose.
      </p>
      ${groups.length === 0
        ? html`<div class="text-[13px] text-ink-3 dark:text-d-ink-3">
            No models compatible with this engine.
          </div>`
        : groups.map((g) => this.renderProviderGroup(g))}
    `;
  }

  private renderProviderGroup(group: ProviderGroup) {
    return html`
      <section
        class="border rounded-lg overflow-hidden mb-3
          ${group.hasCredential
            ? 'border-surface-3 dark:border-d-surface-3'
            : 'border-amber-300 dark:border-amber-700'}"
      >
        <header
          class="px-3 py-2 flex items-center gap-2 text-[13px] font-medium
            ${group.hasCredential
              ? 'bg-surface-2 dark:bg-d-surface-2'
              : 'bg-amber-50 dark:bg-amber-900/20 text-amber-800 dark:text-amber-200'}"
        >
          <span class="capitalize">${group.provider}</span>
          ${group.hasCredential
            ? nothing
            : html`
                <span class="text-[11.5px] font-normal">
                  needs ${group.provider} credential
                </span>
                <button
                  type="button"
                  class="ml-auto text-[12px] underline text-amber-800 dark:text-amber-200"
                  @click=${() => this.requestCredential(group.provider)}
                >+ Add credential</button>
              `}
        </header>
        <ul class="divide-y divide-surface-3 dark:divide-d-surface-3">
          ${group.models.map((m) => this.renderModelRow(m, group.hasCredential))}
        </ul>
      </section>
    `;
  }

  private renderModelRow(m: Model, hasCredential: boolean) {
    const selected = this.draft.modelId === m.id;
    const inactive = !m.is_active;
    const disabled = inactive || !hasCredential;
    return html`
      <li>
        <button
          type="button"
          class="w-full text-left px-3 py-2 flex items-center gap-3
            ${selected ? 'bg-brand/10' : 'hover:bg-surface-2 dark:hover:bg-d-surface-2'}
            ${disabled ? 'opacity-60 cursor-not-allowed' : 'cursor-pointer'}"
          ?disabled=${disabled}
          title=${inactive
            ? 'This model is marked inactive in the catalogue.'
            : !hasCredential
              ? `Add a ${m.provider} credential to use this model.`
              : ''}
          @click=${() => {
            if (disabled) return;
            this.draft = { ...this.draft, modelId: m.id };
          }}
        >
          <span
            class="w-3 h-3 rounded-full border
              ${selected
                ? 'border-brand bg-brand'
                : 'border-surface-3 dark:border-d-surface-3'}"
          ></span>
          <div class="flex-1 min-w-0">
            <div class="text-[13.5px]">${m.display_name}</div>
            <div class="text-[11.5px] text-ink-3 dark:text-d-ink-3 font-mono truncate">
              ${m.model_id}
            </div>
          </div>
          <span class="text-[11.5px] text-ink-3 dark:text-d-ink-3">${m.model_family}</span>
          ${inactive
            ? html`<span
                class="text-[11px] px-1.5 py-0.5 rounded bg-amber-100 dark:bg-amber-900/40
                  text-amber-800 dark:text-amber-200"
              >inactive</span>`
            : nothing}
        </button>
      </li>
    `;
  }

  private requestCredential(provider: ModelProvider) {
    // The credential dialog is a sibling — emit and let the page
    // host it. We deliberately do NOT mount the dialog inside the
    // wizard tree (locked decision #3).
    this.dispatchEvent(
      new CustomEvent<{ provider: ModelProvider }>('request-credential', {
        detail: { provider },
        bubbles: true,
        composed: true,
      }),
    );
  }

  private renderTools() {
    return html`
      <h3 class="text-[15px] font-semibold mb-1">Tools</h3>
      <p class="text-[13px] text-ink-3 dark:text-d-ink-3 mb-4">
        Bind MCP servers this coworker can call. Whole-server binding
        only here — per-tool selection lives on the coworker page.
      </p>
      ${this.mcpServers.length === 0
        ? html`<div class="text-[13px] text-ink-3 dark:text-d-ink-3">
            No MCP servers yet — connect one to bind.
          </div>`
        : html`<div class="border border-surface-3 dark:border-d-surface-3 rounded-lg overflow-hidden">
            ${this.mcpServers.map((s) => this.renderToolRow(s))}
          </div>`}
      <button
        type="button"
        class="mt-3 text-[12.5px] px-3 py-1.5 rounded-md border border-surface-3
          dark:border-d-surface-3 text-ink-2 dark:text-d-ink-2
          hover:bg-surface-2 dark:hover:bg-d-surface-2 cursor-pointer"
        @click=${() => this.requestAddMCPServer()}
      >+ Connect a new server</button>
    `;
  }

  private renderToolRow(s: MCPServer) {
    const checked = this.draft.mcpServerIds.includes(s.id);
    return html`
      <label
        class="flex items-center gap-3 px-3 py-2 border-b border-surface-3
          dark:border-d-surface-3 last:border-b-0 cursor-pointer
          hover:bg-surface-2 dark:hover:bg-d-surface-2"
      >
        <input
          type="checkbox"
          class="rm-checkbox"
          .checked=${checked}
          @change=${(e: Event) => this.toggleMcp(s.id, (e.target as HTMLInputElement).checked)}
        />
        <div class="flex-1 min-w-0">
          <div class="text-[13.5px]">${s.name}</div>
          <div class="text-[11.5px] text-ink-3 dark:text-d-ink-3">
            ${s.type} · ${s.auth_mode}
          </div>
        </div>
      </label>
    `;
  }

  private toggleMcp(id: string, on: boolean) {
    const set = new Set(this.draft.mcpServerIds);
    if (on) set.add(id);
    else set.delete(id);
    this.draft = { ...this.draft, mcpServerIds: [...set] };
  }

  private requestAddMCPServer() {
    this.dispatchEvent(
      new CustomEvent('request-add-mcp-server', {
        bubbles: true,
        composed: true,
      }),
    );
  }

  private renderSkills() {
    return html`
      <h3 class="text-[15px] font-semibold mb-1">Skills</h3>
      <p class="text-[13px] text-ink-3 dark:text-d-ink-3 mb-4">
        Bind skill packages that give this coworker extra know-how.
      </p>
      ${this.skills.length === 0
        ? html`<div class="text-[13px] text-ink-3 dark:text-d-ink-3">
            No skills available. Create one from Settings → Skills.
          </div>`
        : html`<div class="border border-surface-3 dark:border-d-surface-3 rounded-lg overflow-hidden">
            ${this.skills.map((s) => this.renderSkillRow(s))}
          </div>`}
    `;
  }

  private renderSkillRow(s: SkillSummary) {
    const checked = this.draft.skillIds.includes(s.id);
    return html`
      <label
        class="flex items-center gap-3 px-3 py-2 border-b border-surface-3
          dark:border-d-surface-3 last:border-b-0 cursor-pointer
          hover:bg-surface-2 dark:hover:bg-d-surface-2"
      >
        <input
          type="checkbox"
          class="rm-checkbox"
          .checked=${checked}
          @change=${(e: Event) => this.toggleSkill(s.id, (e.target as HTMLInputElement).checked)}
        />
        <div class="flex-1 min-w-0">
          <div class="text-[13.5px]">${s.name}</div>
          ${s.description
            ? html`<div class="text-[11.5px] text-ink-3 dark:text-d-ink-3 truncate">
                ${s.description}
              </div>`
            : nothing}
        </div>
      </label>
    `;
  }

  private toggleSkill(id: string, on: boolean) {
    const set = new Set(this.draft.skillIds);
    if (on) set.add(id);
    else set.delete(id);
    this.draft = { ...this.draft, skillIds: [...set] };
  }

  private renderReview() {
    const backend = this.selectedBackend;
    const model = this.selectedModel;
    return html`
      <h3 class="text-[15px] font-semibold mb-1">Review</h3>
      <p class="text-[13px] text-ink-3 dark:text-d-ink-3 mb-4">
        Confirm and create. You can edit most fields later.
      </p>
      <dl class="text-[13.5px] space-y-2">
        ${row('Name', this.draft.name || html`<em class="text-ink-3">(empty)</em>`)}
        ${row('Slug', html`<code class="font-mono">${this.effectiveSlug}</code>`)}
        ${row('Engine', backend?.name ?? '—')}
        ${row(
          'Model',
          model
            ? html`${model.display_name}
                <span class="text-ink-3 dark:text-d-ink-3 font-mono">
                  (${model.model_id})
                </span>`
            : '—',
        )}
        ${row('Tools', `${this.draft.mcpServerIds.length} bound`)}
        ${row('Skills', `${this.draft.skillIds.length} bound`)}
        ${row(
          'Instructions',
          this.draft.instructions.trim() === ''
            ? html`<em class="text-ink-3">(none)</em>`
            : html`<span class="whitespace-pre-wrap">${this.draft.instructions.trim()}</span>`,
        )}
      </dl>
    `;
  }
}

function row(label: string, value: unknown) {
  return html`
    <div class="flex gap-3">
      <dt class="w-28 text-ink-3 dark:text-d-ink-3 shrink-0">${label}</dt>
      <dd class="flex-1">${value}</dd>
    </div>
  `;
}
