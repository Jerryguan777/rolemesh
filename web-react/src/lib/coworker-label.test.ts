// Copied from web/src/services/coworker-label.test.ts @ cf6b0f1; keep in sync manually until workspace extraction.
// Pinned tests for the coworker subtitle formatter.
//
// The contract: never throw on missing data — UI render paths must
// degrade to "show what we know" rather than blanking the row.

import { describe, expect, it } from 'vitest';

import {
  backendLabel,
  coworkerSubtitle,
  modelsByIdMap,
} from './coworker-label.js';
import type { Coworker, Model } from '../api/client.js';

function makeCoworker(overrides: Partial<Coworker> = {}): Coworker {
  return {
    id: 'cw-1',
    tenant_id: 't1',
    name: 'Ops coworker',
    folder: 'ops',
    agent_backend: 'claude',
    status: 'active',
    max_concurrent_containers: 1,
    created_at: '2026-01-01T00:00:00Z',
    ...overrides,
  } as Coworker;
}

function makeModel(overrides: Partial<Model> = {}): Model {
  return {
    id: 'mdl-1',
    provider: 'anthropic',
    model_id: 'claude-opus-4-7',
    model_family: 'claude-opus',
    display_name: 'Claude Opus 4.7',
    is_active: true,
    ...overrides,
  } as Model;
}

describe('backendLabel', () => {
  it('maps known backends to a title-cased display name', () => {
    expect(backendLabel('claude')).toBe('Claude');
    expect(backendLabel('pi')).toBe('Pi');
  });

  it('falls through to the raw slug for unknown values', () => {
    // Future backends print their slug until the map is updated;
    // never throw or return an empty string.
    expect(backendLabel('vertex' as unknown as 'claude')).toBe('vertex');
  });
});

describe('coworkerSubtitle', () => {
  it('renders Backend · Model when both are present', () => {
    const cw = makeCoworker({ agent_backend: 'claude', model_id: 'mdl-1' });
    const map = modelsByIdMap([makeModel({ id: 'mdl-1' })]);
    expect(coworkerSubtitle(cw, map)).toBe('Claude · Claude Opus 4.7');
  });

  it('handles Pi backend symmetrically', () => {
    const cw = makeCoworker({ agent_backend: 'pi', model_id: 'mdl-2' });
    const map = modelsByIdMap([
      makeModel({
        id: 'mdl-2',
        provider: 'openai',
        display_name: 'GPT-4o',
      }),
    ]);
    expect(coworkerSubtitle(cw, map)).toBe('Pi · GPT-4o');
  });

  it('falls back to just the backend when model_id is null', () => {
    // Most pre-v1.1 coworker rows have NULL model_id but still chat
    // fine (the agent reads PI_MODEL_ID from env). Don't surface a
    // "(no model)" hint that misleads users — the backend label
    // alone is honest about what the SPA actually knows.
    const cw = makeCoworker({ model_id: null });
    expect(coworkerSubtitle(cw)).toBe('Claude');
  });

  it('falls back to just the backend when the model lookup misses', () => {
    // Useful when the models endpoint failed but the coworker list
    // succeeded — we still know the backend.
    const cw = makeCoworker({ model_id: 'mdl-deleted' });
    const map = modelsByIdMap([]);
    expect(coworkerSubtitle(cw, map)).toBe('Claude');
  });

  it('never throws when called without a map at all', () => {
    const cw = makeCoworker({ model_id: 'mdl-1' });
    expect(() => coworkerSubtitle(cw)).not.toThrow();
    expect(coworkerSubtitle(cw)).toBe('Claude');
  });
});

describe('modelsByIdMap', () => {
  it('builds a Map keyed by model.id', () => {
    const map = modelsByIdMap([
      makeModel({ id: 'a' }),
      makeModel({ id: 'b', display_name: 'B' }),
    ]);
    expect(map.size).toBe(2);
    expect(map.get('a')?.display_name).toBe('Claude Opus 4.7');
    expect(map.get('b')?.display_name).toBe('B');
  });

  it('returns an empty Map for an empty input', () => {
    expect(modelsByIdMap([]).size).toBe(0);
  });
});
