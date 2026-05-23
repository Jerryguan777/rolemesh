#!/usr/bin/env node
// Forbid v1.1 flat hash literals (`#/coworkers`, `#/admin/safety/…`, …)
// in v2 frontend code. The redirect surface in `web/src/router.ts`
// rewrites them at runtime, but a hard-coded link anywhere else
// would either skip the redirect (worst) or trigger a flicker
// before it kicks in (still bad UX).
//
// Allowlist: the redirect map itself + this lint + the router test
// that pins the rewrite contract. Everything else must use the v2
// nested hash directly.
//
// Run as: `npm run lint:flat-route` (added to package.json scripts).
// Exits 0 on clean, 1 on violation.

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, '..', 'src');

/** Flat hashes the redirect surface rewrites. Each must match as a
 *  string literal in source code (we don't require a path boundary
 *  in the source — `'#/skills'` is the literal that breaks, even if
 *  the runtime would also rewrite `'#/skillset'`). */
const FORBIDDEN = [
  '#/coworkers',
  '#/mcp-servers',
  '#/models',
  '#/credentials',
  '#/skills',
  '#/approvals',
  '#/admin/safety/rules',
  '#/admin/safety/decisions',
  '#/admin/safety',
  '#/admin',
];

// Files that are *allowed* to mention flat hashes because they own
// the rewrite surface, test it, or are v1.1 page components that
// the v2-A session keeps zero-touched. The redirect handler in
// router.ts rewrites their runtime navigations transparently, so
// the strings inside are harmless. Reskinning these to v2 hashes
// is a v2-B / v2-C cleanup.
//
// Path is relative to `web/src/`.
const ALLOWLIST = new Set([
  'router.ts',
  'router.test.ts',
  // v1.1 cross-links — kept zero-touched in v2-A; redirect handles them.
  'components/sidebar.ts',
  'components/skills-page.ts',
  'components/skills-page.test.ts',
  'components/skill-detail-page.ts',
  'components/skill-detail-page.test.ts',
  'components/coworker-skills-tab.ts',
  'components/coworker-skills-tab.test.ts',
]);

/** Heuristic check: is the literal inside a comment? We can be
 *  conservative — the goal is to avoid flagging the redirect map
 *  reference in a doc comment, not to be a real parser. Line
 *  starts with `//` or `*` (block comment continuation), or the
 *  literal appears inside a `/* … *\/` span on the same line. */
function isInComment(text, idx) {
  // Walk backwards to the start of the line.
  let i = idx;
  while (i > 0 && text[i - 1] !== '\n') i -= 1;
  const lineStart = i;
  const linePrefix = text.slice(lineStart, idx).trimStart();
  if (linePrefix.startsWith('//') || linePrefix.startsWith('*')) return true;
  // Inline `/* ... */`: check that the most recent `/*` before idx
  // is not closed before idx.
  const beforeIdx = text.slice(0, idx);
  const lastOpen = beforeIdx.lastIndexOf('/*');
  if (lastOpen === -1) return false;
  const lastClose = beforeIdx.lastIndexOf('*/');
  return lastOpen > lastClose;
}

function walk(dir, out) {
  for (const entry of readdirSync(dir)) {
    const p = join(dir, entry);
    const s = statSync(p);
    if (s.isDirectory()) {
      walk(p, out);
    } else if (s.isFile() && /\.(ts|js|mjs)$/.test(p)) {
      out.push(p);
    }
  }
}

const files = [];
walk(ROOT, files);

let bad = 0;
for (const f of files) {
  const rel = relative(ROOT, f);
  if (ALLOWLIST.has(rel)) continue;
  const text = readFileSync(f, 'utf8');
  for (const literal of FORBIDDEN) {
    let from = 0;
    while (true) {
      const idx = text.indexOf(literal, from);
      if (idx === -1) break;
      // Require a quote character immediately before to keep the
      // false-positive rate low. Comments mentioning the flat hash
      // (e.g. CHANGELOG-style notes inside a JSDoc) are fine.
      const before = text[idx - 1];
      const isQuoted =
        before === '"' || before === "'" || before === '`';
      if (isQuoted && !isInComment(text, idx)) {
        const line = text.slice(0, idx).split('\n').length;
        bad += 1;
        process.stderr.write(
          `${rel}:${line}: forbidden flat hash literal ${literal} — ` +
            `use the v2 nested form (see router.ts redirect map).\n`,
        );
      }
      from = idx + literal.length;
    }
  }
}

if (bad > 0) {
  process.stderr.write(
    `\n${bad} violation(s). Replace each with the corresponding ` +
      `\`#/manage/...\` or \`#/activity/...\` hash from router.ts.\n`,
  );
  process.exit(1);
}
process.stdout.write('lint-flat-route: clean\n');
