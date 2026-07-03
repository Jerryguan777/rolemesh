// PolicyDialog — create / edit / duplicate an HITL approval policy
// (spec H.3; behavioral reference web/src/components/approval-policy-dialog.ts).
// One dialog backs all three flows:
//   - editing non-null     → edit  (PATCH; "Edit approval policy")
//   - duplicating non-null → create, pre-filled (POST; "Duplicate …")
//   - both null            → create, defaults (POST; "New approval policy")
//
// Every field is top-level — no disclosure. Server/tool are COMBOBOXES
// (input + datalist): an unconfigured name may be typed, policies can
// pre-date server registration. Tool options = `*` + the keys of the
// typed server's tool_reversibility map (operator-declared names — no
// live tool list exists). A live preview regenerates on every change
// through the same conditionSentence renderer as the list cards.
//
// A stored expression too complex for the flat builder opens READ-ONLY
// on the condition (other fields stay editable): edit-PATCH omits
// condition_expr entirely; duplicate-POST carries the source expression
// verbatim — never silently flatten a hand-crafted nested condition.
//
// Duplicate seeds ALL fields from the source, `enabled` included (Lit
// seedForm parity — the spec's "duplicate starts disabled" note does
// not match the shipped behavior).

import { useEffect, useMemo, useState } from 'react';
import { X } from 'lucide-react';
import {
  ApiError,
  getApiClient,
  type ApprovalPolicy,
  type ApprovalPolicyCreate,
  type ApprovalPolicyUpdate,
} from '../../../api/client';
import { useMCPServers } from '../../../api/queries';
import { BrandMark } from '../../../components/brand-mark';
import { Switch } from '../../../components/switch';
import {
  CONDITION_OPS,
  type ConditionMode,
  type LeafRow,
  buildConditionExpr,
  conditionSentence,
  emptyRow,
  exprToForm,
} from './condition-form';

export function PolicyDialog({
  editing,
  duplicating,
  onClose,
  onSaved,
}: {
  editing: ApprovalPolicy | null;
  duplicating: ApprovalPolicy | null;
  onClose: () => void;
  /** Fired with the saved policy + a toast line; the page splices it
   *  into the list (create appends, edit replaces) and pulses the card. */
  onSaved: (policy: ApprovalPolicy, toast: string) => void;
}) {
  const isEdit = editing !== null;
  // The policy the form seeds from: the edit target, else the duplicate
  // source, else nothing (defaults). Also the untouched-expression
  // source for the read-only condition path.
  const seedSource = editing ?? duplicating;

  const [form, setForm] = useState(() => {
    if (seedSource) {
      const cond = exprToForm(seedSource.condition_expr);
      return {
        server: seedSource.mcp_server_name,
        tool: seedSource.tool_name,
        priority: seedSource.priority,
        enabled: seedSource.enabled,
        mode: cond.mode,
        connective: cond.connective,
        rows: cond.rows.length ? cond.rows : [emptyRow()],
        editable: cond.editable,
      };
    }
    return {
      server: '',
      tool: '*',
      priority: 0,
      enabled: true,
      mode: 'always' as ConditionMode,
      connective: 'and' as const,
      rows: [emptyRow()],
      editable: true,
    };
  });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Suggestion lists — the SAME ['mcp-servers'] query Part D owns;
  // best-effort (catch-to-empty), the fields stay free-text regardless.
  const serversQ = useMCPServers(true);
  const servers = serversQ.data ?? [];
  const toolOptions = useMemo(() => {
    const match = servers.find((s) => s.name === form.server);
    const declared = match ? Object.keys(match.tool_reversibility ?? {}) : [];
    return ['*', ...declared.filter((t) => t && t !== '*')];
  }, [servers, form.server]);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key !== 'Escape') return;
      e.stopPropagation();
      if (!busy) onClose();
    }
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [busy, onClose]);

  /** condition_expr from the current form state (fail-closed §5.14);
   *  a non-editable stored expression passes through verbatim. */
  function currentExpr() {
    if (!form.editable) return seedSource!.condition_expr;
    return buildConditionExpr(form);
  }

  function updateRow(i: number, patch: Partial<LeafRow>) {
    setForm((f) => ({
      ...f,
      rows: f.rows.map((r, idx) => (idx === i ? { ...r, ...patch } : r)),
    }));
  }

  async function submit() {
    if (busy) return;
    if (form.server.trim() === '' || form.tool.trim() === '') {
      setErr('MCP server name and tool name are required.');
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const api = getApiClient();
      let saved: ApprovalPolicy;
      if (isEdit) {
        const body: ApprovalPolicyUpdate = {
          mcp_server_name: form.server.trim(),
          tool_name: form.tool.trim(),
          priority: form.priority,
          enabled: form.enabled,
        };
        // Only resend the condition if it's still editable here —
        // otherwise leave the (complex) stored expression untouched.
        if (form.editable) body.condition_expr = currentExpr();
        saved = await api.updateApprovalPolicy(editing.id, body);
        onSaved(saved, 'Policy updated');
      } else {
        // Create — covers both New and Duplicate (duplicate just seeds
        // the form; the POST is identical, server assigns a new id).
        const body: ApprovalPolicyCreate = {
          mcp_server_name: form.server.trim(),
          tool_name: form.tool.trim(),
          priority: form.priority,
          enabled: form.enabled,
          condition_expr: currentExpr(),
        };
        saved = await api.createApprovalPolicy(body);
        onSaved(saved, 'Policy created');
      }
      onClose();
    } catch (e) {
      setErr(
        e instanceof ApiError ? (e.body?.message ?? `HTTP ${e.status}`) : (e as Error).message,
      );
      setBusy(false);
    }
  }

  const title = isEdit
    ? 'Edit approval policy'
    : duplicating
      ? 'Duplicate approval policy'
      : 'New approval policy';
  const previewTool = form.tool.trim() === '*' ? '* (all tools)' : form.tool.trim() || '—';

  return (
    <div
      className="scrim"
      onClick={(e) => {
        if (e.target === e.currentTarget && !busy) onClose();
      }}
    >
      <div
        className="dlg"
        style={{ width: 640 }}
        role="dialog"
        aria-modal="true"
        aria-label="Approval policy"
      >
        <div className="dlg-header">
          <div className="hleft">
            <div className="dlg-brand-icon">
              <BrandMark size={16} />
            </div>
            <h2 className="dlg-title">{title}</h2>
          </div>
          <button className="icon-btn" aria-label="Close" disabled={busy} onClick={onClose}>
            <X />
          </button>
        </div>
        <div className="wiz-body" style={{ minHeight: 0 }}>
          <div style={{ display: 'flex', gap: 12 }}>
            <div className="field" style={{ flex: 1 }}>
              <label htmlFor="pf-server">MCP server</label>
              <input
                id="pf-server"
                type="text"
                list="pf-server-opts"
                spellCheck={false}
                value={form.server}
                disabled={busy}
                onChange={(e) =>
                  // Server change resets the tool to `*` — the old
                  // server's declared tool names don't carry over.
                  setForm((f) => ({ ...f, server: e.target.value, tool: '*' }))
                }
              />
              <datalist id="pf-server-opts">
                {servers.map((s) => (
                  <option key={s.id} value={s.name} />
                ))}
              </datalist>
            </div>
            <div className="field" style={{ flex: 1 }}>
              <label htmlFor="pf-tool">Tool</label>
              <input
                id="pf-tool"
                type="text"
                list="pf-tool-opts"
                spellCheck={false}
                value={form.tool}
                disabled={busy}
                onChange={(e) => setForm((f) => ({ ...f, tool: e.target.value }))}
              />
              <datalist id="pf-tool-opts">
                {toolOptions.map((t) => (
                  <option key={t} value={t} />
                ))}
              </datalist>
            </div>
          </div>
          <div className="hint" style={{ margin: '-6px 0 12px' }}>
            Pick a configured server/tool, or type one that isn't connected yet.
          </div>

          {/* Lit parity: a non-editable condition replaces the WHOLE
              condition area (mode seg included) with the read-only block —
              the seg would be a lying no-op since the stored expression
              passes through untouched regardless. */}
          {!form.editable ? (
            <div className="field" data-testid="condition-readonly">
              <div className="hint">
                This policy uses an advanced condition that the form can't edit. It
                is kept as-is when you save.
              </div>
              <pre className="cond-readonly">
                {JSON.stringify(seedSource?.condition_expr ?? {}, null, 2)}
              </pre>
            </div>
          ) : (
            <div className="field">
              <label>Ask for approval</label>
              <span className="seg" role="radiogroup" aria-label="When to require approval">
                <button
                  type="button"
                  className={form.mode === 'always' ? 'on' : ''}
                  aria-pressed={form.mode === 'always'}
                  disabled={busy}
                  onClick={() => setForm((f) => ({ ...f, mode: 'always' }))}
                >
                  Every time
                </button>
                <button
                  type="button"
                  className={form.mode === 'match' ? 'on' : ''}
                  aria-pressed={form.mode === 'match'}
                  disabled={busy}
                  onClick={() =>
                    setForm((f) => ({
                      ...f,
                      mode: 'match',
                      rows: f.rows.length ? f.rows : [emptyRow()],
                    }))
                  }
                >
                  Only when…
                </button>
              </span>
            </div>
          )}

          {form.editable && form.mode === 'match' ? (
            <>
                <div className="field">
                  <label>Combine conditions with</label>
                  <span className="seg">
                    <button
                      type="button"
                      className={form.connective === 'and' ? 'on' : ''}
                      disabled={busy}
                      onClick={() => setForm((f) => ({ ...f, connective: 'and' }))}
                    >
                      All (AND)
                    </button>
                    <button
                      type="button"
                      className={form.connective === 'or' ? 'on' : ''}
                      disabled={busy}
                      onClick={() => setForm((f) => ({ ...f, connective: 'or' }))}
                    >
                      Any (OR)
                    </button>
                  </span>
                </div>
                <div className="field">
                  <label>Conditions</label>
                  {form.rows.map((r, i) => (
                    <div className="cond-row" key={i}>
                      <input
                        className="cf"
                        placeholder="field (e.g. amount)"
                        value={r.field}
                        disabled={busy}
                        onChange={(e) => updateRow(i, { field: e.target.value })}
                      />
                      <select
                        aria-label="Operator"
                        value={r.op}
                        disabled={busy}
                        onChange={(e) =>
                          updateRow(i, { op: e.target.value as LeafRow['op'] })
                        }
                      >
                        {CONDITION_OPS.map((op) => (
                          <option key={op} value={op}>
                            {op}
                          </option>
                        ))}
                      </select>
                      <input
                        className="cv"
                        placeholder="value (e.g. 5000)"
                        value={r.value}
                        disabled={busy}
                        onChange={(e) => updateRow(i, { value: e.target.value })}
                      />
                      <button
                        className="icon-btn danger"
                        title="Remove condition"
                        disabled={busy || form.rows.length === 1}
                        onClick={() =>
                          setForm((f) => ({
                            ...f,
                            rows: f.rows.filter((_, idx) => idx !== i),
                          }))
                        }
                      >
                        <X />
                      </button>
                    </div>
                  ))}
                  <button
                    className="btn-ghost"
                    style={{ paddingLeft: 0 }}
                    disabled={busy}
                    onClick={() =>
                      setForm((f) => ({ ...f, rows: [...f.rows, emptyRow()] }))
                    }
                  >
                    + Add condition
                  </button>
                </div>
              </>
          ) : null}

          <div
            style={{ display: 'flex', gap: 20, alignItems: 'flex-end', marginBottom: 14 }}
          >
            <div className="field" style={{ marginBottom: 0 }}>
              <label htmlFor="pf-priority" style={{ whiteSpace: 'nowrap' }}>
                Priority{' '}
                <span style={{ fontWeight: 400, color: 'var(--rm-text-muted)' }}>
                  higher wins on ties
                </span>
              </label>
              <input
                id="pf-priority"
                type="text"
                inputMode="numeric"
                style={{ width: 96, display: 'block' }}
                value={String(form.priority)}
                disabled={busy}
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    priority: parseInt(e.target.value, 10) || 0,
                  }))
                }
              />
            </div>
            <div className="field" style={{ marginBottom: 0 }}>
              <label>Status</label>
              <Switch
                on={form.enabled}
                disabled={busy}
                onToggle={() => setForm((f) => ({ ...f, enabled: !f.enabled }))}
              />
            </div>
          </div>

          <div className="preview" data-testid="policy-preview">
            <div className="pv-label">This policy</div>
            <span>
              <b>
                {form.server.trim() || '—'} · {previewTool}
              </b>{' '}
              —{' '}
              <span
                dangerouslySetInnerHTML={{ __html: conditionSentence(currentExpr()) }}
              />{' '}
              → pause to confirm
            </span>
          </div>
        </div>
        <div className="wiz-foot">
          {err ? (
            <span className="wiz-err" role="alert">
              {err}
            </span>
          ) : (
            <span />
          )}
          <span style={{ display: 'inline-flex', gap: 8 }}>
            <button className="btn-ghost" disabled={busy} onClick={onClose}>
              Cancel
            </button>
            <button className="btn-primary" disabled={busy} onClick={() => void submit()}>
              {busy
                ? isEdit
                  ? 'Saving…'
                  : 'Creating…'
                : isEdit
                  ? 'Save changes'
                  : 'Create policy'}
            </button>
          </span>
        </div>
      </div>
    </div>
  );
}
