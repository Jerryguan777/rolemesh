#!/usr/bin/env node
// Ported from web/scripts/lint-no-admin-chat.mjs, extended with the
// two structural import-direction rules from the web-react layout
// (spec §1.1):
//
//   1. No `/api/admin/` URL literals anywhere in src/ — chat is on
//      `/api/v1/*`. (web/ carries a safety-pages allowlist; web-react
//      is greenfield and starts with an EMPTY allowlist. When the
//      safety pages arrive, they add themselves here with a reason.)
//   2. `features/chat/**` must not import from `features/settings/**`
//      and vice versa (one-way dependency, lean chat bundle).
//   3. `lib/**` must never import react — keeps the copied pure
//      modules framework-free so a later workspace extraction is
//      zero-cost.
//
// Run as: `npm run lint:no-admin-chat`. Exits 0 on clean, 1 on violation.

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, '..', 'src');

const ADMIN_ALLOWLIST = new Set([]);

const ADMIN_PATTERN = /\/api\/admin\//g;
// Bare or scoped react import/require inside lib/ (react, react-dom,
// react/jsx-runtime, …).
const REACT_IMPORT_RE = /from\s+['"]react(-dom)?(\/[^'"]*)?['"]/g;
const IMPORT_SOURCE_RE = /from\s+['"]([^'"]+)['"]/g;

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

function report(rel, line, msg) {
  bad += 1;
  process.stderr.write(`${rel}:${line}: ${msg}\n`);
}

for (const f of files) {
  const rel = relative(ROOT, f);
  const text = readFileSync(f, 'utf8');

  // Rule 1 — /api/admin/ literals.
  if (!ADMIN_ALLOWLIST.has(rel)) {
    let m;
    ADMIN_PATTERN.lastIndex = 0;
    while ((m = ADMIN_PATTERN.exec(text)) !== null) {
      const line = text.slice(0, m.index).split('\n').length;
      report(rel, line, 'forbidden `/api/admin/` literal in chat-relevant code');
    }
  }

  // Rule 2 — chat ⇄ settings isolation (skip tests: they may mount
  // cross-feature fixtures).
  const isTest = /\.test\.(ts|tsx)$/.test(rel);
  if (!isTest && (rel.startsWith('features/chat/') || rel.startsWith('features/settings/'))) {
    const own = rel.startsWith('features/chat/') ? 'chat' : 'settings';
    const other = own === 'chat' ? 'settings' : 'chat';
    let m;
    IMPORT_SOURCE_RE.lastIndex = 0;
    while ((m = IMPORT_SOURCE_RE.exec(text)) !== null) {
      const source = m[1];
      // Both relative traversals and absolute-ish paths count.
      if (source.includes(`features/${other}/`) || source.includes(`features/${other}'`)) {
        const line = text.slice(0, m.index).split('\n').length;
        report(rel, line, `features/${own} must not import features/${other} (${source})`);
      }
    }
  }

  // Rule 3 — lib/ purity: no react imports.
  if (rel.startsWith('lib/')) {
    let m;
    REACT_IMPORT_RE.lastIndex = 0;
    while ((m = REACT_IMPORT_RE.exec(text)) !== null) {
      const line = text.slice(0, m.index).split('\n').length;
      report(rel, line, 'lib/ must stay framework-free — no react imports');
    }
  }
}

if (bad > 0) {
  process.stderr.write(
    `\n${bad} violation(s). Use the typed v1 client (ApiClient), keep ` +
      `chat/settings isolated, and keep lib/ react-free.\n`,
  );
  process.exit(1);
}
process.stdout.write('lint-no-admin-chat: clean\n');
