// Ported from web/src/components/condition-form.test.ts alongside the
// module (H.5: copy WITH tests), plus sentence/round-trip additions for
// the read-only fallback path.
import { describe, expect, it } from 'vitest';

import {
  buildConditionExpr,
  conditionSentence,
  exprToForm,
  formatValue,
  parseValue,
  type LeafRow,
} from './condition-form';

const leaf = (field: string, op: LeafRow['op'], value: string): LeafRow => ({
  field,
  op,
  value,
});

describe('parseValue', () => {
  it('parses a numeric string to a number', () => {
    expect(parseValue('100')).toBe(100);
  });
  it('parses a JSON array to an array', () => {
    expect(parseValue('["USD","EUR"]')).toEqual(['USD', 'EUR']);
  });
  it('parses booleans', () => {
    expect(parseValue('true')).toBe(true);
  });
  it('keeps a bare word as a string (JSON.parse would throw)', () => {
    expect(parseValue('USD')).toBe('USD');
  });
  it('treats empty input as an empty string, not undefined', () => {
    expect(parseValue('   ')).toBe('');
  });
});

describe('buildConditionExpr', () => {
  it('always-mode → {always:true}', () => {
    expect(
      buildConditionExpr({ mode: 'always', connective: 'and', rows: [] }),
    ).toEqual({ always: true });
  });

  it('a single leaf is emitted bare (no connective wrapper)', () => {
    expect(
      buildConditionExpr({
        mode: 'match',
        connective: 'and',
        rows: [leaf('amount', '>', '100')],
      }),
    ).toEqual({ field: 'amount', op: '>', value: 100 });
  });

  it('multiple leaves wrap in the chosen connective', () => {
    expect(
      buildConditionExpr({
        mode: 'match',
        connective: 'or',
        rows: [leaf('a', '==', '1'), leaf('b', '==', '2')],
      }),
    ).toEqual({
      or: [
        { field: 'a', op: '==', value: 1 },
        { field: 'b', op: '==', value: 2 },
      ],
    });
  });

  it('drops rows with a blank field', () => {
    expect(
      buildConditionExpr({
        mode: 'match',
        connective: 'and',
        rows: [leaf('', '==', 'x'), leaf('amount', '>', '5')],
      }),
    ).toEqual({ field: 'amount', op: '>', value: 5 });
  });

  it('match-mode with no usable rows degrades to the conservative gate', () => {
    // Never produce "approve nothing" — an empty match means require approval.
    expect(
      buildConditionExpr({
        mode: 'match',
        connective: 'and',
        rows: [leaf('', '==', '')],
      }),
    ).toEqual({ always: true });
  });

  it('an `in` op carries a list value through', () => {
    expect(
      buildConditionExpr({
        mode: 'match',
        connective: 'and',
        rows: [leaf('currency', 'in', '["USD","EUR"]')],
      }),
    ).toEqual({ field: 'currency', op: 'in', value: ['USD', 'EUR'] });
  });
});

describe('exprToForm', () => {
  it('round-trips a single leaf', () => {
    const form = exprToForm({ field: 'amount', op: '>', value: 100 });
    expect(form.editable).toBe(true);
    expect(form.mode).toBe('match');
    expect(form.rows).toEqual([{ field: 'amount', op: '>', value: '100' }]);
    // ...and rebuilds to the same expr.
    expect(buildConditionExpr(form)).toEqual({ field: 'amount', op: '>', value: 100 });
  });

  it('round-trips a flat or-of-leaves', () => {
    const expr = {
      or: [
        { field: 'a', op: '==', value: 'x' },
        { field: 'b', op: '!=', value: 2 },
      ],
    };
    const form = exprToForm(expr);
    expect(form.editable).toBe(true);
    expect(form.connective).toBe('or');
    expect(form.rows).toHaveLength(2);
    expect(buildConditionExpr(form)).toEqual(expr);
  });

  it('round-trips a string-list `in` value back to its JSON text', () => {
    const expr = { field: 'currency', op: 'in', value: ['USD', 'EUR'] };
    const form = exprToForm(expr);
    expect(form.rows[0].value).toBe('["USD","EUR"]');
    expect(buildConditionExpr(form)).toEqual(expr);
  });

  it('marks a clean {always:true} editable in always-mode', () => {
    const form = exprToForm({ always: true });
    expect(form).toMatchObject({ mode: 'always', editable: true });
  });

  it('refuses a nested connective (not editable in the flat builder)', () => {
    const form = exprToForm({
      and: [{ or: [{ field: 'a', op: '==', value: 1 }] }],
    });
    expect(form.editable).toBe(false);
  });

  it('refuses an unknown op', () => {
    expect(exprToForm({ field: 'a', op: 'regex', value: '.*' }).editable).toBe(
      false,
    );
  });

  it('refuses a mixed-form node (always + leaf keys)', () => {
    expect(
      exprToForm({ always: true, field: 'x', op: '==', value: 1 }).editable,
    ).toBe(false);
  });

  it('refuses a non-object', () => {
    expect(exprToForm('nope').editable).toBe(false);
    expect(exprToForm(null).editable).toBe(false);
  });
});

describe('formatValue', () => {
  it('quotes strings so "5000" is distinguishable from 5000', () => {
    expect(formatValue('5000')).toBe('"5000"');
    expect(formatValue(5000)).toBe('5000');
  });
  it('renders null and booleans bare', () => {
    expect(formatValue(null)).toBe('null');
    expect(formatValue(false)).toBe('false');
  });
  it('renders arrays as JSON', () => {
    expect(formatValue(['USD'])).toBe('["USD"]');
  });
});

describe('conditionSentence', () => {
  it('always:true → every time; always:false → never', () => {
    expect(conditionSentence({ always: true })).toBe('every time');
    expect(conditionSentence({ always: false })).toBe('never');
  });
  it('renders a leaf bolded with the display-form value', () => {
    expect(conditionSentence({ field: 'amount', op: '>', value: 5000 })).toBe(
      'when <b>amount &gt; 5000</b>',
    );
  });
  it('joins a flat or with OR', () => {
    expect(
      conditionSentence({
        or: [
          { field: 'a', op: '==', value: 1 },
          { field: 'b', op: '==', value: 2 },
        ],
      }),
    ).toBe('when <b>a == 1 OR b == 2</b>');
  });
  it('escapes HTML in leaf text', () => {
    expect(
      conditionSentence({ field: '<img>', op: '==', value: 'x' }),
    ).toContain('&lt;img&gt;');
  });
  it('collapses nested shapes to the advanced-condition note', () => {
    expect(
      conditionSentence({ and: [{ or: [{ field: 'a', op: '==', value: 1 }] }] }),
    ).toBe('when <i>(advanced condition)</i>');
  });
});
