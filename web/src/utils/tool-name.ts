/**
 * Format a raw Claude / Agent tool identifier into something readable.
 *
 * Patterns:
 *   "mcp__rolemesh__send_message" → "rolemesh › send_message"
 *   "mcp__filesystem__read_text"  → "filesystem › read_text"
 *   "bash"                        → "bash"
 *   ""                            → ""
 *
 * The frontend owns this formatting so the orchestrator can keep
 * shipping the raw tool name (no backend dictionary to maintain). When
 * new MCP servers are added, this function still produces a sensible
 * label without any code changes.
 *
 * MCP tools follow the convention ``mcp__<server>__<tool>`` where the
 * tool slug may itself contain ``_`` characters but the boundary uses
 * exactly ``__``. We split on the double-underscore so a tool name
 * like ``read_text_file`` is preserved verbatim after the separator.
 */
export function beautifyToolName(raw: string | null | undefined): string {
  if (!raw) return '';
  const parts = raw.split('__');
  if (parts.length >= 3 && parts[0] === 'mcp') {
    const server = parts[1];
    const tool = parts.slice(2).join('__');
    return `${server} › ${tool}`;
  }
  return raw;
}
