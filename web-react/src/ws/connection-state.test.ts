// Copied from web/src/ws/connection-state.test.ts @ cf6b0f1; keep in sync manually until workspace extraction.
// ConnectionState — pin the aggregate-OR semantics and the
// notification edge so a noisy reconnect can't spam subscribers.

import { describe, expect, it, vi } from 'vitest';

import { ConnectionState } from './connection-state.js';

describe('ConnectionState', () => {
  it('starts disconnected and notifies on the first open', () => {
    const cs = new ConnectionState();
    const listener = vi.fn();
    cs.subscribe(listener);
    expect(cs.connected).toBe(false);
    cs.set('a', true);
    expect(cs.connected).toBe(true);
    expect(listener).toHaveBeenCalledExactlyOnceWith(true);
  });

  it('does not double-fire when a second channel comes up while one is already live', () => {
    const cs = new ConnectionState();
    const listener = vi.fn();
    cs.set('a', true);
    cs.subscribe(listener);
    listener.mockReset();
    // Aggregate is already true; bringing up a second channel must
    // not re-emit the same boolean — that drives Lit re-renders we
    // don't need and hides genuine flips behind churn.
    cs.set('b', true);
    expect(listener).not.toHaveBeenCalled();
  });

  it('stays connected when one of two live channels closes', () => {
    const cs = new ConnectionState();
    cs.set('a', true);
    cs.set('b', true);
    const listener = vi.fn();
    cs.subscribe(listener);
    cs.set('a', false);
    // Aggregate stays true because b is still live; listener silent.
    expect(cs.connected).toBe(true);
    expect(listener).not.toHaveBeenCalled();
    cs.set('b', false);
    expect(cs.connected).toBe(false);
    expect(listener).toHaveBeenCalledExactlyOnceWith(false);
  });

  it('remove() drops a channel — equivalent to flipping it to false', () => {
    const cs = new ConnectionState();
    cs.set('a', true);
    const listener = vi.fn();
    cs.subscribe(listener);
    cs.remove('a');
    expect(cs.connected).toBe(false);
    expect(listener).toHaveBeenCalledExactlyOnceWith(false);
  });

  it('remove() is a no-op when the channel was never registered', () => {
    const cs = new ConnectionState();
    cs.set('a', true);
    const listener = vi.fn();
    cs.subscribe(listener);
    cs.remove('never-seen');
    expect(cs.connected).toBe(true);
    expect(listener).not.toHaveBeenCalled();
  });

  it('subscribe() returns an unsubscribe handle that detaches the listener', () => {
    const cs = new ConnectionState();
    const listener = vi.fn();
    const off = cs.subscribe(listener);
    off();
    cs.set('a', true);
    expect(listener).not.toHaveBeenCalled();
  });

  it('a single channel toggling repeatedly only notifies on actual aggregate flips', () => {
    const cs = new ConnectionState();
    const listener = vi.fn();
    cs.subscribe(listener);
    cs.set('a', true);
    cs.set('a', true);
    cs.set('a', true);
    cs.set('a', false);
    cs.set('a', false);
    cs.set('a', true);
    // false → true (1st), true → false (4th), false → true (6th)
    expect(listener).toHaveBeenCalledTimes(3);
  });
});
