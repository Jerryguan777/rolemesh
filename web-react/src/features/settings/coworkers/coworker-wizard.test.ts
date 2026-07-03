import { describe, expect, it } from 'vitest';
import { emptyDraft, isStepValid, type WizardDraft } from './coworker-wizard';
import type { ProviderGroup } from '../../../lib/models-grouping';
import type { Model } from '../../../api/client';

function draft(overrides: Partial<WizardDraft>): WizardDraft {
  return { ...emptyDraft(), ...overrides };
}

function model(id: string, isActive = true): Model {
  return {
    id,
    provider: 'anthropic',
    model_id: id,
    model_family: 'claude',
    display_name: id,
    is_active: isActive,
  } as Model;
}

function group(hasCredential: boolean, ids: string[]): ProviderGroup {
  return {
    provider: 'anthropic',
    hasCredential,
    credentialUpdatedAt: null,
    models: ids.map((id) => model(id)),
  };
}

function groupWith(hasCredential: boolean, models: Model[]): ProviderGroup {
  return { provider: 'anthropic', hasCredential, credentialUpdatedAt: null, models };
}

// Pins the per-step advance gates (spec C.4 table, D-C4 resolved:
// Lit parity — the Model step is REQUIRED, no "Backend default" card).
describe('isStepValid', () => {
  it('gates Identity on non-empty name AND a valid slug', () => {
    expect(isStepValid(0, draft({}))).toBe(false);
    expect(isStepValid(0, draft({ name: 'Mira', folder: '' }))).toBe(false);
    expect(isStepValid(0, draft({ name: 'Mira', folder: 'Bad Slug' }))).toBe(false);
    expect(isStepValid(0, draft({ name: 'Mira', folder: 'mira' }))).toBe(true);
  });

  it('gates Engine on a backend pick', () => {
    expect(isStepValid(1, draft({}))).toBe(false);
    expect(isStepValid(1, draft({ backend: 'claude' }))).toBe(true);
  });

  it('Model REQUIRES a pick — a null model (incl. legacy edit seeds) blocks', () => {
    const groups = [group(true, ['m-1'])];
    expect(isStepValid(2, draft({ modelId: null }), groups)).toBe(false);
  });

  it('Model requires the provider to be credentialed', () => {
    const d = draft({ modelId: 'm-1' });
    expect(isStepValid(2, d, [group(false, ['m-1'])])).toBe(false);
    expect(isStepValid(2, d, [group(true, ['m-1'])])).toBe(true);
  });

  it('Model must be in the backend-filtered visible set', () => {
    // e.g. engine switched and the old pick fell out of the groups.
    const d = draft({ modelId: 'm-gone' });
    expect(isStepValid(2, d, [group(true, ['m-1'])])).toBe(false);
  });

  it('Model must be ACTIVE — an inactive pick blocks (F.4 usable predicate)', () => {
    const d = draft({ modelId: 'm-dead' });
    // credentialed group but the picked model is inactive → blocked.
    expect(isStepValid(2, d, [groupWith(true, [model('m-dead', false)])])).toBe(false);
    // same model active → allowed.
    expect(isStepValid(2, d, [groupWith(true, [model('m-dead', true)])])).toBe(true);
  });

  it('leaves Tools/Skills/Review free', () => {
    const d = draft({ mcpServerIds: [], skillIds: [] });
    expect(isStepValid(3, d)).toBe(true);
    expect(isStepValid(4, d)).toBe(true);
    expect(isStepValid(5, d)).toBe(true);
  });
});
