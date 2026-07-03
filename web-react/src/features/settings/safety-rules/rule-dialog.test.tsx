// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import type { Coworker, SafetyCheck, SafetyRule } from '../../../api/client';
import { RuleDialog } from './rule-dialog';

const piiRegex: SafetyCheck = {
  id: 'pii.regex',
  version: '1',
  stages: ['input_prompt', 'pre_tool_call', 'model_output'],
  cost_class: 'cheap',
  action_model: 'fixed',
  natural_actions: {
    input_prompt: 'block',
    pre_tool_call: 'block',
    model_output: 'block',
  },
  supported_actions: {
    input_prompt: ['allow', 'block', 'require_approval', 'warn'],
    pre_tool_call: ['allow', 'block', 'require_approval', 'warn'],
    model_output: ['allow', 'block', 'require_approval'],
  },
  supported_codes: [],
  config_schema: null,
} as unknown as SafetyCheck;

const presidio: SafetyCheck = {
  id: 'presidio.pii',
  version: '1',
  stages: ['post_tool_result'],
  cost_class: 'slow',
  action_model: 'config_routed',
  natural_actions: { post_tool_result: 'allow' },
  supported_actions: { post_tool_result: ['allow', 'block', 'redact', 'warn'] },
  supported_codes: [],
  config_schema: null,
} as unknown as SafetyCheck;

const domainAllowlist: SafetyCheck = {
  id: 'domain_allowlist',
  version: '1',
  stages: ['pre_tool_call'],
  cost_class: 'cheap',
  action_model: 'fixed',
  natural_actions: { pre_tool_call: 'block' },
  supported_actions: { pre_tool_call: ['allow', 'block', 'warn'] },
  supported_codes: [],
  config_schema: null,
} as unknown as SafetyCheck;

const CHECKS = [piiRegex, presidio, domainAllowlist];
const COWORKERS = [{ id: 'cw-1', name: 'Ops coworker' }] as unknown as Coworker[];

function rule(over: Partial<SafetyRule> = {}): SafetyRule {
  return {
    id: 'r1',
    tenant_id: 't1',
    coworker_id: null,
    stage: 'pre_tool_call',
    check_id: 'pii.regex',
    config: { patterns: { SSN: true } },
    priority: 100,
    enabled: true,
    description: '',
    source: 'tenant',
    tier: null,
    created_at: '2026-06-01T00:00:00Z',
    updated_at: '2026-06-01T00:00:00Z',
    ...over,
  } as SafetyRule;
}

function renderDialog(opts: {
  editing?: SafetyRule | null;
  duplicating?: SafetyRule | null;
  rules?: SafetyRule[];
}) {
  const onClose = vi.fn();
  const onSaved = vi.fn();
  const onDuplicateFromEdit = vi.fn();
  render(
    <RuleDialog
      editing={opts.editing ?? null}
      duplicating={opts.duplicating ?? null}
      checks={CHECKS}
      coworkers={COWORKERS}
      rules={opts.rules ?? []}
      onClose={onClose}
      onSaved={onSaved}
      onDuplicateFromEdit={onDuplicateFromEdit}
    />,
  );
  return { onClose, onSaved, onDuplicateFromEdit };
}

afterEach(cleanup);

describe('RuleDialog — experiences', () => {
  it('fixed check: action panel with natural dot + disabled unsupported actions', () => {
    renderDialog({});
    expect(screen.getByTestId('saf-action-field')).toBeTruthy();
    const seg = document.querySelector('.actseg')!;
    const buttons = [...seg.querySelectorAll('button')] as HTMLButtonElement[];
    const byLabel = (l: string) => buttons.find((b) => b.textContent?.includes(l))!;
    // allow disabled with the disable-the-rule hint (block-natural check).
    expect(byLabel('Allow').disabled).toBe(true);
    expect(byLabel('Allow').title).toMatch(/disable the rule/i);
    // redact never overridable outside presidio.
    expect(byLabel('Redact').disabled).toBe(true);
    // warn + approve pickable on pre_tool_call.
    expect(byLabel('Warn').disabled).toBe(false);
    expect(byLabel('Approve').disabled).toBe(false);
    // natural (Block) carries the default dot.
    expect(byLabel('Block').querySelector('.dot')).toBeTruthy();
  });

  it('config_routed: action field REMOVED, routing table renders instead', () => {
    renderDialog({});
    fireEvent.change(screen.getByLabelText('Check'), {
      target: { value: 'presidio.pii' },
    });
    expect(screen.queryByTestId('saf-action-field')).toBeNull();
    expect(screen.getByTestId('saf-routing')).toBeTruthy();
    expect(screen.getByTestId('saf-routing-inert')).toBeTruthy(); // nothing routed yet
  });

  it('host-list: no action field, hosts textarea IS the rule', () => {
    renderDialog({});
    fireEvent.change(screen.getByLabelText('Check'), {
      target: { value: 'domain_allowlist' },
    });
    expect(screen.queryByTestId('saf-action-field')).toBeNull();
    expect(screen.getByTestId('saf-hosts')).toBeTruthy();
  });

  it('stage select constrained to the check stages + fail-mode note', () => {
    renderDialog({});
    const stageSel = screen.getByLabelText('Where it runs') as HTMLSelectElement;
    expect(stageSel.querySelectorAll('option').length).toBe(3); // piiRegex stages
    expect(screen.getByText(/the call is blocked by default/)).toBeTruthy();
  });

  it('live preview renders the safSentence + priority', () => {
    renderDialog({});
    const pv = screen.getByTestId('saf-preview');
    expect(pv.textContent).toContain('On input'); // default stage = input_prompt
    expect(pv.textContent).toContain('Priority 100');
  });
});

describe('RuleDialog — G3 duplicate detection', () => {
  it('collision auto-flips to editing the existing rule (blue banner + preload)', () => {
    renderDialog({ rules: [rule({ stage: 'input_prompt', priority: 42 })] });
    expect(screen.getByTestId('saf-dup-banner-info')).toBeTruthy();
    expect(screen.getByText('Edit existing rule')).toBeTruthy();
    expect(screen.getByText('Save changes')).toBeTruthy();
    // Existing rule's priority pre-loaded.
    expect((screen.getByLabelText(/Priority/) as HTMLInputElement).value).toBe('42');
    // Scope locked in auto-flip mode.
    expect((screen.getByLabelText('Applies to') as HTMLSelectElement).disabled).toBe(true);
  });

  it('force-create escape → amber banner + defaults reset + return path', () => {
    renderDialog({ rules: [rule({ stage: 'input_prompt', priority: 42 })] });
    fireEvent.click(screen.getByText('Create a separate rule anyway'));
    expect(screen.getByTestId('saf-dup-banner-warn')).toBeTruthy();
    // Title AND save button both read 'Create separate rule'.
    expect(screen.getAllByText('Create separate rule').length).toBe(2);
    expect((screen.getByLabelText(/Priority/) as HTMLInputElement).value).toBe('100');
    expect((screen.getByLabelText('Applies to') as HTMLSelectElement).disabled).toBe(false);
    // Return path re-runs detection → back to the blue banner.
    fireEvent.click(screen.getByText('Switch back to editing the existing one'));
    expect(screen.getByTestId('saf-dup-banner-info')).toBeTruthy();
  });

  it('platform overlap → subtle FYI, no flip', () => {
    renderDialog({ rules: [rule({ stage: 'input_prompt', source: 'platform' })] });
    expect(screen.getByTestId('saf-dup-banner-fyi')).toBeTruthy();
    expect(screen.getByText('New safety rule')).toBeTruthy(); // no flip
    expect(screen.getByText('Create rule')).toBeTruthy();
  });

  it('duplicating a rule does not detect it against itself', () => {
    const src = rule({});
    renderDialog({ duplicating: src, rules: [src] });
    expect(screen.queryByTestId('saf-dup-banner-info')).toBeNull();
    expect(screen.getByText('Duplicate safety rule')).toBeTruthy();
  });

  it('detection skipped entirely in real edit mode', () => {
    const other = rule({ id: 'r2' });
    renderDialog({ editing: rule({}), rules: [other] });
    expect(screen.queryByTestId('saf-dup-banner-info')).toBeNull();
    expect(screen.getByText('Edit safety rule')).toBeTruthy();
  });
});

describe('RuleDialog — scope immutability', () => {
  it('edit mode locks the scope select with the duplicate link-out', () => {
    const { onDuplicateFromEdit } = renderDialog({ editing: rule({}) });
    expect((screen.getByLabelText('Applies to') as HTMLSelectElement).disabled).toBe(true);
    expect(screen.getByTestId('saf-scope-locked')).toBeTruthy();
    fireEvent.click(screen.getByText('duplicate this rule'));
    expect(onDuplicateFromEdit).toHaveBeenCalledWith(expect.objectContaining({ id: 'r1' }));
  });

  it('create + duplicate modes keep scope editable', () => {
    renderDialog({ duplicating: rule({ coworker_id: 'cw-1' }) });
    const sel = screen.getByLabelText('Applies to') as HTMLSelectElement;
    expect(sel.disabled).toBe(false);
    expect(sel.value).toBe('cw-1');
  });
});

describe('RuleDialog — config seeding + advanced JSON', () => {
  it('edit seeds the pii grid from the wire patterns dict', () => {
    renderDialog({ editing: rule({ config: { patterns: { SSN: true, EMAIL: true } } }) });
    const grid = screen.getByTestId('saf-pii-grid');
    const checked = [...grid.querySelectorAll('input:checked')];
    expect(checked.length).toBe(2);
  });

  it('advanced JSON hatch seeds from the current backend-shaped config', () => {
    renderDialog({ editing: rule({ config: { patterns: { SSN: true } } }) });
    fireEvent.click(screen.getByTestId('saf-adv-toggle'));
    const ta = screen.getByTestId('saf-config-json') as HTMLTextAreaElement;
    const parsed = JSON.parse(ta.value);
    expect(parsed.patterns).toEqual({ SSN: true }); // backend shape, not _piiKeys
    // Toggle back to the visual form.
    fireEvent.click(screen.getByTestId('saf-adv-toggle'));
    expect(screen.getByTestId('saf-pii-grid')).toBeTruthy();
  });

  it('G4: empty pii selection blocks the save with a friendly error', () => {
    // config_schema present → sanity checks active.
    const withSchema = {
      ...piiRegex,
      config_schema: { type: 'object' },
    } as SafetyCheck;
    const onSaved = vi.fn();
    render(
      <RuleDialog
        editing={rule({ config: { patterns: {} } })}
        duplicating={null}
        checks={[withSchema, presidio, domainAllowlist]}
        coworkers={COWORKERS}
        rules={[]}
        onClose={vi.fn()}
        onSaved={onSaved}
        onDuplicateFromEdit={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTestId('saf-submit'));
    expect(screen.getByTestId('saf-error-banner').textContent).toMatch(
      /at least one type of personal data/,
    );
    expect(onSaved).not.toHaveBeenCalled();
  });
});
