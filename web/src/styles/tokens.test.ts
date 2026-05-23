// Tokens — pin the prototype-derived design language so a casual
// "tidy up colors" edit does not silently drop the v2 visual
// identity. We assert presence + key values rather than CSS-cascade
// behaviour because happy-dom does not fully resolve custom property
// inheritance through shadow roots and we'd rather a fast string
// check than a flaky DOM assertion.
//
// Each block here exists because the token would otherwise be
// easy to remove by accident: the accent colour drives every CTA
// in the new shell, the cream surfaces define the "paper-like"
// feel that the prototype's mood depends on, and the dark-mode
// branch is hidden behind `@media` so manual review tends to miss
// it.

import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const __dirname = dirname(fileURLToPath(import.meta.url));
const TOKENS = readFileSync(resolve(__dirname, 'tokens.css'), 'utf8');

describe('tokens.css', () => {
  it('declares the terracotta accent and cream surfaces from the prototype', () => {
    expect(TOKENS).toMatch(/--rm-accent:\s*#C2613F/);
    expect(TOKENS).toMatch(/--rm-accent-2:\s*#A94E30/);
    expect(TOKENS).toMatch(/--rm-bg:\s*#FAF8F3/);
    expect(TOKENS).toMatch(/--rm-surface:\s*#FFFFFF/);
    expect(TOKENS).toMatch(/--rm-surface-2:\s*#F3F0E9/);
  });

  it('keeps a serif-then-system fallback chain for the display font', () => {
    const m = TOKENS.match(/--rm-font-display:\s*([^;]+);/);
    expect(m).not.toBeNull();
    const value = m![1];
    expect(value).toMatch(/Fraunces/);
    // System fallbacks must come AFTER the Google Font; if the CDN
    // fails or the user is offline the UI should still render with
    // a sensible serif (design §7).
    expect(value).toMatch(/Georgia|serif/);
  });

  it('keeps Hanken with system fallbacks for the body font', () => {
    const m = TOKENS.match(/--rm-font-body:\s*([^;]+);/);
    expect(m).not.toBeNull();
    expect(m![1]).toMatch(/Hanken Grotesk/);
    expect(m![1]).toMatch(/system-ui|sans-serif/);
  });

  it('overrides surfaces and ink under prefers-color-scheme: dark', () => {
    const darkBlock = TOKENS.match(
      /@media\s*\(prefers-color-scheme:\s*dark\)\s*\{[\s\S]*?\}\s*\}/,
    );
    expect(darkBlock, 'dark-mode @media block missing').not.toBeNull();
    const dark = darkBlock![0];
    // Cream → near-black; if the dark surface is still pale white,
    // somebody deleted the dark branch.
    expect(dark).toMatch(/--rm-bg:\s*#1E1B17/);
    expect(dark).toMatch(/--rm-ink:\s*#ECE7DC/);
    expect(dark).toMatch(/--rm-accent:/);
  });
});
