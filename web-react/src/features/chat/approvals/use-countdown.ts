// useNowTick — the single shared 1 Hz ticker (spec O.6). Every mounted
// countdown (cards + inbox rows) subscribes to ONE module-level
// interval; it starts with the first subscriber and stops with the
// last, so an idle chat holds no timer (the Lit inbox's "no standing
// high-frequency timer" rule). The tick is DISPLAY-ONLY: expiry never
// flips a card — that is the server's `expired` push to make.

import { useSyncExternalStore } from 'react';

let now = Date.now();
let timer: ReturnType<typeof setInterval> | null = null;
const subscribers = new Set<() => void>();

function subscribe(cb: () => void): () => void {
  subscribers.add(cb);
  if (!timer) {
    timer = setInterval(() => {
      now = Date.now();
      subscribers.forEach((fn) => fn());
    }, 1000);
  }
  return () => {
    subscribers.delete(cb);
    if (subscribers.size === 0 && timer) {
      clearInterval(timer);
      timer = null;
    }
  };
}

export function useNowTick(): number {
  return useSyncExternalStore(subscribe, () => now);
}
