// Skill filter-chip classification (spec E.1, Lit parity). UX-only
// re-classification of the already server-visibility-filtered list —
// NEVER a security gate. Pure so it's testable.

import type { SkillSummary } from '../../../api/client';
import { isOwnResource } from '../../../lib/capabilities';

export type SkillChip = 'all' | 'mine' | 'shared';

export const SKILL_CHIPS: { id: SkillChip; label: string }[] = [
  { id: 'all', label: 'All visible' },
  { id: 'mine', label: 'Mine' },
  { id: 'shared', label: 'Shared by others' },
];

/** `mine` = own rows; `shared` = shared AND not own (own shared rows
 *  stay under Mine); `all` = everything visible. */
export function chipMatches(chip: SkillChip, skill: SkillSummary): boolean {
  if (chip === 'all') return true;
  if (chip === 'mine') return isOwnResource(skill);
  return skill.visibility === 'shared' && !isOwnResource(skill);
}

/** Empty-state copy varies by chip (Lit parity). */
export function chipEmptyCopy(chip: SkillChip, anySkills: boolean): string {
  if (!anySkills) return 'No skills yet.';
  if (chip === 'mine') return 'You have not created any skills yet.';
  if (chip === 'shared') return 'No one has shared a skill with you yet.';
  return 'No skills yet.';
}
