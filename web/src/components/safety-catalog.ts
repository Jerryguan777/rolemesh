// Safety-check presentation catalog + sentence / action helpers (spec §6).
//
// WHY THIS FILE EXISTS — the wire/presentation split.
// `GET /api/v1/safety/checks` returns `SafetyCheck[]` carrying only the
// *behaviour* of each check: which `stages` it runs at, its `action_model`,
// the `natural_actions` a hit produces, the `supported_actions` a rule may
// pick, and `cost_class`. It does NOT carry the human label, description,
// category grouping, or which config form to render — those are pure
// presentation and live here, keyed by `check_id`. Components merge the two:
// behaviour from the wire (never hardcoded), presentation from this catalog.
//
// USER-FACING LANGUAGE (spec §8.5). Nothing here leaks engineering taxonomy.
// `action_model` ("fixed" / "config_routed" / "aggregated") drives which
// editor experience renders but is never shown; "inert" becomes "running but
// doing nothing"; stages get friendly phrases ("before tool calls"), with the
// mono enum kept only for deep-inspection surfaces (the decision detail modal).

import type {
  SafetyCheck,
  SafetyStage,
  SafetyVerdictAction,
} from '../api/client.js';

// Which config form a check renders — a presentation hint derived from the
// check's config_schema (spec §6.12). Not on the wire.
export type CfgKind =
  | 'pii-entities'
  | 'presidio-routing'
  | 'moderation-routing'
  | 'threshold'
  | 'host-list'
  | 'secret-plugins';

export interface CheckPresentation {
  /** Human label — shown everywhere a check is named. Never the raw id. */
  label: string;
  /** One-line "what this check does", shown under the dialog's check select. */
  desc: string;
  /** Group heading in the check `<optgroup>` (Sensitive data / … / Network). */
  category: string;
  /** Which config form to render; absent ⇒ the check takes no config. */
  cfgKind?: CfgKind;
}

// Presentation metadata, keyed by registered check id. Keys MUST match the
// ids the backend registry exposes (verified by the catalog-coverage test).
// Behaviour fields (stages/action_model/…) are intentionally absent — they
// come from the wire SafetyCheck so the two never drift.
export const SAFETY_CHECK_CATALOG: Record<string, CheckPresentation> = {
  'pii.regex': {
    label: 'Personal data (regex)',
    desc: 'Fast pattern matchers for SSN, credit cards, email, phone, and IP addresses. Cheap enough to run before every tool call; for broader coverage use the Presidio check.',
    category: 'Sensitive data',
    cfgKind: 'pii-entities',
  },
  'presidio.pii': {
    label: 'Personal data (Presidio)',
    desc: 'Broader entity coverage (names, locations, emails, and more) with an adjustable confidence threshold. The only check that can rewrite a payload — required for redaction.',
    category: 'Sensitive data',
    cfgKind: 'presidio-routing',
  },
  secret_scanner: {
    label: 'Secrets & credentials',
    desc: 'Scans for API keys, tokens, passwords, and cloud credentials. Run it on outputs to stop a coworker from leaking a secret. Runs all built-in detectors automatically — no configuration needed.',
    category: 'Sensitive data',
    // No cfgKind: backend SecretScannerConfig only has action_override.
    // The dialog shows a plain info note instead of a config form.
    cfgKind: 'secret-plugins' as const,
  },
  domain_allowlist: {
    label: 'Allowed domains only',
    desc: 'Blocks any tool call whose URL reaches a host that is not on the allowlist. Complements network egress control.',
    category: 'Network',
    cfgKind: 'host-list',
  },
  'egress.domain_rule': {
    label: 'Egress domain rule',
    desc: 'Restricts the hosts a coworker can reach over the network. Enforced on every outbound request the coworker makes.',
    category: 'Network',
    cfgKind: 'host-list',
  },
  'llm_guard.prompt_injection': {
    label: 'Prompt injection',
    desc: 'Catches attempts to hijack the coworker through instructions hidden in user input or tool output.',
    category: 'Adversarial input',
    cfgKind: 'threshold',
  },
  'llm_guard.jailbreak': {
    label: 'Jailbreak attempts',
    desc: 'Detects attempts to talk the coworker out of its guardrails (role-play exploits and similar tricks).',
    category: 'Adversarial input',
    cfgKind: 'threshold',
  },
  'llm_guard.toxicity': {
    label: 'Toxic content',
    desc: 'Flags hateful, harassing, or abusive language on either side of the conversation.',
    category: 'Content',
    cfgKind: 'threshold',
  },
  openai_moderation: {
    label: 'Content moderation',
    desc: 'Runs text through moderation categories (sexual / hate / harassment / self-harm / violence) and lets you choose what to do for each.',
    category: 'Content',
    cfgKind: 'moderation-routing',
  },
};

// Category render order for the check `<optgroup>` list — most-reached for
// the common case first.
export const SAFETY_CATEGORY_ORDER = [
  'Sensitive data',
  'Adversarial input',
  'Content',
  'Network',
];

// Action ladder, severity-ascending — the stable order the segmented control
// renders regardless of which subset a (check, stage) supports.
export const SAF_ACTION_ORDER: SafetyVerdictAction[] = [
  'allow',
  'warn',
  'redact',
  'require_approval',
  'block',
];

// The only actions a rule may set via `config.action_override` (backend
// whitelist — see src/webui/admin.py `_validate_safety_rule_body`). `allow`
// is the absence of an override (a rule that "allows" everything is just the
// natural pass-through), and `redact` cannot be synthesized by an override
// (it needs a check that emits a modified payload — that path is the Presidio
// routing form, not an override). The action panel uses this to decide which
// non-natural actions are pickable, so the UI never produces a 400.
export const OVERRIDABLE_ACTIONS: ReadonlySet<SafetyVerdictAction> = new Set([
  'block',
  'warn',
  'require_approval',
]);

// Stages where a check failure fails CLOSED (the call is blocked). The other
// two (post_tool_result, pre_compaction) fail SAFE (the call is let through).
// Surfaced as a one-line helper in the dialog, never as "control/observational".
export const SAF_CONTROL_STAGES: ReadonlySet<string> = new Set([
  'input_prompt',
  'pre_tool_call',
  'model_output',
  'egress_request',
]);

// Long form for the stage `<select>` — teaches the concept in plain language.
export const SAF_STAGE_LABEL: Record<string, string> = {
  input_prompt: 'On user input — before the coworker processes it',
  pre_tool_call: 'Before a tool runs — check the tool arguments first',
  post_tool_result: 'After a tool returns — before the coworker sees the result',
  model_output: 'On the reply — before the user sees it',
  pre_compaction: 'Before conversation compaction',
  egress_request: 'On outbound network requests',
};

// Short friendly phrase for sentences/cards (NOT the mono enum — §8.5).
export const SAF_STAGE_SHORT: Record<string, string> = {
  input_prompt: 'on input',
  pre_tool_call: 'before tool calls',
  post_tool_result: 'on tool results',
  model_output: 'on the reply',
  pre_compaction: 'before compaction',
  egress_request: 'on network requests',
};

export const SAF_ACTION_LABEL: Record<SafetyVerdictAction, string> = {
  allow: 'Allow',
  warn: 'Warn',
  redact: 'Redact',
  require_approval: 'Approve',
  block: 'Block',
};

// Tiny sub-caption under each segmented-control button.
export const SAF_ACTION_SUB: Record<SafetyVerdictAction, string> = {
  allow: 'log only',
  warn: 'flag the coworker',
  redact: 'strip + continue',
  require_approval: 'ask the user',
  block: 'stop the call',
};

// Sentence verb for each action (spec §6.13).
export const SAF_ACTION_VERB: Record<SafetyVerdictAction, string> = {
  allow: 'let it through (audit only)',
  warn: 'let it through but flag it for the coworker',
  redact: 'strip the matched content and continue',
  require_approval: 'pause and ask the person in the chat',
  block: 'block the call',
};

// Map an action to the pill modifier class (defined in settings-pages.css).
// allow=green, warn=gray, redact=amber, require_approval=purple, block=red.
const SAF_ACTION_PILL: Record<SafetyVerdictAction, string> = {
  allow: 'rm-pill-on',
  warn: 'rm-pill-off',
  redact: 'rm-pill-warn',
  require_approval: 'rm-pill-appr',
  block: 'rm-pill-bad',
};

export function safActionPillClass(action: SafetyVerdictAction): string {
  return SAF_ACTION_PILL[action] ?? 'rm-pill-off';
}

// Why an action is unavailable for a (check, stage). The server is the source
// of truth for WHICH (via supported_actions); these strings only explain the
// common reasons in plain language. A reason not listed falls back to generic.
const SAF_UNSUPPORTED_REASON: Partial<Record<SafetyVerdictAction, string>> = {
  redact: 'Only the Presidio check can rewrite a payload, so redaction is not available here.',
  warn: 'Nothing reads a warning at this stage.',
  require_approval: 'There is no one to ask at this stage — the tool has already run, or there is no chat.',
};

export function unsupportedReason(action: SafetyVerdictAction): string {
  return (
    SAF_UNSUPPORTED_REASON[action] ??
    'Not available for this check at this stage.'
  );
}

// ---- behaviour lookups against the wire SafetyCheck ----

export function naturalAction(
  check: SafetyCheck | null,
  stage: SafetyStage,
): SafetyVerdictAction | null {
  const na = check?.natural_actions as
    | Partial<Record<string, SafetyVerdictAction>>
    | undefined;
  return na?.[stage] ?? null;
}

export function supportedActions(
  check: SafetyCheck | null,
  stage: SafetyStage,
): SafetyVerdictAction[] {
  const sa = check?.supported_actions as
    | Partial<Record<string, SafetyVerdictAction[]>>
    | undefined;
  return sa?.[stage] ?? [];
}

export interface ActionButtonState {
  enabled: boolean;
  /** Tooltip shown when disabled; empty when enabled. */
  reason: string;
}

// Whether a segmented-control button for `action` is pickable, and why not.
// A button is enabled when it is the natural action (the default — selecting
// it writes no override) OR it is both server-supported AND writable as an
// override. `allow`/`redact` therefore stay disabled on a block-natural check
// (you cannot downgrade to allow; redaction is configured on Presidio, not
// overridden) — which is exactly what the backend accepts.
export function actionButtonState(
  check: SafetyCheck | null,
  stage: SafetyStage,
  action: SafetyVerdictAction,
  natural: SafetyVerdictAction | null,
): ActionButtonState {
  if (action === natural) return { enabled: true, reason: '' };
  const supported = supportedActions(check, stage).includes(action);
  if (!supported) return { enabled: false, reason: unsupportedReason(action) };
  if (!OVERRIDABLE_ACTIONS.has(action)) {
    // Supported as a verdict, but not reachable as a rule override.
    const reason =
      action === 'allow'
        ? 'This check always acts on a hit. To stop it, disable the rule instead.'
        : unsupportedReason(action);
    return { enabled: false, reason };
  }
  return { enabled: true, reason: '' };
}

// ---- effective action (no top-level `action` field in production) ----

// The host-list checks express their whole intent as "allow these, block the
// rest", regardless of action_model — so their effective verdict for display
// is always block (for anything off the list).
function isHostList(checkId: string): boolean {
  return SAFETY_CHECK_CATALOG[checkId]?.cfgKind === 'host-list';
}

const ROUTED_VERB_PRIORITY: SafetyVerdictAction[] = [
  'redact',
  'block',
  'require_approval',
  'warn',
  'allow',
];

function representativeRoutedAction(
  routing: Record<string, unknown>,
): SafetyVerdictAction | null {
  const values = new Set(Object.values(routing).filter(Boolean) as string[]);
  if (values.size === 0) return null;
  for (const a of ROUTED_VERB_PRIORITY) if (values.has(a)) return a;
  return null;
}

/** A rule (saved or in-progress draft) reduced to what the sentence needs. */
export interface SentenceRule {
  check_id: string;
  stage: SafetyStage;
  config: Record<string, unknown>;
}

// The single action a rule effectively takes, for the card pill and preview.
// Returns null when a routed check has no routing yet (inert — no pill verb).
export function effectiveAction(
  rule: SentenceRule,
  check: SafetyCheck | null,
): SafetyVerdictAction | null {
  if (isHostList(rule.check_id)) return 'block';
  if (check?.action_model === 'config_routed') {
    if (rule.check_id === 'presidio.pii') {
      // Backend stores block_codes + redact_codes (not a routing dict).
      const blockCodes = (rule.config?.['block_codes'] as string[]) ?? [];
      const redactCodes = (rule.config?.['redact_codes'] as string[]) ?? [];
      if (redactCodes.length > 0) return 'redact';
      if (blockCodes.length > 0) return 'block';
      return null; // inert
    }
    if (rule.check_id === 'openai_moderation') {
      // Backend stores block_categories + warn_categories (not a routing dict).
      const blockCats = (rule.config?.['block_categories'] as string[]) ?? [];
      const warnCats = (rule.config?.['warn_categories'] as string[]) ?? [];
      if (blockCats.length > 0) return 'block';
      if (warnCats.length > 0) return 'warn';
      return null; // inert
    }
    return null;
  }
  const override = rule.config?.['action_override'];
  if (typeof override === 'string') return override as SafetyVerdictAction;
  return naturalAction(check, rule.stage) ?? 'block';
}

// ---- sentence rendering (HTML string; the single source of truth) ----

function esc(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

// Keys match the backend pattern keys (uppercase, per _CONFIG_KEY_TO_CODE).
const PII_REGEX_LABELS: Record<string, string> = {
  SSN: 'SSNs',
  CREDIT_CARD: 'credit cards',
  EMAIL: 'emails',
  PHONE_US: 'phones',
  IP_ADDRESS: 'IPs',
};
const PRESIDIO_ENTITY_LABELS: Record<string, string> = {
  EMAIL_ADDRESS: 'emails',
  PHONE_NUMBER: 'phones',
  US_SSN: 'SSNs',
  CREDIT_CARD: 'credit cards',
  PERSON: 'names',
  LOCATION: 'locations',
  IP_ADDRESS: 'IPs',
  DATE_TIME: 'dates',
};

// Subject+verb phrase describing what the check looks for, varying by check
// (spec §6.13). Returns plain text; the caller wraps it in the sentence.
export function safWhatPhrase(
  checkId: string,
  config: Record<string, unknown>,
): string {
  const pres = SAFETY_CHECK_CATALOG[checkId];
  if (checkId === 'pii.regex') {
    // Backend: { patterns: { SSN: true, CREDIT_CARD: true, ... } }
    const patterns = (config?.['patterns'] as Record<string, boolean> | undefined) ?? {};
    const keys = Object.keys(patterns).filter((k) => patterns[k]);
    const named = keys.map((k) => PII_REGEX_LABELS[k] ?? k);
    return `detect ${named.length ? named.join(', ') : 'configured personal data'}`;
  }
  if (checkId === 'presidio.pii') {
    // Backend: { block_codes: [...], redact_codes: [...] }
    const blockCodes = (config?.['block_codes'] as string[]) ?? [];
    const redactCodes = (config?.['redact_codes'] as string[]) ?? [];
    const total = blockCodes.length + redactCodes.length;
    if (total === 0)
      return 'scan for personal data (not configured yet — running but doing nothing)';
    const entries: [string, string][] = [
      ...blockCodes.map((c): [string, string] => [c, 'block']),
      ...redactCodes.map((c): [string, string] => [c, 'redact']),
    ];
    const summary = entries
      .slice(0, 3)
      .map(([k, a]) => `${PRESIDIO_ENTITY_LABELS[k] ?? k}→${a}`)
      .join(', ');
    const more = entries.length > 3 ? ` +${entries.length - 3} more` : '';
    return summary + more;
  }
  if (checkId === 'secret_scanner') {
    // SecretScannerConfig has no plugin selection — runs all detectors.
    return 'scan for secrets';
  }
  if (checkId === 'domain_allowlist') {
    // domain_allowlist backend key is `allowed_hosts` (DomainAllowlistConfig).
    const n = ((config?.['allowed_hosts'] as string[]) ?? []).length;
    return `allow only ${n || 'listed'} host${n === 1 ? '' : 's'}`;
  }
  if (checkId === 'egress.domain_rule') {
    // egress.domain_rule uses `domain_pattern` (single string per rule).
    const p = config?.['domain_pattern'] as string | undefined;
    return p ? `allow ${p}` : 'allow listed domains';
  }
  if (checkId === 'openai_moderation') {
    // Backend: { block_categories: [...], warn_categories: [...] }
    const blockCats = (config?.['block_categories'] as string[]) ?? [];
    const warnCats = (config?.['warn_categories'] as string[]) ?? [];
    const n = blockCats.length + warnCats.length;
    if (n === 0) return 'check content moderation (not configured yet)';
    return `flag ${n} categor${n === 1 ? 'y' : 'ies'}`;
  }
  if (pres?.cfgKind === 'threshold') {
    // llm_guard checks store threshold; presidio uses score_threshold on backend.
    const t = config?.['threshold'] ?? config?.['score_threshold'];
    const label = (pres.label || checkId).toLowerCase();
    return `detect ${label}${t != null ? ` (sensitivity ${t})` : ''}`;
  }
  return (pres?.label ?? checkId).toLowerCase();
}

// Human-readable rule sentence — used on list cards, the dialog live preview,
// and the delete-confirmation modal. Returns an HTML string (with <b>/<span>)
// for `unsafeHTML`; all dynamic text is escaped.
export function safSentence(
  rule: SentenceRule,
  check: SafetyCheck | null,
  coworkerName: string | null,
): string {
  const scope = coworkerName ? `${coworkerName} only.` : 'All coworkers.';
  const what = safWhatPhrase(rule.check_id, rule.config);
  const stageShort = SAF_STAGE_SHORT[rule.stage] ?? rule.stage;
  const stageCap = stageShort.charAt(0).toUpperCase() + stageShort.slice(1);
  const scopeSpan = `<span class="rm-saf-scope-mute">${esc(scope)}</span>`;

  // Inert routed checks: the "running but doing nothing" phrase already
  // carries the meaning — appending a verb would contradict it.
  if (check?.action_model === 'config_routed') {
    const routing = (rule.config?.['routing'] as Record<string, unknown>) ?? {};
    if (Object.keys(routing).length === 0) {
      return `${esc(stageCap)}, ${esc(what)}. ${scopeSpan}`;
    }
  }

  if (isHostList(rule.check_id)) {
    return `${esc(stageCap)}, ${esc(what)} — <b>block the call</b> <span class="rm-saf-scope-mute">(for anything else)</span>. ${scopeSpan}`;
  }

  const action = effectiveAction(rule, check);
  const verb = action ? SAF_ACTION_VERB[action] : null;
  if (!verb) return `${esc(stageCap)}, ${esc(what)}. ${scopeSpan}`;
  return `${esc(stageCap)}, ${esc(what)} — <b>${esc(verb)}</b>. ${scopeSpan}`;
}

export function checkLabel(checkId: string): string {
  return SAFETY_CHECK_CATALOG[checkId]?.label ?? checkId;
}

export function safStageShort(stage: SafetyStage): string {
  return SAF_STAGE_SHORT[stage] ?? stage;
}
