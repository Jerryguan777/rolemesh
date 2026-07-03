import { describe, expect, it } from 'vitest';
import { parseSkillMd, serializeSkillMd } from './skill-manifest';

describe('parseSkillMd', () => {
  it('extracts description from frontmatter and returns the body', () => {
    const raw = '---\nname: pdf-toolkit\ndescription: Handles PDFs\n---\nBody text here';
    expect(parseSkillMd(raw)).toEqual({ description: 'Handles PDFs', body: 'Body text here' });
  });

  it('treats a blob with no frontmatter as all-body, empty description', () => {
    expect(parseSkillMd('just markdown')).toEqual({ description: '', body: 'just markdown' });
  });

  it('returns raw as body when the closing fence is missing', () => {
    const raw = '---\nname: x\ndescription: y\nno close';
    expect(parseSkillMd(raw)).toEqual({ description: '', body: raw });
  });

  it('tolerates leading whitespace before the opening fence', () => {
    const raw = '\n\n---\ndescription: Trimmed\n---\nB';
    expect(parseSkillMd(raw).description).toBe('Trimmed');
  });
});

describe('serializeSkillMd', () => {
  it('assembles name/description/body into a manifest', () => {
    expect(serializeSkillMd('pdf-toolkit', 'Handles PDFs', 'Body')).toBe(
      '---\nname: pdf-toolkit\ndescription: Handles PDFs\n---\nBody',
    );
  });

  it('flattens newlines in the description to keep single-line YAML', () => {
    expect(serializeSkillMd('x', 'line1\nline2', 'B')).toContain('description: line1 line2');
  });

  it('strips existing leading frontmatter from the body (no double-wrap)', () => {
    const body = '---\nname: stale\ndescription: old\n---\nreal body';
    expect(serializeSkillMd('x', 'new', body)).toBe(
      '---\nname: x\ndescription: new\n---\nreal body',
    );
  });

  it('round-trips: serialize then parse recovers description + body', () => {
    const manifest = serializeSkillMd('name-a', 'A skill', 'The instructions');
    expect(parseSkillMd(manifest)).toEqual({ description: 'A skill', body: 'The instructions' });
  });
});
