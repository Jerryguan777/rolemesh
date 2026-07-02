import { describe, expect, it } from 'vitest';
import { relativeTime } from './relative-time';

const NOW = Date.parse('2026-07-02T12:00:00Z');
const at = (msAgo: number) => new Date(NOW - msAgo).toISOString();

describe('relativeTime', () => {
  it('renders the seconds bands', () => {
    expect(relativeTime(at(3_000), NOW)).toBe('just now');
    expect(relativeTime(at(26_000), NOW)).toBe('26 seconds ago');
  });

  it('renders minutes and hours', () => {
    expect(relativeTime(at(4 * 60_000), NOW)).toBe('4m ago');
    expect(relativeTime(at(2 * 3_600_000), NOW)).toBe('2h ago');
  });

  it('renders day bands', () => {
    expect(relativeTime(at(30 * 3_600_000), NOW)).toBe('yesterday');
    expect(relativeTime(at(3 * 86_400_000), NOW)).toBe('3 days ago');
    expect(relativeTime(at(10 * 86_400_000), NOW)).toBe('last week');
  });

  it('falls back to a date beyond two weeks', () => {
    const label = relativeTime(at(30 * 86_400_000), NOW);
    expect(label).not.toContain('ago');
    expect(label.length).toBeGreaterThan(0);
  });

  it('clamps future timestamps to just now and tolerates junk', () => {
    expect(relativeTime(at(-5_000), NOW)).toBe('just now');
    expect(relativeTime('not-a-date', NOW)).toBe('');
  });
});
