// PolicyCard — one approval policy in evaluation order (spec H.2;
// behavioral reference web/src/components/approval-policies-page.ts
// renderRow). Priority badge · target + condition sentence · the
// ALWAYS-visible enable switch (a disabled card dims the body but the
// switch stays bright — it's the way back) · hover-revealed
// Edit / Duplicate / Delete.

import { Copy, Pencil, Trash2 } from 'lucide-react';
import type { ApprovalPolicy } from '../../../api/client';
import { Switch } from '../../../components/switch';
import { priorityBadgeClass } from '../../../lib/rule-ordering';
import { conditionSentence } from './condition-form';

export function PolicyCard({
  policy,
  toggling,
  flash,
  onToggle,
  onEdit,
  onDuplicate,
  onDelete,
}: {
  policy: ApprovalPolicy;
  /** Mid-PATCH on the enable toggle — disables the switch so a
   *  double-click can't queue two conflicting writes. */
  toggling: boolean;
  /** 1.8s highlight pulse after create/duplicate (spec §5.7). */
  flash: boolean;
  onToggle: () => void;
  onEdit: () => void;
  onDuplicate: () => void;
  onDelete: () => void;
}) {
  return (
    <div
      className={`pol-card${policy.enabled ? '' : ' off'}${flash ? ' flash' : ''}`}
      data-policy-id={policy.id}
    >
      <span className={`pol-pri${priorityBadgeClass(policy.priority)}`}>
        priority {policy.priority}
      </span>
      <span className="body">
        <div className="target">
          {policy.mcp_server_name} ·{' '}
          {policy.tool_name === '*' ? (
            <>
              *<span className="all"> (all tools)</span>
            </>
          ) : (
            policy.tool_name
          )}
        </div>
        <div className="sentence">
          {/* conditionSentence escapes leaf text; the only markup is its
              own <b>/<i> wrapping — safe to inject (same contract as the
              Lit unsafeHTML call). */}
          <span
            dangerouslySetInnerHTML={{
              __html: conditionSentence(policy.condition_expr),
            }}
          />{' '}
          → pause to confirm
        </div>
      </span>
      <Switch
        on={policy.enabled}
        disabled={toggling}
        onToggle={onToggle}
        title={policy.enabled ? 'Click to disable' : 'Click to enable'}
      />
      <span className="cardacts">
        <button
          className="icon-btn"
          title="Edit policy"
          onClick={(e) => {
            e.stopPropagation();
            onEdit();
          }}
        >
          <Pencil />
        </button>
        <button
          className="icon-btn"
          title="Duplicate policy"
          onClick={(e) => {
            e.stopPropagation();
            onDuplicate();
          }}
        >
          <Copy />
        </button>
        <button
          className="icon-btn danger"
          title="Delete policy"
          onClick={(e) => {
            e.stopPropagation();
            onDelete();
          }}
        >
          <Trash2 />
        </button>
      </span>
    </div>
  );
}
