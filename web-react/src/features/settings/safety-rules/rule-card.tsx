// RuleCard — one safety rule in evaluation order (spec I.2; behavioral
// reference web/src/components/safety-rules-page.ts renderCard, visual
// layout per the v10 prototype). Priority badge (platform tier gets the
// accent badge) · check LABEL (never the id) + slow/scope chips inline ·
// safSentence line · action pill — omitted entirely for config-routed
// and host-list rules (their sentence carries the semantics) · toggle
// (platform: fixed-on no-op) · hover acts (platform: Audit only).

import { Copy, History, Pencil, Trash2 } from 'lucide-react';
import type { SafetyCheck, SafetyRule } from '../../../api/client';
import { Switch } from '../../../components/switch';
import { priorityBadgeClass } from '../../../lib/rule-ordering';
import {
  SAF_ACTION_LABEL,
  SAFETY_CHECK_CATALOG,
  checkLabel,
  effectiveAction,
  safSentence,
} from '../../../lib/safety-catalog';

export function RuleCard({
  rule,
  check,
  coworkerName,
  toggling,
  flash,
  onToggle,
  onEdit,
  onDuplicate,
  onAudit,
  onDelete,
}: {
  rule: SafetyRule;
  /** Wire check behaviour (null when the catalog is still loading or the
   *  check id is unknown — sentence helpers degrade gracefully). */
  check: SafetyCheck | null;
  /** Resolved coworker name when scoped; null for tenant-wide rules. */
  coworkerName: string | null;
  toggling: boolean;
  flash: boolean;
  onToggle: () => void;
  onEdit: () => void;
  onDuplicate: () => void;
  onAudit: () => void;
  onDelete: () => void;
}) {
  const platform = rule.source === 'platform';
  const config = (rule.config ?? {}) as Record<string, unknown>;
  const slow = check?.cost_class === 'slow';
  // Pill suppressed when the sentence already carries the routing /
  // allowlist semantics (spec I.2).
  const routedNoAction =
    check?.action_model === 'config_routed' ||
    SAFETY_CHECK_CATALOG[rule.check_id]?.cfgKind === 'host-list';
  const action = routedNoAction
    ? null
    : effectiveAction({ check_id: rule.check_id, stage: rule.stage, config }, check);

  return (
    <div
      className={`pol-card${rule.enabled ? '' : ' off'}${flash ? ' flash' : ''}`}
      data-rule-id={rule.id}
    >
      <span
        className={`pol-pri${platform ? ' pol-pri--plat' : priorityBadgeClass(rule.priority)}`}
      >
        priority {rule.priority}
      </span>
      <span className="body">
        <div className="target">
          {checkLabel(rule.check_id)}{' '}
          {slow ? (
            <span className="saf-chip saf-chip--slow" title="This check adds latency">
              slow
            </span>
          ) : null}{' '}
          {coworkerName ? <span className="saf-chip">{coworkerName}</span> : null}
        </div>
        <div className="sentence">
          {/* safSentence escapes dynamic text; its own <b>/<span> markup
              is safe to inject (same contract as the Lit unsafeHTML). */}
          <span
            dangerouslySetInnerHTML={{
              __html: safSentence(
                { check_id: rule.check_id, stage: rule.stage, config },
                check,
                coworkerName,
              ),
            }}
          />
        </div>
      </span>
      {action ? (
        <span className={`saf-act saf-act--${action}`}>
          {SAF_ACTION_LABEL[action] ?? action}
        </span>
      ) : null}
      {platform ? (
        <button
          type="button"
          className="switch on"
          style={{ cursor: 'default' }}
          title="Platform-tier rules are always enabled"
          aria-disabled="true"
        >
          <span className="track" />
          Enabled
        </button>
      ) : (
        <Switch
          on={rule.enabled}
          disabled={toggling}
          onToggle={onToggle}
          title={rule.enabled ? 'Click to disable' : 'Click to enable'}
        />
      )}
      {platform ? (
        // Platform rules: audit only (no edit / duplicate / delete) — §6.2.2;
        // always visible (there is nothing else to reveal on hover).
        <span className="cardacts" style={{ opacity: 1 }}>
          <button
            className="icon-btn"
            title="Change history"
            onClick={(e) => {
              e.stopPropagation();
              onAudit();
            }}
          >
            <History />
          </button>
        </span>
      ) : (
        <span className="cardacts">
          <button
            className="icon-btn"
            title="Edit rule"
            onClick={(e) => {
              e.stopPropagation();
              onEdit();
            }}
          >
            <Pencil />
          </button>
          <button
            className="icon-btn"
            title="Duplicate — useful for moving scope or branching config"
            onClick={(e) => {
              e.stopPropagation();
              onDuplicate();
            }}
          >
            <Copy />
          </button>
          <button
            className="icon-btn"
            title="Change history"
            onClick={(e) => {
              e.stopPropagation();
              onAudit();
            }}
          >
            <History />
          </button>
          <button
            className="icon-btn danger"
            title="Delete rule"
            onClick={(e) => {
              e.stopPropagation();
              onDelete();
            }}
          >
            <Trash2 />
          </button>
        </span>
      )}
    </div>
  );
}
