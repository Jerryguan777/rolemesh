// Pure helpers for the approval-policy condition builder
// (docs/21-hitl-approval-plan.md §7 / §10 S5). Kept separate from the page
// component so the (lossy, structured) form ⇄ condition_expr mapping is
// unit-testable on its own.
//
// The §7 grammar this UI exposes is intentionally a *subset* of what the
// matcher accepts: the builder produces either `{"always": true}` or a single
// flat `and`/`or` of leaf comparisons. Arbitrary nesting is valid on the wire
// (and round-trips through the REST API untouched), but the form only edits the
// shallow shapes — `exprToForm` reports `editable: false` for anything deeper
// so the page can fall back to read-only / raw display instead of silently
// flattening a nested policy.

import type { ConditionExpr } from '../api/client.js';

export const CONDITION_OPS = [
  '==',
  '!=',
  '>',
  '>=',
  '<',
  '<=',
  'in',
  'not_in',
  'contains',
] as const;

export type ConditionOp = (typeof CONDITION_OPS)[number];

export interface LeafRow {
  field: string;
  op: ConditionOp;
  /** Raw text from the input; parsed to a JSON value (or kept as a string)
   *  by {@link parseValue} when the expression is built. */
  value: string;
}

export type ConditionMode = 'always' | 'match';

export interface ConditionForm {
  mode: ConditionMode;
  connective: 'and' | 'or';
  rows: LeafRow[];
  /** False when the loaded expression is too complex for the structured
   *  builder (deep nesting, mixed forms) — the page shows it read-only. */
  editable: boolean;
}

/** Parse a raw value string into the JSON value the comparison should use.
 *
 *  `"100"` → number 100, `"true"` → boolean, `'["USD","EUR"]'` → array,
 *  `"USD"` → the string `"USD"` (JSON.parse throws → fall back to raw). This
 *  is what lets `amount > 100` compare numerically while `currency in
 *  ["USD","EUR"]` compares against a real list. */
export function parseValue(raw: string): unknown {
  const trimmed = raw.trim();
  if (trimmed === '') return '';
  try {
    return JSON.parse(trimmed);
  } catch {
    return raw;
  }
}

/** Build a condition_expr from the structured form. */
export function buildConditionExpr(form: {
  mode: ConditionMode;
  connective: 'and' | 'or';
  rows: LeafRow[];
}): ConditionExpr {
  if (form.mode === 'always') {
    return { always: true };
  }
  const leaves = form.rows
    .filter((r) => r.field.trim() !== '')
    .map((r) => ({
      field: r.field.trim(),
      op: r.op,
      value: parseValue(r.value),
    }));
  if (leaves.length === 0) {
    // A "match" mode with no usable rows degrades to always-require — the
    // conservative gate, never accidentally "approve nothing".
    return { always: true };
  }
  if (leaves.length === 1) {
    return leaves[0] as unknown as ConditionExpr;
  }
  return { [form.connective]: leaves } as unknown as ConditionExpr;
}

/** Reverse of {@link parseValue} for display in a sentence (§5.12).
 *
 *  Numbers / booleans render bare, `null` as `null`, strings quoted (so the
 *  reader can tell `"5000"` the string from `5000` the number), arrays/objects
 *  as JSON. This is the *display* form — distinct from {@link leafToRow}'s
 *  edit form, which strips the quotes off a string so the input is editable. */
export function formatValue(value: unknown): string {
  if (value === null) return 'null';
  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }
  if (typeof value === 'string') {
    return `"${value.replace(/"/g, '\\"')}"`;
  }
  return JSON.stringify(value);
}

/** Escape a string for safe interpolation into the `conditionSentence` HTML. */
function esc(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function leafSentence(leaf: Leaf): string {
  return esc(`${leaf.field ?? '?'} ${leaf.op ?? '?'} ${formatValue(leaf.value)}`);
}

/** Human-readable, HTML-safe rendering of a `condition_expr` (Appendix C.3).
 *
 *  The single source of truth for both the list-card subtitle and the dialog's
 *  live preview — the same sentence appears in both places so what the user
 *  previews is exactly what the card will show. The condition body is wrapped
 *  in `<b>…</b>`; callers must render the result with `unsafeHTML` (the leaf
 *  text is escaped, so this is safe). Anything the flat builder can't express
 *  (deep nesting, mixed forms) collapses to an `(advanced condition)` note
 *  rather than lying about the shape.
 *
 *  Returns the *clause* only ("every time" / "when <b>…</b>"); callers add the
 *  surrounding "→ pause to confirm" / full-sentence framing. */
export function conditionSentence(expr: unknown): string {
  if (typeof expr !== 'object' || expr === null) return 'every time';
  const obj = expr as Record<string, unknown>;
  if ('always' in obj) {
    return obj.always === false ? 'never' : 'every time';
  }
  if (isLeaf(obj)) {
    return `when <b>${leafSentence(obj)}</b>`;
  }
  for (const [connective, word] of [
    ['and', 'AND'],
    ['or', 'OR'],
  ] as const) {
    const subs = obj[connective];
    if (Array.isArray(subs) && subs.length > 0 && subs.every(isLeaf)) {
      return `when <b>${(subs as Leaf[]).map(leafSentence).join(` ${word} `)}</b>`;
    }
  }
  return 'when <i>(advanced condition)</i>';
}

interface Leaf {
  field: string;
  op: string;
  value: unknown;
}

function isLeaf(node: unknown): node is Leaf {
  if (typeof node !== 'object' || node === null) return false;
  const keys = Object.keys(node).sort();
  return (
    keys.length === 3 &&
    keys[0] === 'field' &&
    keys[1] === 'op' &&
    keys[2] === 'value'
  );
}

function leafToRow(leaf: Leaf): LeafRow | null {
  if (!CONDITION_OPS.includes(leaf.op as ConditionOp)) return null;
  // Reverse of parseValue: a string round-trips bare, everything else as JSON
  // so the input shows `["USD","EUR"]` / `100`, not `[object Object]`.
  const value =
    typeof leaf.value === 'string' ? leaf.value : JSON.stringify(leaf.value);
  return { field: String(leaf.field), op: leaf.op as ConditionOp, value };
}

function emptyRow(): LeafRow {
  return { field: '', op: '==', value: '' };
}

/** Best-effort reverse of {@link buildConditionExpr}, for the edit flow.
 *
 *  Recognises `{always}`, a single leaf, and a flat `and`/`or` of leaves.
 *  Anything else (nested connectives, unknown ops) → `editable: false` so the
 *  caller renders it raw instead of corrupting it. */
export function exprToForm(expr: unknown): ConditionForm {
  const fallback: ConditionForm = {
    mode: 'match',
    connective: 'and',
    rows: [emptyRow()],
    editable: false,
  };
  if (typeof expr !== 'object' || expr === null) return fallback;
  const obj = expr as Record<string, unknown>;

  if ('always' in obj) {
    // Only a clean `{always: bool}` is editable; a mixed node is not.
    if (Object.keys(obj).length === 1 && typeof obj.always === 'boolean') {
      return { mode: 'always', connective: 'and', rows: [emptyRow()], editable: true };
    }
    return fallback;
  }

  for (const connective of ['and', 'or'] as const) {
    if (connective in obj) {
      if (Object.keys(obj).length !== 1) return fallback;
      const subs = obj[connective];
      if (!Array.isArray(subs) || subs.length === 0) return fallback;
      const rows: LeafRow[] = [];
      for (const sub of subs) {
        if (!isLeaf(sub)) return fallback;
        const row = leafToRow(sub);
        if (!row) return fallback;
        rows.push(row);
      }
      return { mode: 'match', connective, rows, editable: true };
    }
  }

  if (isLeaf(obj)) {
    const row = leafToRow(obj);
    if (!row) return fallback;
    return { mode: 'match', connective: 'and', rows: [row], editable: true };
  }

  return fallback;
}

export { emptyRow };
