// ActionPanel — editor experience 1 (fixed checks, spec §6.11.1): a
// plain-language default line + the five-action segmented control in
// stable ladder order. The natural action is dot-marked; unsupported /
// non-overridable actions are disabled + line-through with a reason
// tooltip. Experiences 2/3 remove the field entirely — the dialog never
// mounts this for them.

import type { SafetyCheck, SafetyStage, SafetyVerdictAction } from '../../../api/client';
import {
  SAF_ACTION_LABEL,
  SAF_ACTION_ORDER,
  SAF_ACTION_SUB,
  actionButtonState,
  naturalAction,
} from '../../../lib/safety-catalog';

export function ActionPanel({
  check,
  stage,
  pickedAction,
  busy,
  onPick,
}: {
  check: SafetyCheck | null;
  stage: SafetyStage;
  /** Override; null ⇒ the natural action is in effect. */
  pickedAction: SafetyVerdictAction | null;
  busy: boolean;
  /** null when the user picked the natural action (no override written). */
  onPick: (action: SafetyVerdictAction | null) => void;
}) {
  const natural = naturalAction(check, stage);
  const current = pickedAction ?? natural;
  const naturalLabel = natural ? SAF_ACTION_LABEL[natural].toLowerCase() : 'act on';
  return (
    <div className="field" data-testid="saf-action-field">
      <label>When it triggers, do this</label>
      <div className="hint" style={{ marginBottom: 6 }}>
        By default, this check <b>{naturalLabel}s</b> anything it finds. You can
        change the action below.
      </div>
      <div className="actseg" role="group">
        {SAF_ACTION_ORDER.map((a) => {
          const st = actionButtonState(check, stage, a, natural);
          const on = a === current && st.enabled;
          return (
            <button
              key={a}
              type="button"
              className={on ? 'on' : ''}
              disabled={!st.enabled || busy}
              title={st.enabled ? undefined : st.reason}
              onClick={() => onPick(a === natural ? null : a)}
            >
              {SAF_ACTION_LABEL[a]}
              {a === natural ? <span className="dot" title="Default for this check" /> : null}
              <span className="sub">{SAF_ACTION_SUB[a]}</span>
            </button>
          );
        })}
      </div>
      <div className="hint" style={{ marginTop: 6 }}>
        <span
          className="dot"
          style={{
            display: 'inline-block',
            width: 6,
            height: 6,
            borderRadius: '50%',
            background: 'var(--rm-text-muted)',
            marginRight: 5,
          }}
        />
        = the default. Pick something else to override.
      </div>
    </div>
  );
}
