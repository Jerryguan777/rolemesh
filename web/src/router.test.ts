// @vitest-environment happy-dom
// Router — pins both the v2 IA helpers and the legacy redirect
// surface. Each redirect entry is a contract with bookmarked URLs;
// removing one silently is the kind of breakage tests are for.
//
// Anti-mirror: we drive the public API (`applyLegacyRedirect`,
// `installLegacyRedirects`, `topLevelShell`, `matchRoute`) and
// assert visible behaviour (URL replacement, route id, shell
// identifier). We never reach into internal map state.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  applyLegacyRedirect,
  installLegacyRedirects,
  topLevelShell,
} from './router.js';

describe('applyLegacyRedirect', () => {
  // Each row is `[old, new]` — exhaustive list of the 8 redirects
  // the v2-A session prompt locks in. Iterating instead of writing
  // 8 near-identical `it()` blocks keeps the contract auditable.
  const TABLE: Array<[string, string]> = [
    ['#/coworkers',              '#/manage/coworkers'],
    ['#/mcp-servers',            '#/manage/mcp-servers'],
    ['#/models',                 '#/manage/models'],
    ['#/credentials',            '#/manage/credentials'],
    ['#/skills',                 '#/manage/skills'],
    ['#/approvals',              '#/manage/approval-policies'],
    ['#/admin/safety/rules',     '#/manage/safety'],
    ['#/admin/safety/decisions', '#/activity/safety-decisions'],
  ];

  it.each(TABLE)('rewrites %s → %s', (oldHash, newHash) => {
    expect(applyLegacyRedirect(oldHash)).toBe(newHash);
  });

  it('preserves sub-paths after the redirected segment', () => {
    expect(applyLegacyRedirect('#/skills/abc')).toBe('#/manage/skills/abc');
    expect(applyLegacyRedirect('#/coworkers/123/edit')).toBe(
      '#/manage/coworkers/123/edit',
    );
  });

  it('preserves query strings appended to the legacy hash', () => {
    expect(applyLegacyRedirect('#/skills?foo=1')).toBe(
      '#/manage/skills?foo=1',
    );
  });

  it('returns null for hashes that are already on the v2 IA', () => {
    expect(applyLegacyRedirect('#/manage/coworkers')).toBeNull();
    expect(applyLegacyRedirect('#/activity/safety-decisions')).toBeNull();
    expect(applyLegacyRedirect('#/')).toBeNull();
    expect(applyLegacyRedirect('')).toBeNull();
  });

  it('does not accidentally rewrite an unrelated hash that shares a prefix', () => {
    // '#/skillset' starts with '#/skills' lexically but is a
    // different page — the rewrite must require a path boundary.
    expect(applyLegacyRedirect('#/skillset')).toBeNull();
    expect(applyLegacyRedirect('#/modelspec')).toBeNull();
  });
});

describe('topLevelShell', () => {
  it('routes /manage/* to the settings shell', () => {
    expect(topLevelShell('#/manage/coworkers')).toBe('manage');
    expect(topLevelShell('#/manage')).toBe('manage');
  });
  it('routes /activity/* to the activity shell', () => {
    expect(topLevelShell('#/activity/safety-decisions')).toBe('activity');
  });
  it('falls back to chat for any other hash', () => {
    expect(topLevelShell('#/')).toBe('chat');
    expect(topLevelShell('')).toBe('chat');
    expect(topLevelShell('#/unknown')).toBe('chat');
  });
});

describe('installLegacyRedirects', () => {
  let originalReplace: typeof location.replace;
  let calls: string[];
  let teardown: () => void;

  beforeEach(() => {
    calls = [];
    originalReplace = location.replace.bind(location);
    // happy-dom's location.replace updates the URL but does not
    // dispatch hashchange the way browsers do; we spy on the call
    // itself which is the externally observable behaviour.
    vi.spyOn(location, 'replace').mockImplementation((url: string | URL) => {
      calls.push(String(url));
      // Reflect into location.hash so subsequent reads of
      // location.hash see the new value (matches browser semantics).
      const u = String(url);
      const hashIdx = u.indexOf('#');
      if (hashIdx >= 0) {
        // Bypass our own intercept by writing to a fresh location-
        // like via the original.
        Object.defineProperty(location, 'hash', {
          configurable: true,
          value: u.slice(hashIdx),
          writable: true,
        });
      }
    });
    // Reset hash before each case.
    Object.defineProperty(location, 'hash', {
      configurable: true,
      value: '',
      writable: true,
    });
  });

  afterEach(() => {
    teardown?.();
    vi.restoreAllMocks();
    void originalReplace;
  });

  it('uses location.replace (not assign) for a flat legacy hash', () => {
    Object.defineProperty(location, 'hash', {
      configurable: true,
      value: '#/coworkers',
      writable: true,
    });
    teardown = installLegacyRedirects();
    expect(calls.length).toBe(1);
    expect(calls[0]).toMatch(/#\/manage\/coworkers$/);
  });

  it('does not redirect when the hash is already on the v2 IA', () => {
    Object.defineProperty(location, 'hash', {
      configurable: true,
      value: '#/manage/coworkers',
      writable: true,
    });
    teardown = installLegacyRedirects();
    expect(calls.length).toBe(0);
  });

  it('handles legacy hashchanges after install', () => {
    Object.defineProperty(location, 'hash', {
      configurable: true,
      value: '#/',
      writable: true,
    });
    teardown = installLegacyRedirects();
    expect(calls.length).toBe(0);
    // Simulate the user navigating to a legacy hash mid-session.
    Object.defineProperty(location, 'hash', {
      configurable: true,
      value: '#/admin/safety/decisions',
      writable: true,
    });
    window.dispatchEvent(new HashChangeEvent('hashchange'));
    expect(calls.length).toBe(1);
    expect(calls[0]).toMatch(/#\/activity\/safety-decisions$/);
  });
});
