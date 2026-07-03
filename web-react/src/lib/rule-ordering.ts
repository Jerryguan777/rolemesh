// Rule-list ordering + priority badge tint — graduated from
// features/settings/approval-policies/ when the safety-rules page became
// the second consumer (§1.1: pure + ≥2 consumers → lib/). Both pages
// list rules in SERVER EVALUATION ORDER and share the badge scale.

/** List order = server evaluation order: priority desc, then the newest
 *  first on ties. `created_at` is an ISO-8601 string from the API. */
export function sortByEvaluationOrder<
  T extends { priority: number; created_at: string },
>(rows: T[]): T[] {
  return [...rows].sort(
    (a, b) =>
      b.priority - a.priority ||
      Date.parse(b.created_at) - Date.parse(a.created_at),
  );
}

/** Badge tint class for a priority value (Appendix C.3): amber when ≥10,
 *  muted when exactly 0, neutral otherwise. Classes live in ui.css. */
export function priorityBadgeClass(priority: number): string {
  if (priority >= 10) return ' pol-pri--hi';
  if (priority === 0) return ' pol-pri--zero';
  return '';
}
