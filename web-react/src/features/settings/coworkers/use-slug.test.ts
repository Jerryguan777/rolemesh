import { describe, expect, it } from 'vitest';
import { isValidSlug, slugify } from './use-slug';

describe('slugify', () => {
  it('lowers, dashes non-slug chars, collapses runs', () => {
    expect(slugify('Portfolio Manager')).toBe('portfolio-manager');
    expect(slugify('SEC — EDGAR!! Agent')).toBe('sec-edgar-agent');
  });

  it('trims leading non-alphanumerics and trailing dashes', () => {
    expect(slugify('---Mira')).toBe('mira');
    expect(slugify('Mira?!')).toBe('mira');
  });

  it('keeps digits as a valid first char and caps at 64', () => {
    expect(slugify('42 helpers')).toBe('42-helpers');
    expect(slugify('x'.repeat(80)).length).toBeLessThanOrEqual(64);
  });

  it('produces valid slugs for typical names', () => {
    for (const name of ['Portfolio Manager', 'Mira', 'Data_Analyst 2']) {
      expect(isValidSlug(slugify(name))).toBe(true);
    }
  });
});

describe('isValidSlug', () => {
  it('accepts contract-shaped slugs', () => {
    expect(isValidSlug('portfolio-manager')).toBe(true);
    expect(isValidSlug('a')).toBe(true);
    expect(isValidSlug('42_helpers')).toBe(true);
  });

  it('rejects empty, leading dash, uppercase, illegal chars, overlong', () => {
    expect(isValidSlug('')).toBe(false);
    expect(isValidSlug('-lead')).toBe(false);
    expect(isValidSlug('Upper')).toBe(false);
    expect(isValidSlug('has space')).toBe(false);
    expect(isValidSlug('a'.repeat(65))).toBe(false);
  });
});
