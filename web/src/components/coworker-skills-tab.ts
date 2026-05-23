// Coworker → skills subtab.
//
// Embedded inside the coworker detail page (design §6.3 C: Overview /
// Skills / MCP / Bindings / Schedules / Conversations). Loads the
// tenant's full catalog + the bindings for this coworker, then
// renders a row per catalog skill with an enable/disable checkbox.
//
// The component reads `coworker-id` as an attribute so the parent
// page can swap it on hash change without unmounting the component:
//
//   <rm-coworker-skills-tab coworker-id=${id}></rm-coworker-skills-tab>
//
// Open-question resolution: NO search/filter input yet. Spec calls
// for one only once a tenant has >50 skills — left to a polish chore
// once we see that scale.

import { LitElement, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';

import { ApiError, getApiClient } from '../api/client.js';
import type {
  CoworkerSkillBinding,
  SkillSummary,
} from '../api/client.js';

@customElement('rm-coworker-skills-tab')
export class CoworkerSkillsTab extends LitElement {
  @property({ type: String, attribute: 'coworker-id' })
  coworkerId = '';

  @state() private skills: SkillSummary[] = [];
  @state() private bindings: Record<string, boolean> = {};
  @state() private loading = false;
  @state() private error: string | null = null;
  @state() private rowError: Record<string, string> = {};
  @state() private rowBusy: Record<string, boolean> = {};

  private readonly api = getApiClient();

  protected override createRenderRoot() {
    return this;
  }

  override connectedCallback() {
    super.connectedCallback();
    if (this.coworkerId) void this.refresh();
  }

  override updated(changed: Map<string, unknown>) {
    if (changed.has('coworkerId') && this.coworkerId) {
      void this.refresh();
    }
  }

  private errMessage(err: unknown): string {
    if (err instanceof ApiError) return err.body?.message ?? `${err.status}`;
    return (err as Error).message;
  }

  private async refresh(): Promise<void> {
    this.loading = true;
    this.error = null;
    try {
      const [skills, bindings] = await Promise.all([
        this.api.listSkills(),
        this.api.listCoworkerSkills(this.coworkerId),
      ]);
      this.skills = skills;
      const map: Record<string, boolean> = {};
      for (const b of bindings as CoworkerSkillBinding[]) {
        map[b.skill_id] = b.enabled;
      }
      this.bindings = map;
    } catch (err) {
      this.skills = [];
      this.bindings = {};
      this.error = this.errMessage(err);
    } finally {
      this.loading = false;
    }
  }

  /** Toggle a binding. Optimistic: flip in-state first, roll back on
   *  failure. Keeps the checkbox feeling instant even on slow links.
   */
  private async toggle(skillId: string, enable: boolean): Promise<void> {
    this.rowBusy = { ...this.rowBusy, [skillId]: true };
    this.rowError = { ...this.rowError, [skillId]: '' };
    const previous = this.bindings[skillId];
    this.bindings = { ...this.bindings, [skillId]: enable };
    try {
      if (enable) {
        await this.api.enableCoworkerSkill(this.coworkerId, skillId);
      } else {
        await this.api.disableCoworkerSkill(this.coworkerId, skillId);
        // The DELETE removes the binding entirely; reflect that as
        // "not bound" in our local state.
        const next = { ...this.bindings };
        delete next[skillId];
        this.bindings = next;
      }
    } catch (err) {
      // Roll back.
      if (previous === undefined) {
        const next = { ...this.bindings };
        delete next[skillId];
        this.bindings = next;
      } else {
        this.bindings = { ...this.bindings, [skillId]: previous };
      }
      this.rowError = { ...this.rowError, [skillId]: this.errMessage(err) };
    } finally {
      this.rowBusy = { ...this.rowBusy, [skillId]: false };
    }
  }

  override render() {
    if (this.loading && this.skills.length === 0) {
      return html`<div class="p-4 text-[13px] text-ink-3 dark:text-d-ink-3">Loading…</div>`;
    }
    if (this.error) {
      return html`<div class="m-4 border border-red-200 dark:border-red-800
        bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300
        text-[13px] px-3 py-2 rounded-lg">${this.error}</div>`;
    }
    if (this.skills.length === 0) {
      return html`<div class="p-6 text-center text-[13px] text-ink-2 dark:text-d-ink-2">
        No skills in this tenant yet. Create one on the
        <a href="#/skills" class="text-brand hover:underline">Skills page</a>.
      </div>`;
    }
    return html`
      <ul class="divide-y divide-surface-3 dark:divide-d-surface-3">
        ${this.skills.map((s) => this.renderRow(s))}
      </ul>
    `;
  }

  private renderRow(s: SkillSummary) {
    const bound = s.id in this.bindings;
    const cellError = this.rowError[s.id] || '';
    const busy = this.rowBusy[s.id] || false;
    return html`
      <li class="px-4 py-2.5 flex items-start gap-3">
        <input
          type="checkbox"
          class="mt-1 cursor-pointer"
          .checked=${bound}
          ?disabled=${busy}
          @change=${(e: Event) =>
            void this.toggle(s.id, (e.target as HTMLInputElement).checked)}
        />
        <div class="min-w-0 flex-1">
          <div class="flex items-baseline gap-2">
            <a
              href=${`#/skills/${encodeURIComponent(s.id)}`}
              class="text-[13.5px] font-medium text-ink-0 dark:text-d-ink-0
                hover:underline truncate"
            >${s.name}</a>
            ${s.enabled
              ? nothing
              : html`<span class="text-[10.5px] uppercase tracking-wide
                  px-1.5 py-0.5 rounded bg-surface-3 dark:bg-d-surface-3
                  text-ink-3 dark:text-d-ink-3"
                  title="The catalog skill is globally disabled — even with
                    this binding the orchestrator will not project it.">
                  catalog-disabled
                </span>`}
          </div>
          ${s.description
            ? html`<div class="text-[12px] text-ink-3 dark:text-d-ink-3 mt-0.5">${s.description}</div>`
            : nothing}
          ${cellError
            ? html`<div class="text-[11.5px] text-red-600 dark:text-red-300 mt-1">${cellError}</div>`
            : nothing}
        </div>
      </li>
    `;
  }
}
