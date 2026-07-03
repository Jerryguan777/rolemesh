// useSystemTheme — live `(prefers-color-scheme: dark)` subscription
// (spec N.3; behavioral reference web/src/components/appearance-page.ts).
// The listener is removed in the effect cleanup — the Lit
// disconnectedCallback teardown. Nothing is persisted (locked
// decision, plan §13 / v2-A: no in-app toggle).
//
// Single consumer today (the read-only Appearance page). The day the
// dark token palette lands, this hook graduates to the app layer and
// starts driving the actual theme — the mechanism is wired ahead of
// the tokens on purpose (D-N1).

import { useEffect, useState } from 'react';

export function useSystemTheme(): 'light' | 'dark' {
  const [dark, setDark] = useState(
    () =>
      typeof window !== 'undefined' &&
      !!window.matchMedia &&
      window.matchMedia('(prefers-color-scheme: dark)').matches,
  );

  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return;
    const mql = window.matchMedia('(prefers-color-scheme: dark)');
    const onChange = (e: MediaQueryListEvent) => setDark(e.matches);
    mql.addEventListener('change', onChange);
    return () => mql.removeEventListener('change', onChange);
  }, []);

  return dark ? 'dark' : 'light';
}
