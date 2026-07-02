import { describe, expect, it } from 'vitest';
import { applyLegacyRedirect } from './legacy-redirects';

// Pins the redirect contract ported from web/src/router.ts — the two
// SPAs must rewrite the same bookmarks to the same homes.
describe('applyLegacyRedirect', () => {
  it('rewrites flat v1.1 hashes to their v2 nested home', () => {
    expect(applyLegacyRedirect('#/coworkers')).toBe('#/manage/coworkers');
    expect(applyLegacyRedirect('#/mcp-servers')).toBe('#/manage/mcp-servers');
    expect(applyLegacyRedirect('#/models')).toBe('#/manage/models');
    expect(applyLegacyRedirect('#/credentials')).toBe('#/manage/credentials');
    expect(applyLegacyRedirect('#/skills')).toBe('#/manage/skills');
    expect(applyLegacyRedirect('#/admin/safety/rules')).toBe('#/manage/safety');
    expect(applyLegacyRedirect('#/admin/safety/decisions')).toBe(
      '#/manage/safety-log',
    );
    expect(applyLegacyRedirect('#/activity/safety-decisions')).toBe(
      '#/manage/safety-log',
    );
  });

  it('preserves sub-paths and query strings', () => {
    expect(applyLegacyRedirect('#/skills/abc')).toBe('#/manage/skills/abc');
    expect(applyLegacyRedirect('#/skills/abc?x=1')).toBe(
      '#/manage/skills/abc?x=1',
    );
    expect(applyLegacyRedirect('#/admin/safety/decisions?rule_id=r1')).toBe(
      '#/manage/safety-log?rule_id=r1',
    );
  });

  it('returns null for hashes that need no rewriting', () => {
    expect(applyLegacyRedirect('#/')).toBeNull();
    expect(applyLegacyRedirect('#/manage/coworkers')).toBeNull();
    expect(applyLegacyRedirect('#/coworkersandmore')).toBeNull();
  });
});
