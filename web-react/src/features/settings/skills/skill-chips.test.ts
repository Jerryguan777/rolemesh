import { afterEach, describe, expect, it } from 'vitest';
import { chipEmptyCopy, chipMatches } from './skill-chips';
import { setMe } from '../../../lib/capabilities';
import type { Me, SkillSummary } from '../../../api/client';

function me(userId = 'u1'): Me {
  return { user_id: userId, tenant_id: 't1', role: 'member', plane: 'tenant', capabilities: [] };
}
function skill(overrides: Partial<SkillSummary> = {}): SkillSummary {
  return {
    id: 's1',
    tenant_id: 't1',
    name: 'pdf-toolkit',
    description: 'Handles PDFs',
    enabled: true,
    bound_coworker_count: 0,
    visibility: 'private',
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    created_by_user_id: 'u1',
    ...overrides,
  } as SkillSummary;
}

afterEach(() => setMe(null));

describe('chipMatches', () => {
  it('all matches everything', () => {
    setMe(me());
    expect(chipMatches('all', skill({ created_by_user_id: 'other' }))).toBe(true);
  });

  it('mine = own rows only (incl. own shared rows)', () => {
    setMe(me('u1'));
    expect(chipMatches('mine', skill({ created_by_user_id: 'u1' }))).toBe(true);
    expect(chipMatches('mine', skill({ created_by_user_id: 'u1', visibility: 'shared' }))).toBe(true);
    expect(chipMatches('mine', skill({ created_by_user_id: 'other' }))).toBe(false);
  });

  it('shared = shared AND not own (own shared rows excluded)', () => {
    setMe(me('u1'));
    expect(chipMatches('shared', skill({ created_by_user_id: 'other', visibility: 'shared' }))).toBe(true);
    expect(chipMatches('shared', skill({ created_by_user_id: 'u1', visibility: 'shared' }))).toBe(false);
    expect(chipMatches('shared', skill({ created_by_user_id: 'other', visibility: 'private' }))).toBe(false);
  });

  it('null-creator rows are never "mine"', () => {
    setMe(me('u1'));
    expect(chipMatches('mine', skill({ created_by_user_id: null }))).toBe(false);
  });
});

describe('chipEmptyCopy', () => {
  it('varies by chip and by whether any skills exist', () => {
    expect(chipEmptyCopy('all', false)).toBe('No skills yet.');
    expect(chipEmptyCopy('mine', true)).toBe('You have not created any skills yet.');
    expect(chipEmptyCopy('shared', true)).toBe('No one has shared a skill with you yet.');
  });
});
