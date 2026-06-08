// @vitest-environment happy-dom
// <rm-access-denied> — the 403 fallback (spec §7.7). Presentation only,
// no authorization logic. We pin two contracts:
//   1. the required capability name appears verbatim in the copy, so the
//      user (and any support thread) has the exact term;
//   2. the "← Back to" link points at the slug the shell passed in, not
//      a hardcoded default.

import { afterEach, describe, expect, it } from 'vitest';

import './access-denied-page.js';
import type { RmAccessDenied } from './access-denied-page.js';

async function settle(el: RmAccessDenied): Promise<void> {
  for (let i = 0; i < 10; i += 1) {
    await Promise.resolve();
    await el.updateComplete;
  }
}

async function mount(
  props: Partial<
    Pick<
      RmAccessDenied,
      'capability' | 'pageLabel' | 'backSlug' | 'backLabel'
    >
  >,
): Promise<RmAccessDenied> {
  const el = document.createElement('rm-access-denied') as RmAccessDenied;
  Object.assign(el, props);
  document.body.appendChild(el);
  await settle(el);
  return el;
}

describe('<rm-access-denied>', () => {
  afterEach(() => {
    document.querySelectorAll('rm-access-denied').forEach((el) => el.remove());
  });

  it('names the required capability verbatim in the copy', async () => {
    const el = await mount({ capability: 'safety.read', pageLabel: 'Safety rules' });
    const cap = el.querySelector('[data-testid="access-denied-capability"]');
    expect(cap?.textContent?.trim()).toBe('safety.read');
    // The capability must appear in the human sentence, not just an attr.
    const copy = el.querySelector('[data-testid="access-denied-copy"]');
    expect(copy?.textContent).toContain('safety.read');
  });

  it('names the gated page label in the copy when provided', async () => {
    const el = await mount({ capability: 'safety.read', pageLabel: 'Safety rules' });
    const copy = el.querySelector('[data-testid="access-denied-copy"]');
    expect(copy?.textContent).toContain('Safety rules');
  });

  it('points the back-link at the slug it was given (not a hardcoded default)', async () => {
    // A user whose first visible page is Skills (not Coworkers) must be
    // routed back THERE — proving the target is data-driven, so a member
    // who lost the coworkers entry would still get a working link.
    const el = await mount({
      capability: 'tenant.manage',
      pageLabel: 'General',
      backSlug: 'skills',
      backLabel: 'Skills',
    });
    const back = el.querySelector('[data-testid="access-denied-back"]');
    expect(back?.getAttribute('href')).toBe('#/manage/skills');
    expect(back?.getAttribute('data-slug')).toBe('skills');
    expect(back?.textContent).toContain('Skills');
  });

  it('defaults the back-link to coworkers when no slug is supplied', async () => {
    const el = await mount({ capability: 'safety.read' });
    const back = el.querySelector('[data-testid="access-denied-back"]');
    expect(back?.getAttribute('href')).toBe('#/manage/coworkers');
    expect(back?.textContent).toContain('Coworkers');
  });

  it('renders the "Access denied" heading and a lock glyph', async () => {
    const el = await mount({ capability: 'safety.read' });
    expect(el.textContent).toContain('Access denied');
    // Lock glyph is an inline <svg>; assert it exists so a future
    // refactor that drops it fails here.
    expect(el.querySelector('.ad-glyph svg')).not.toBeNull();
  });
});
