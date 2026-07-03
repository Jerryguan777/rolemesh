// Slug derive/validate logic — ported from the top of
// web/src/components/coworker-wizard.ts (kept byte-equivalent so the
// two SPAs derive identical folders from identical names). Pure module
// colocated with the wizard per the §1.1 settings growth rule.
//
// The slug names the coworker's container mount path; the regex
// mirrors the contract's `CoworkerCreate.folder` pattern. It is
// immutable after create (`CoworkerUpdate` has no `folder` field).

export function slugify(name: string): string {
  const lowered = name.toLowerCase();
  // Replace any char that's not a-z 0-9 _ - with -.
  const replaced = lowered.replace(/[^a-z0-9_-]+/g, '-');
  // Collapse runs of `-`.
  const collapsed = replaced.replace(/-+/g, '-');
  // Trim leading non-alphanumeric (per backend regex). Numbers OK as
  // first char per `^[a-z0-9][a-z0-9_-]{0,63}$`.
  const trimmed = collapsed.replace(/^[^a-z0-9]+/, '');
  // Trim trailing `-` for tidiness (regex allows trailing `-` but it
  // looks weird as a derived slug).
  return trimmed.replace(/-+$/, '').slice(0, 64);
}

const SLUG_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;

export function isValidSlug(s: string): boolean {
  return SLUG_RE.test(s);
}
