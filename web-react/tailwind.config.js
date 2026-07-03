/** Tailwind 3 (JS config + PostCSS) — deliberate intra-repo divergence
 *  from web/ (Tailwind 4, CSS-first) per spec D-8. The shared ground
 *  between the two SPAs is src/styles/tokens.css; every theme color
 *  below resolves to a --rm-* token, never a literal (lint:tokens-only
 *  enforces the same rule on component code). */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        action: 'var(--rm-action)',
        'action-hover': 'var(--rm-action-hover)',
        ink: 'var(--rm-text)',
        'ink-muted': 'var(--rm-text-muted)',
        line: 'var(--rm-border)',
        'app-bg': 'var(--rm-app-bg)',
        'card-bg': 'var(--rm-card-bg)',
        'info-bg': 'var(--rm-info-bg)',
        'info-text': 'var(--rm-info-text)',
        'nav-text': 'var(--rm-nav-text)',
        'nav-text-2': 'var(--rm-nav-text-2)',
        'nav-border': 'var(--rm-nav-border)',
        'nav-hover': 'var(--rm-nav-hover)',
      },
      fontFamily: {
        base: 'var(--font-base)',
      },
    },
  },
  plugins: [],
};
