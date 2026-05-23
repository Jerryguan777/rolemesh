#!/usr/bin/env node
// Forbid hard-coded colour hex / font-family literals in v2 source.
//
// Why: v2 ships a token palette (`src/styles/tokens.css`) so dark
// mode and the upcoming OIDC tenant theming swap one variable rather
// than every site-of-use. The migration is half-done; this lint
// keeps NEW hard-codes from sneaking back in while we burn down the
// existing pile.
//
// Detection rules:
//   * `#RGB` / `#RRGGBB` / `#RRGGBBAA` inside source files.
//   * `font-family: '...'` / `font-family: "..."` literals (not
//     `font-family: var(--rm-font-body)` and not `font-family: inherit`).
//
// Scope: `web/src/**/*.{ts,tsx,css}` excluding:
//   * `src/styles/tokens.css`   — owns the palette itself
//   * `src/api/generated/**`    — codegen'd; not our authoring surface
//   * any `*.test.ts`           — fixture colours / spy keys; not UI
//   * lit `svg`` style attrs   — currentColor handled via stroke="currentColor"
//
// Exits 0 on clean, 1 on violation. Run with
// `npm run lint:tokens-only`.

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, '..', 'src');

// File-level allow list. Paths are relative to `web/src/`.
const ALLOWLIST_FILES = new Set([
  'styles/tokens.css',
  // The chat-shell file owns the v2 coworker-avatar palette inline
  // (`AVATAR_COLOURS`) — that palette is functional (deterministic
  // user-identification colour), not a brand token. Leaving as a
  // tracked known-exception until v3 lifts it into tokens.css.
  'components/chat-shell.ts',
  // Legacy global stylesheet predating the token system. v2-C lint
  // ships in lockfile mode — these need a dedicated migration chore
  // (~25 colour literals + 2 font literals). Tracked in v2-C
  // Findings; remove from allowlist once migrated.
  'app.css',
  // v1.0 login page rendered with raw hex before tokens.css existed.
  // Same chore as app.css — burn down together, not piecemeal.
  'components/login-page.ts',
]);

const ALLOWLIST_PREFIXES = ['api/generated/'];

const HEX_RE = /#[0-9a-fA-F]{3,8}\b/g;
const FONT_LITERAL_RE = /font-family\s*:\s*['"]/g;

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
    } else if (s.isFile() && /\.(ts|tsx|css)$/.test(p)) {
      out.push(p);
    }
  }
}

function isAllowed(rel) {
  if (ALLOWLIST_FILES.has(rel)) return true;
  if (rel.endsWith('.test.ts')) return true;
  for (const prefix of ALLOWLIST_PREFIXES) {
    if (rel.startsWith(prefix)) return true;
  }
  return false;
}

const files = [];
walk(ROOT, files);

const violations = [];

for (const f of files) {
  const rel = relative(ROOT, f);
  if (isAllowed(rel)) continue;
  const text = readFileSync(f, 'utf8');

  // Hex colour literals.
  let m;
  HEX_RE.lastIndex = 0;
  while ((m = HEX_RE.exec(text)) !== null) {
    if (isInComment(text, m.index)) continue;
    // Skip 4-char hash IDs like `#abc` only if the next char is not a
    // hex digit — but a strict hex-colour regex already required 3+
    // hex chars; we still trim out the case where the hex is used as
    // a fragment URL (`href="#foo"`) by requiring at least one digit.
    const literal = m[0];
    if (!/[0-9]/.test(literal)) continue;
    // 3 / 4 / 6 / 8 char hashes only — anything else is unlikely to
    // be a CSS colour and likelier to be a git sha in a comment.
    const len = literal.length - 1;
    if (![3, 4, 6, 8].includes(len)) continue;
    const line = text.slice(0, m.index).split('\n').length;
    violations.push({ rel, line, kind: 'hex', literal });
  }

  // font-family string literals.
  FONT_LITERAL_RE.lastIndex = 0;
  while ((m = FONT_LITERAL_RE.exec(text)) !== null) {
    if (isInComment(text, m.index)) continue;
    const line = text.slice(0, m.index).split('\n').length;
    violations.push({ rel, line, kind: 'font', literal: m[0].trim() });
  }
}

if (violations.length > 0) {
  for (const v of violations) {
    process.stderr.write(
      `${v.rel}:${v.line}: hard-coded ${v.kind} ${v.literal} — ` +
        `use a token var(--rm-...) from styles/tokens.css\n`,
    );
  }
  process.stderr.write(
    `\n${violations.length} violation(s). Promote to a token or ` +
      `add the file to ALLOWLIST_FILES with a written reason.\n`,
  );
  process.exit(1);
}
process.stdout.write('lint-tokens-only: clean\n');
