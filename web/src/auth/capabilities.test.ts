// capabilities.ts — the capability/ownership gating helpers (spec §7.1).
//
// These are the single mechanism the whole SPA gates on, so the corners
// that matter are the null/undefined edges of `created_by_user_id` (the
// three-value ownership-escape semantics) and the fail-closed behaviour
// when no Me is cached. We exercise the REAL module cache (no mocks) so a
// regression in the setMe/currentMe wiring would surface here.

import { beforeEach, describe, expect, it } from 'vitest';
import type { Me } from '../api/client.js';
import {
  canManage,
  currentMe,
  hasCapability,
  isOwnResource,
  setMe,
} from './capabilities.js';

function makeMe(over: Partial<Me> = {}): Me {
  return {
    user_id: 'u-alice',
    tenant_id: 't1',
    name: 'Alice',
    email: 'alice@example.com',
    role: 'member',
    plane: 'tenant',
    capabilities: ['coworker.create', 'coworker.use'],
    ...over,
  };
}

beforeEach(() => {
  // Reset the module-level cache so cases don't bleed into each other.
  setMe(null);
});

describe('currentMe / setMe', () => {
  it('returns exactly what setMe last set, including back to null', () => {
    expect(currentMe()).toBeNull();
    const me = makeMe();
    setMe(me);
    expect(currentMe()).toBe(me);
    setMe(null);
    expect(currentMe()).toBeNull();
  });
});

describe('hasCapability', () => {
  it('is false when no Me is cached (fail-closed before boot)', () => {
    expect(hasCapability('coworker.create')).toBe(false);
  });

  it('is true for a present action and false for an absent one', () => {
    setMe(makeMe({ capabilities: ['coworker.create', 'skill.manage'] }));
    expect(hasCapability('coworker.create')).toBe(true);
    expect(hasCapability('skill.manage')).toBe(true);
    expect(hasCapability('mcp.configure')).toBe(false);
  });

  it('is false against an empty capability list (a role with nothing)', () => {
    setMe(makeMe({ capabilities: [] }));
    expect(hasCapability('coworker.create')).toBe(false);
  });
});

describe('isOwnResource (three-value ownership escape)', () => {
  it('is true when created_by_user_id matches the cached user_id', () => {
    setMe(makeMe({ user_id: 'u-alice' }));
    expect(isOwnResource({ created_by_user_id: 'u-alice' })).toBe(true);
  });

  it('is false when the ids differ', () => {
    setMe(makeMe({ user_id: 'u-alice' }));
    expect(isOwnResource({ created_by_user_id: 'u-bob' })).toBe(false);
  });

  // The crux: a platform-default resource has a NULL owner and must NEVER
  // count as owned — mirrors the SQL `col = :uid` where NULL never matches.
  it('is false when created_by_user_id is null (platform-default resource)', () => {
    setMe(makeMe({ user_id: 'u-alice' }));
    expect(isOwnResource({ created_by_user_id: null })).toBe(false);
  });

  it('is false when created_by_user_id is undefined (field absent)', () => {
    setMe(makeMe({ user_id: 'u-alice' }));
    expect(isOwnResource({})).toBe(false);
    expect(isOwnResource({ created_by_user_id: undefined })).toBe(false);
  });

  it('is false when no Me is cached, even for a non-null owner id', () => {
    expect(isOwnResource({ created_by_user_id: 'u-alice' })).toBe(false);
  });

  // Guard against a naive `me.user_id === resource.created_by_user_id`
  // implementation that would wrongly return true when BOTH sides are null.
  it('is false when both the user_id and the owner id are null-ish', () => {
    setMe(makeMe({ user_id: null as unknown as string }));
    expect(isOwnResource({ created_by_user_id: null })).toBe(false);
  });
});

describe('canManage (capability OR ownership)', () => {
  it('is true via the manage capability even for a resource owned by someone else', () => {
    setMe(makeMe({ user_id: 'u-alice', capabilities: ['coworker.manage'] }));
    expect(canManage({ created_by_user_id: 'u-bob' }, 'coworker.manage')).toBe(
      true,
    );
  });

  it('is true via ownership even without the manage capability', () => {
    setMe(makeMe({ user_id: 'u-alice', capabilities: [] }));
    expect(
      canManage({ created_by_user_id: 'u-alice' }, 'coworker.manage'),
    ).toBe(true);
  });

  it('is false when the user neither has the capability nor owns the resource', () => {
    setMe(makeMe({ user_id: 'u-alice', capabilities: ['coworker.use'] }));
    expect(canManage({ created_by_user_id: 'u-bob' }, 'coworker.manage')).toBe(
      false,
    );
  });

  it('is false for a null-owner resource when the user lacks the capability', () => {
    // Ownership escape must not fire on a platform-default resource.
    setMe(makeMe({ user_id: 'u-alice', capabilities: ['coworker.use'] }));
    expect(canManage({ created_by_user_id: null }, 'coworker.manage')).toBe(
      false,
    );
  });
});
