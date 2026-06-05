// <rm-safety-decision-detail-dialog> — one safety-log decision in full
// (spec §7.8–7.10). Metadata grid + findings list + the data-minimization
// privacy note. Split from the page like the other v2 dialogs.
//
// This is a DEEP-INSPECTION surface, so it dual-displays stage (friendly +
// mono enum) per §8.5 #3 — the mono form helps engineers root-cause without
// forcing the taxonomy on admins elsewhere.

import { LitElement, html, nothing, type TemplateResult } from 'lit';
import { customElement, property } from 'lit/decorators.js';

import './dialog.js';
import type {
  SafetyDecision,
  SafetyFinding,
  SafetyVerdictAction,
} from '../api/client.js';
import {
  SAF_STAGE_SHORT,
  safActionPillClass,
} from './safety-catalog.js';

/** Severity → finding-pill class. */
export function severityClass(sev: SafetyFinding['severity']): string {
  return `rm-saf-sev-${sev}`;
}

@customElement('rm-safety-decision-detail-dialog')
export class SafetyDecisionDetailDialog extends LitElement {
  @property({ type: Boolean }) open = false;
  @property({ attribute: false }) decision: SafetyDecision | null = null;
  /** Resolved coworker display name, or null for organization-wide. */
  @property() coworkerName: string | null = null;
  /** rule_id → human label, resolved by the page from the loaded rules. */
  @property({ attribute: false }) ruleLabels: Record<string, string> = {};

  protected override createRenderRoot() {
    return this;
  }

  private close = () => {
    this.dispatchEvent(new CustomEvent('close', { bubbles: true, composed: true }));
  };

  private stageDual(stage: string): TemplateResult {
    const friendly = SAF_STAGE_SHORT[stage] ?? stage;
    const cap = friendly.charAt(0).toUpperCase() + friendly.slice(1);
    return html`${cap} <span class="rm-saf-mono">(${stage})</span>`;
  }

  private triggeredRules(): TemplateResult {
    const ids = this.decision?.triggered_rule_ids ?? [];
    if (ids.length === 0) return html`—`;
    return html`${ids.map(
      (id, i) => html`${i > 0 ? ', ' : ''}${this.ruleLabels[id] ?? 'rule'}
        <span class="rm-saf-mono">(${id.slice(0, 8)})</span>`,
    )}`;
  }

  override render(): TemplateResult {
    const d = this.decision;
    return html`
      <rm-dialog
        ?open=${this.open && d !== null}
        title=${d ? `Decision ${d.id.slice(0, 8)}` : 'Decision'}
        width="640px"
        @close=${this.close}
      >
        ${d
          ? html`
              <div class="rm-saf-meta" data-testid="saf-dec-meta">
                <span class="rm-saf-mlabel">When</span>
                <span>${new Date(d.created_at).toLocaleString()}</span>
                <span class="rm-saf-mlabel">Verdict</span>
                <span
                  ><span class="rm-pill ${safActionPillClass(d.verdict_action as SafetyVerdictAction)}"
                    >${d.verdict_action}</span
                  ></span
                >
                <span class="rm-saf-mlabel">Stage</span>
                <span>${this.stageDual(d.stage)}</span>
                <span class="rm-saf-mlabel">Coworker</span>
                <span>${this.coworkerName ?? 'organization-wide'}</span>
                <span class="rm-saf-mlabel">Triggered rule</span>
                <span>${this.triggeredRules()}</span>
                <span class="rm-saf-mlabel">Context digest</span>
                <span class="rm-saf-mono"
                  >${d.context_digest ? `sha256:${d.context_digest}` : '—'}</span
                >
                <span class="rm-saf-mlabel">Summary</span>
                <span>${d.context_summary || '—'}</span>
              </div>

              <div class="rm-saf-section-cap">Findings</div>
              ${(d.findings ?? []).length === 0
                ? html`<p
                    class="text-[12.5px] text-ink-3 dark:text-d-ink-3"
                    style="font-style: italic"
                    data-testid="saf-dec-nofindings"
                  >
                    No findings — check ran and verdict was
                    <b>${d.verdict_action}</b>.
                  </p>`
                : html`<div class="rm-saf-findings">
                    ${(d.findings ?? []).map((f) => this.renderFinding(f))}
                  </div>`}

              <div class="rm-saf-privacy" data-testid="saf-dec-privacy">
                <b>Data minimization</b>
                Raw payload is not stored — only the SHA-256 digest above and
                the short summary. To investigate further, open the conversation
                around ${new Date(d.created_at).toLocaleTimeString()}.
              </div>
            `
          : nothing}

        <div slot="footer" style="display: flex; justify-content: flex-end">
          <button type="button" class="rm-btn rm-btn--secondary" @click=${this.close}>
            Close
          </button>
        </div>
      </rm-dialog>
    `;
  }

  private renderFinding(f: SafetyFinding): TemplateResult {
    const meta = f.metadata ?? {};
    const hasMeta = Object.keys(meta).length > 0;
    return html`
      <div class="rm-saf-finding">
        <div class="rm-saf-fhead">
          <span class="rm-saf-fcode">${f.code}</span>
          <span class="rm-saf-fsev ${severityClass(f.severity)}">${f.severity}</span>
        </div>
        <div class="rm-saf-fmsg">${f.message}</div>
        ${hasMeta
          ? html`<div class="rm-saf-fmeta">${JSON.stringify(meta)}</div>`
          : nothing}
      </div>
    `;
  }
}
