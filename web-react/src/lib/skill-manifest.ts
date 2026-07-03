// SKILL.md frontmatter assemble/strip — ported verbatim from the Lit
// skill-dialog.ts (parseSkillMd / serializeSkillMd) so the round-trip
// is byte-identical between the two SPAs. A subtly-different regex here
// would corrupt SKILL.md on a load→edit→save cycle, so this is a
// straight port with the Lit tests carried alongside.
//
// Mental model (kept from Lit): a skill is a short name + a description
// + a body of instructions. YAML frontmatter is NEVER shown to the
// user — the dialog assembles `---\nname: X\ndescription: Y\n---\n{body}`
// on save and strips it back on load.

/** Parse a SKILL.md blob into its description (from frontmatter) and
 *  the body that follows. Liberal grammar: leading whitespace
 *  tolerated, `---` delimiters optional. Anything we can't parse shows
 *  up as the body with an empty description. */
export function parseSkillMd(raw: string): { description: string; body: string } {
  const trimmed = raw.trimStart();
  if (!trimmed.startsWith('---')) {
    return { description: '', body: raw };
  }
  // Find the closing `---` on its own line.
  const afterOpen = trimmed.slice(3); // skip leading ---
  const closeIdx = afterOpen.search(/(^|\n)---\s*(\n|$)/);
  if (closeIdx === -1) {
    return { description: '', body: raw };
  }
  const fm = afterOpen.slice(0, closeIdx);
  // Strip the trailing closing fence + any leading newline of the body.
  const restStart = afterOpen.indexOf('---', closeIdx) + 3;
  const body = afterOpen.slice(restStart).replace(/^\n/, '');
  // Pull description out of the YAML-ish frontmatter. A single line
  // `description: …` is the only field we care about.
  const descMatch = fm.match(/(^|\n)description:\s*(.*)/);
  const description = descMatch ? descMatch[2].trim() : '';
  return { description, body };
}

/** Re-assemble a SKILL.md blob from the dialog's 3 inputs. */
export function serializeSkillMd(
  name: string,
  description: string,
  body: string,
): string {
  // Guard the colon-and-newline case for a single-line YAML value.
  const safeDescription = description.replace(/\n/g, ' ').trim();
  // Strip any existing leading frontmatter from `body` to avoid
  // double-wrapping if the user pasted raw markdown back into the
  // textarea.
  const cleanBody = body.replace(/^---[\s\S]*?\n---\n?/, '').replace(/^\n+/, '');
  return `---\nname: ${name}\ndescription: ${safeDescription}\n---\n${cleanBody}`;
}
