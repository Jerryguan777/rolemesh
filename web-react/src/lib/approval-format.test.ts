// Ported from web/src/components/approvals-inbox.test.ts (the pure-
// helper describes) alongside the module's move into lib/.
import { describe, expect, it } from 'vitest';
import { formatCountdown, isUrgent, paramsInline } from './approval-format';

describe('paramsInline', () => {
  it('joins up to the first 4 entries as k: v · …', () => {
    const out = paramsInline({ a: 1, b: 'two', c: true, d: 'four', e: 'five' });
    expect(out).toBe('a: 1 · b: two · c: true · d: four');
    expect(out).not.toContain('e:');
  });

  it('truncates a long value at 30 chars + ellipsis (never silently drops it)', () => {
    const long = 'x'.repeat(50);
    const out = paramsInline({ text: long });
    expect(out).toBe('text: ' + 'x'.repeat(30) + '…');
  });

  it('returns empty string for empty object, non-object, array, and null', () => {
    expect(paramsInline({})).toBe('');
    expect(paramsInline(null)).toBe('');
    expect(paramsInline(undefined)).toBe('');
    expect(paramsInline('nope')).toBe('');
    expect(paramsInline([1, 2, 3])).toBe('');
  });
});

describe('formatCountdown', () => {
  const now = Date.parse('2026-06-01T12:00:00Z');
  it('renders minutes when > 60s remain', () => {
    expect(formatCountdown('2026-06-01T12:18:00Z', now)).toBe('18m left');
  });
  it('renders seconds under a minute', () => {
    expect(formatCountdown('2026-06-01T12:00:42Z', now)).toBe('42s left');
  });
  it('renders "expired" at or past the deadline', () => {
    expect(formatCountdown('2026-06-01T12:00:00Z', now)).toBe('expired');
    expect(formatCountdown('2026-06-01T11:59:00Z', now)).toBe('expired');
  });
  it('drops a missing or unparseable timestamp', () => {
    expect(formatCountdown(null, now)).toBe('');
    expect(formatCountdown('not-a-date', now)).toBe('');
  });
});

describe('isUrgent (badge / row threshold)', () => {
  const now = Date.parse('2026-06-01T12:00:00Z');
  it('is false at exactly 5 minutes out (boundary is strict <)', () => {
    expect(isUrgent('2026-06-01T12:05:00Z', now)).toBe(false);
  });
  it('is true just inside 5 minutes', () => {
    expect(isUrgent('2026-06-01T12:04:59Z', now)).toBe(true);
  });
  it('is true for an already-expired item', () => {
    expect(isUrgent('2026-06-01T11:50:00Z', now)).toBe(true);
  });
  it('is false with no expiry', () => {
    expect(isUrgent(null, now)).toBe(false);
  });
});
