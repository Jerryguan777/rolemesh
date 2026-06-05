// @vitest-environment happy-dom
// Safety decision detail dialog (spec §7.8–7.10) — metadata grid, findings,
// the data-minimization privacy note, and the rule_id → label mapping.

import { afterEach, describe, expect, it } from 'vitest';

// Side-effect import registers the element (the named import is type-only).
import './safety-decision-detail-dialog.js';
import { severityClass } from './safety-decision-detail-dialog.js';
import type { SafetyDecisionDetailDialog } from './safety-decision-detail-dialog.js';
import type { SafetyDecision } from '../api/client.js';

function makeDecision(over: Partial<SafetyDecision> = {}): SafetyDecision {
  return {
    id: 'dec-abc123',
    tenant_id: 't1',
    coworker_id: 'ops',
    conversation_id: null,
    job_id: null,
    stage: 'pre_tool_call',
    verdict_action: 'block',
    triggered_rule_ids: ['sr-1'],
    findings: [
      { code: 'PII.SSN', severity: 'high', message: 'Detected SSN', metadata: { position: 87 } },
    ],
    context_digest: 'a4f3',
    context_summary: 'erp.refund args contained SSN',
    source: 'tenant',
    created_at: '2026-06-03T14:23:08Z',
    ...over,
  } as SafetyDecision;
}

async function mount(
  decision: SafetyDecision | null,
  props: Partial<SafetyDecisionDetailDialog> = {},
): Promise<SafetyDecisionDetailDialog> {
  const el = document.createElement(
    'rm-safety-decision-detail-dialog',
  ) as SafetyDecisionDetailDialog;
  document.body.appendChild(el);
  await el.updateComplete;
  el.decision = decision;
  Object.assign(el, props);
  el.open = true;
  await el.updateComplete;
  await el.updateComplete;
  return el;
}

const $ = <T extends Element>(el: Element, s: string): T | null =>
  el.querySelector<T>(s);

describe('severityClass', () => {
  it('maps each severity to its finding-pill class', () => {
    expect(severityClass('critical')).toBe('rm-saf-sev-critical');
    expect(severityClass('high')).toBe('rm-saf-sev-high');
    expect(severityClass('medium')).toBe('rm-saf-sev-medium');
    expect(severityClass('low')).toBe('rm-saf-sev-low');
    expect(severityClass('info')).toBe('rm-saf-sev-info');
  });
});

describe('SafetyDecisionDetailDialog', () => {
  afterEach(() => {
    document.body.innerHTML = '';
  });

  it('renders metadata, the digest with a sha256 prefix, and the coworker name', async () => {
    const el = await mount(makeDecision(), {
      coworkerName: 'Ops coworker',
      ruleLabels: { 'sr-1': 'Personal data (regex)' },
    });
    const meta = $(el, '[data-testid="saf-dec-meta"]')!;
    expect(meta.textContent).toContain('Ops coworker');
    expect(meta.textContent).toContain('sha256:a4f3');
    // Stage dual-displays friendly + mono enum (deep-inspection surface).
    expect(meta.textContent).toContain('Before tool calls');
    expect(meta.textContent).toContain('(pre_tool_call)');
    // Triggered rule shows the resolved label + short id.
    expect(meta.textContent).toContain('Personal data (regex)');
    el.remove();
  });

  it('renders findings with code, severity pill, message, and metadata', async () => {
    const el = await mount(makeDecision());
    const finding = $(el, '.rm-saf-finding')!;
    expect($(finding, '.rm-saf-fcode')?.textContent?.trim()).toBe('PII.SSN');
    expect($(finding, '.rm-saf-fsev')?.classList.contains('rm-saf-sev-high')).toBe(true);
    expect($(finding, '.rm-saf-fmsg')?.textContent).toContain('Detected SSN');
    expect($(finding, '.rm-saf-fmeta')?.textContent).toContain('position');
    el.remove();
  });

  it('shows a no-findings note for an allow decision', async () => {
    const el = await mount(makeDecision({ verdict_action: 'allow', findings: [] }));
    expect($(el, '[data-testid="saf-dec-nofindings"]')).not.toBeNull();
    expect($(el, '.rm-saf-finding')).toBeNull();
    el.remove();
  });

  it('always renders the data-minimization privacy note', async () => {
    const el = await mount(makeDecision());
    const note = $(el, '[data-testid="saf-dec-privacy"]')!;
    expect(note.textContent).toMatch(/raw payload is not stored/i);
    el.remove();
  });

  it('falls back to organization-wide when no coworker name is given', async () => {
    const el = await mount(makeDecision({ coworker_id: null }), { coworkerName: null });
    expect($(el, '[data-testid="saf-dec-meta"]')?.textContent).toContain(
      'organization-wide',
    );
    el.remove();
  });
});
