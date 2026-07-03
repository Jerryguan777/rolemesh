import { describe, expect, it } from 'vitest';
import { emptyDraft, isStepValid, type WizardDraft } from './coworker-wizard';

function draft(overrides: Partial<WizardDraft>): WizardDraft {
  return { ...emptyDraft(), ...overrides };
}

// Pins the per-step advance gates (spec C.4 table). Note the deliberate
// divergence from the Lit wizard: the Model step is optional (Backend
// default) — if that gate ever tightens again, this test is the flag.
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

  it('leaves Model optional (Backend default) and Tools/Skills/Review free', () => {
    const d = draft({ modelId: null, mcpServerIds: [], skillIds: [] });
    expect(isStepValid(2, d)).toBe(true);
    expect(isStepValid(3, d)).toBe(true);
    expect(isStepValid(4, d)).toBe(true);
    expect(isStepValid(5, d)).toBe(true);
  });
});
