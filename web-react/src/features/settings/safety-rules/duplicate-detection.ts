// G3 duplicate-rule detection (spec §6.10a / I.3) — the pure half of the
// dialog's state machine, extracted so the collision predicate is
// unit-testable. Triple = (check_id, coworker_id, stage); disabled rules
// still collide (they are one toggle from running). The dialog owns the
// stateful part (auto-flip / force-create / reset on triple change).

import type { SafetyRule, SafetyStage } from '../../../api/client';

export interface DuplicateHit {
  /** Editable org-tier rule with the same triple → the dialog auto-flips
   *  to editing it. */
  orgMatch: SafetyRule | null;
  /** Platform-tier rule covering the same triple → subtle FYI banner
   *  only (the user cannot edit platform rules). */
  platformMatch: SafetyRule | null;
}

export function findDuplicate(
  rules: SafetyRule[],
  triple: { checkId: string; stage: SafetyStage | ''; coworkerId: string | null },
  /** The duplicate-source rule id — excluded so duplicating a rule does
   *  not detect the rule against itself. */
  excludeId?: string,
): DuplicateHit {
  if (!triple.checkId || !triple.stage) {
    return { orgMatch: null, platformMatch: null };
  }
  const matches = (r: SafetyRule) =>
    r.check_id === triple.checkId &&
    r.stage === triple.stage &&
    (r.coworker_id ?? null) === triple.coworkerId;

  const orgMatch =
    rules.find(
      (r) => r.source !== 'platform' && matches(r) && r.id !== excludeId,
    ) ?? null;
  // Platform overlap only reported when there is no org collision — the
  // org auto-flip takes precedence (Lit ordering).
  const platformMatch = orgMatch
    ? null
    : (rules.find((r) => r.source === 'platform' && matches(r)) ?? null);
  return { orgMatch, platformMatch };
}
