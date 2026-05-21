#!/usr/bin/env node
// Forbid `/api/admin/` URL literals in chat-relevant frontend code.
//
// Rationale (01c open question 3, locked): admin endpoints (user
// management, etc.) are still served at `/api/admin/*` during Phase
// 1; chat MUST be on `/api/v1/*`. To keep that invariant from
// silently regressing we scan `web/src/` and fail on any literal
// `/api/admin/` match outside of the safety pages (which still talk
// to the legacy admin routes — Phase 4 will migrate them) and this
// scanner itself.
//
// Run as: `npm run lint:no-admin-chat`
// Exits 0 on clean, 1 on violation.

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, '..', 'src');

// Files that are *allowed* to mention `/api/admin/` because they
// own the (yet-to-migrate) safety admin surface. Listed by path
// suffix relative to `web/src/` so renames within the safety
// component don't silently re-open the loophole.
const ALLOWLIST = new Set([
  // Safety pages still call the admin surface (Phase 4 migration).
  'components/safety-rules-page.ts',
  'components/safety-decisions-page.ts',
  'services/safety-admin-client.ts',
]);

const PATTERN = /\/api\/admin\//g;

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
  let m;
  PATTERN.lastIndex = 0;
  while ((m = PATTERN.exec(text)) !== null) {
    bad += 1;
    const line = text.slice(0, m.index).split('\n').length;
    process.stderr.write(
      `${rel}:${line}: forbidden \`/api/admin/\` literal in chat-relevant code\n`,
    );
  }
}

if (bad > 0) {
  process.stderr.write(
    `\n${bad} violation(s). Use the typed v1 client (ApiClient) or, ` +
      `for safety pages, add the file to the ALLOWLIST in ` +
      `scripts/lint-no-admin-chat.mjs.\n`,
  );
  process.exit(1);
}
process.stdout.write('lint-no-admin-chat: clean\n');
