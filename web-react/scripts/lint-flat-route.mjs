#!/usr/bin/env node
// Ported from web/scripts/lint-flat-route.mjs (path + allowlist
// adjustments for web-react). Forbid v1.1 flat hash literals
// (`#/coworkers`, `#/admin/safety/…`, …) in frontend code. The
// redirect surface in `src/app/legacy-redirects.ts` rewrites them at
// runtime, but a hard-coded link anywhere else would either skip the
// redirect (worst) or trigger a flicker before it kicks in.
//
// Allowlist: the redirect map itself + its test. Everything else
// must use the v2 nested hash (`#/manage/{slug}`) directly.
//
// Run as: `npm run lint:flat-route`. Exits 0 on clean, 1 on violation.

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, '..', 'src');

/** Flat hashes the redirect surface rewrites. Each must match as a
 *  string literal in source code. */
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

// Files allowed to mention flat hashes because they own the rewrite
// surface or test it. Path is relative to `web-react/src/`.
const ALLOWLIST = new Set([
  'app/legacy-redirects.ts',
  'app/legacy-redirects.test.ts',
]);

function isInComment(text, idx) {
  let i = idx;
  while (i > 0 && text[i - 1] !== '\n') i -= 1;
  const lineStart = i;
  const linePrefix = text.slice(lineStart, idx).trimStart();
  if (linePrefix.startsWith('//') || linePrefix.startsWith('*')) return true;
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
    } else if (s.isFile() && /\.(ts|tsx|js|mjs)$/.test(p)) {
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
      // are fine.
      const before = text[idx - 1];
      const isQuoted = before === '"' || before === "'" || before === '`';
      if (isQuoted && !isInComment(text, idx)) {
        const line = text.slice(0, idx).split('\n').length;
        bad += 1;
        process.stderr.write(
          `${rel}:${line}: forbidden flat hash literal ${literal} — ` +
            `use the v2 nested form (see app/legacy-redirects.ts).\n`,
        );
      }
      from = idx + literal.length;
    }
  }
}

if (bad > 0) {
  process.stderr.write(`\n${bad} violation(s).\n`);
  process.exit(1);
}
process.stdout.write('lint-flat-route: clean\n');
