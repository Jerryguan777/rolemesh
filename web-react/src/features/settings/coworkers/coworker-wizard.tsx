// CoworkerWizard — create AND edit in one component (spec §C.4;
// behavioral reference web/src/components/coworker-wizard.ts).
//
// Six steps (pinned order): Identity · Engine · Model · Tools ·
// Skills · Review. Stepper reuses the tab-bar idiom — completed steps
// are blue + ✓ and clickable, current gets the orange underline.
//
// Model step is REQUIRED — Lit parity per D-C4 (spec v5): a model must
// be picked, its provider must hold a credential, and it must survive
// the backend compatibility filter. No "Backend default" card (the
// v4 draft's citation for one was wrong); with zero credentialed
// providers the wizard cannot complete — the credential link-out
// (D-C1) is the intended path. Editing a legacy null-model coworker
// forces the user to converge on a model before advancing.
//
// Edit-mode locks (contract-enforced — CoworkerUpdate has neither
// `folder` nor `agent_backend`): slug read-only with hint; engine
// lock-note with the other cards dimmed + disabled.
//
// Submit ordering (ported from Lit): coworker POST/PATCH first, then
// the MCP/skill binding diffs sequentially; partial failures DON'T
// roll back — a banner pinned above the stepper lists what failed and
// the wizard stays open. Full-success create navigates into chat with
// the new coworker (`/?agent_id={id}` — creation's purpose is to talk
// to it); full-success edit closes + toasts.

import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { X } from 'lucide-react';
import {
  ApiError,
  getApiClient,
  type BackendName,
  type Coworker,
  type Model,
  type ModelProvider,
} from '../../../api/client';
import {
  useBackends,
  useCredentials,
  useMCPServers,
  useModels,
  useSkills,
} from '../../../api/queries';
import { BrandMark } from '../../../components/brand-mark';
import { CredentialDialog } from '../../../components/credential-dialog';
import {
  groupModelsByProvider,
  type ProviderGroup,
} from '../../../lib/models-grouping';
import { isValidSlug, slugify } from './use-slug';

export const WIZARD_STEPS = [
  'Identity',
  'Engine',
  'Model',
  'Tools',
  'Skills',
  'Review',
] as const;

export interface WizardDraft {
  name: string;
  folder: string;
  slugTouched: boolean;
  instructions: string;
  backend: BackendName | null;
  modelId: string | null;
  mcpServerIds: string[];
  skillIds: string[];
}

export function emptyDraft(): WizardDraft {
  return {
    name: '',
    folder: '',
    slugTouched: false,
    instructions: '',
    backend: null,
    modelId: null,
    mcpServerIds: [],
    skillIds: [],
  };
}

/** Per-step advance gate (spec C.4 table, D-C4 Lit parity). Exported
 *  for tests. `modelGroups` is the backend-filtered, credential-
 *  annotated projection (models-grouping.ts) the Model step renders —
 *  the gate and the visible set stay one source. */
export function isStepValid(
  step: number,
  d: WizardDraft,
  modelGroups: readonly ProviderGroup[] = [],
): boolean {
  switch (step) {
    case 0:
      return d.name.trim().length > 0 && isValidSlug(d.folder);
    case 1:
      return d.backend !== null;
    case 2:
      // Model REQUIRED: picked + provider credentialed + model ACTIVE +
      // in the backend-filtered visible set. The `is_active` clause is
      // the F.4 usable predicate (`hasCredential && is_active`), matching
      // renderModel's lock and the Lit `disabled = inactive ||
      // !hasCredential`; without it, editing a coworker whose model was
      // later deactivated would let a stale inactive pick advance.
      if (!d.modelId) return false;
      return modelGroups.some(
        (g) =>
          g.hasCredential &&
          g.models.some((m) => m.id === d.modelId && m.is_active),
      );
    default:
      return true; // Tools/Skills optional · Review free
  }
}

interface SubmitFailure {
  message: string;
  mcpFailures: string[];
  skillFailures: string[];
}

function draftFromCoworker(c: Coworker): WizardDraft {
  return {
    name: c.name,
    folder: c.folder,
    slugTouched: true,
    instructions: c.system_prompt ?? '',
    backend: c.agent_backend,
    modelId: c.model_id ?? null,
    mcpServerIds: [],
    skillIds: [],
  };
}

export function CoworkerWizard({
  editing,
  onClose,
  onSaved,
}: {
  /** null → create flow; a row → edit flow with locks + binding diff. */
  editing: Coworker | null;
  onClose: () => void;
  /** Fired whenever server state changed (edit success, partial
   *  commit) so the page can refresh its list; carries a toast line
   *  on full-success edit. */
  onSaved: (toast: string | null) => void;
}) {
  const isEdit = editing !== null;
  const [step, setStep] = useState(0);
  const [draft, setDraft] = useState<WizardDraft>(() =>
    editing ? draftFromCoworker(editing) : emptyDraft(),
  );
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<SubmitFailure | null>(null);
  // D-C1 (credential half) resolved in v8: step 3's `+ Add credential`
  // opens the credential dialog IN PLACE (pre-filled with the locked
  // group's provider) instead of linking out. On save the shared
  // ['credentials'] query invalidates → modelGroups recomputes → the
  // locked card unlocks live. `undefined` = closed. The MCP half
  // (step 4) still links out (no equivalent inline spawn wired).
  const [credDialogProvider, setCredDialogProvider] = useState<
    ModelProvider | undefined
  >(undefined);

  // Catalogues (credentials/MCP/skills degrade to [] — see queries.ts).
  const backendsQ = useBackends(true);
  const modelsQ = useModels();
  const credentialsQ = useCredentials(true);
  const mcpServersQ = useMCPServers(true);
  const skillsQ = useSkills(true);

  // Edit mode: seed bindings from the coworker's current state
  // (failures degrade to empty — the wizard stays usable for
  // identity/model edits). The originals feed the submit diff.
  const [originals, setOriginals] = useState<{ mcp: string[]; skills: string[] }>(
    { mcp: [], skills: [] },
  );
  useEffect(() => {
    if (!editing) return;
    let cancelled = false;
    void (async () => {
      const [mcpResult, skillResult] = await Promise.allSettled([
        getApiClient().listCoworkerMCPServers(editing.id),
        getApiClient().listCoworkerSkills(editing.id),
      ]);
      if (cancelled) return;
      const mcpIds =
        mcpResult.status === 'fulfilled'
          ? mcpResult.value.map((b) => b.mcp_server_id)
          : [];
      const skillIds =
        skillResult.status === 'fulfilled'
          ? skillResult.value
              .filter((b) => b.enabled !== false)
              .map((b) => b.skill_id)
          : [];
      setOriginals({ mcp: mcpIds, skills: skillIds });
      setDraft((d) => ({ ...d, mcpServerIds: mcpIds, skillIds }));
    })();
    return () => {
      cancelled = true;
    };
  }, [editing]);

  // ESC closes (unless mid-submit). Capture so the page-level ESC
  // handlers don't race.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key !== 'Escape') return;
      // Defer to the stacked credential dialog when it's open — let its
      // own (later-registered) capture handler close it first.
      if (credDialogProvider !== undefined) return;
      e.stopPropagation();
      if (!busy) onClose();
    }
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [busy, onClose, credDialogProvider]);

  const selectedBackend = useMemo(
    () => backendsQ.data?.find((b) => b.name === draft.backend) ?? null,
    [backendsQ.data, draft.backend],
  );
  const modelGroups = useMemo(
    () =>
      groupModelsByProvider(
        modelsQ.data ?? [],
        credentialsQ.data ?? [],
        selectedBackend,
      ),
    [modelsQ.data, credentialsQ.data, selectedBackend],
  );
  const selectedModel: Model | null =
    (draft.modelId && modelsQ.data?.find((m) => m.id === draft.modelId)) || null;

  const canAdvance = isStepValid(step, draft, modelGroups);

  async function handleSubmit(): Promise<void> {
    if (busy) return;
    setBusy(true);
    setFailure(null);
    const api = getApiClient();

    let coworker: Coworker;
    try {
      if (editing) {
        coworker = await api.updateCoworker(editing.id, {
          name: draft.name.trim(),
          // Omit (never null) when unset — the handler rejects
          // `model_id: null` as a clear (see client.ts note).
          model_id: draft.modelId ?? undefined,
          system_prompt: draft.instructions.trim() || null,
        });
      } else {
        coworker = await api.createCoworker({
          name: draft.name.trim(),
          folder: draft.folder,
          agent_backend: draft.backend!,
          model_id: draft.modelId,
          system_prompt: draft.instructions.trim() || null,
          // Not surfaced in the wizard (D-C2) — Lit-locked defaults.
          max_concurrent_containers: 2,
          is_frontdesk: false,
        });
      }
    } catch (err) {
      setFailure({
        message:
          err instanceof ApiError
            ? `${err.status} — ${err.message}`
            : (err as Error).message,
        mcpFailures: [],
        skillFailures: [],
      });
      setBusy(false);
      return;
    }

    // Binding plan: create = all selections; edit = deltas only.
    // Partial failures don't roll back (Lit commit ordering).
    const oldMcp = new Set(isEdit ? originals.mcp : []);
    const newMcp = new Set(draft.mcpServerIds);
    const mcpToAdd = draft.mcpServerIds.filter((id) => !oldMcp.has(id));
    const mcpToRemove = isEdit
      ? originals.mcp.filter((id) => !newMcp.has(id))
      : [];
    const oldSkill = new Set(isEdit ? originals.skills : []);
    const newSkill = new Set(draft.skillIds);
    const skillToAdd = draft.skillIds.filter((id) => !oldSkill.has(id));
    const skillToRemove = isEdit
      ? originals.skills.filter((id) => !newSkill.has(id))
      : [];

    const mcpFailures: string[] = [];
    for (const id of mcpToAdd) {
      try {
        // null = all tools enabled (Lit locked decision #9).
        await api.bindCoworkerMCPServer(coworker.id, {
          mcp_server_id: id,
          enabled_tools: null,
        });
      } catch {
        mcpFailures.push(id);
      }
    }
    for (const id of mcpToRemove) {
      try {
        await api.unbindCoworkerMCPServer(coworker.id, id);
      } catch {
        mcpFailures.push(id);
      }
    }
    const skillFailures: string[] = [];
    for (const id of skillToAdd) {
      try {
        await api.enableCoworkerSkill(coworker.id, id);
      } catch {
        skillFailures.push(id);
      }
    }
    for (const id of skillToRemove) {
      try {
        await api.disableCoworkerSkill(coworker.id, id);
      } catch {
        skillFailures.push(id);
      }
    }

    setBusy(false);

    if (mcpFailures.length || skillFailures.length) {
      // Partial success — keep the wizard open so the user sees what
      // happened; the list still refreshes behind it.
      const total = mcpFailures.length + skillFailures.length;
      setFailure({
        message: `Coworker ${isEdit ? 'updated' : 'created'}, but ${total} binding${total > 1 ? 's' : ''} failed. You can finish wiring it up by editing it again.`,
        mcpFailures,
        skillFailures,
      });
      onSaved(null);
      return;
    }

    if (isEdit) {
      onSaved(`Saved changes to ${coworker.name}`);
      onClose();
      return;
    }
    // Create success — go talk to it. Full-reload navigation keeps the
    // ?agent_id contract identical to the Lit wizard.
    location.href = `${location.pathname}?agent_id=${encodeURIComponent(coworker.id)}#/`;
  }

  function renderIdentity() {
    const slugInvalid = draft.name.trim() !== '' && !isValidSlug(draft.folder);
    return (
      <>
        <div className="field">
          <label htmlFor="wiz-name">Name</label>
          <input
            id="wiz-name"
            type="text"
            maxLength={200}
            placeholder="e.g. Portfolio Manager"
            value={draft.name}
            onChange={(e) => {
              const name = e.target.value;
              setDraft((d) => ({
                ...d,
                name,
                folder: !isEdit && !d.slugTouched ? slugify(name) : d.folder,
              }));
            }}
          />
        </div>
        <div className="field">
          <label htmlFor="wiz-slug">Slug (workspace folder)</label>
          <input
            id="wiz-slug"
            type="text"
            maxLength={64}
            disabled={isEdit}
            value={draft.folder}
            onChange={(e) =>
              setDraft((d) => ({ ...d, folder: e.target.value, slugTouched: true }))
            }
          />
          <div className={`hint${slugInvalid ? ' invalid' : ''}`}>
            {isEdit
              ? 'The slug is fixed for the lifetime of a coworker — it names the container mount path.'
              : slugInvalid
                ? 'Slug must start with a letter or digit and may contain a–z, 0–9, _ or -.'
                : 'Auto-derived from the name; letters, digits, - and _ only.'}
          </div>
        </div>
        <div className="field">
          <label htmlFor="wiz-instructions">Instructions</label>
          <textarea
            id="wiz-instructions"
            placeholder="What this coworker does, its tone, and its boundaries…"
            value={draft.instructions}
            onChange={(e) =>
              setDraft((d) => ({ ...d, instructions: e.target.value }))
            }
          />
          <div className="hint">Becomes the coworker's system prompt.</div>
        </div>
      </>
    );
  }

  function renderEngine() {
    return (
      <>
        {isEdit ? (
          <div className="lock-note">
            Engine is fixed at <code>{draft.backend}</code> for the lifetime of a
            coworker. Delete + recreate if you really need a different one.
          </div>
        ) : null}
        {(backendsQ.data ?? []).map((b) => {
          const selected = draft.backend === b.name;
          const locked = isEdit && !selected;
          return (
            <button
              key={b.name}
              className={`opt-card${selected ? ' selected' : ''}${locked ? ' locked' : ''}`}
              disabled={isEdit}
              onClick={() => {
                if (isEdit) return;
                // Reset the model — the previous pick may not fit the
                // new backend's compatibility matrix.
                setDraft((d) => ({ ...d, backend: b.name, modelId: null }));
              }}
            >
              <div className="t" style={{ textTransform: 'capitalize' }}>
                {b.name}
              </div>
              <div className="d">{b.description}</div>
              <div className="d">
                Providers: {b.supported_providers.join(', ')} · Families:{' '}
                {b.supported_model_families
                  ? b.supported_model_families.join(', ')
                  : 'any'}
              </div>
            </button>
          );
        })}
      </>
    );
  }

  function renderModel() {
    return (
      <>
        {modelGroups.length === 0 ? (
          <div className="hint">No models compatible with this engine.</div>
        ) : null}
        {modelGroups.map((g) => (
          <div key={g.provider}>
            <div className="group-h">{g.provider}</div>
            {g.models.map((m) => {
              const inactive = !m.is_active;
              if (!g.hasCredential || inactive) {
                return (
                  <div key={m.id} className="opt-card locked">
                    <div className="cred-missing">
                      <div className="t">{m.display_name}</div>
                      {inactive ? (
                        <span className="warn" style={{ cursor: 'default' }}>
                          Inactive in the catalogue
                        </span>
                      ) : (
                        // D-C1 (v8): open the credential dialog in place,
                        // pre-filled with this group's provider (mirrors
                        // the Lit wizard's request-credential event).
                        <button
                          className="warn"
                          onClick={() =>
                            setCredDialogProvider(g.provider as ModelProvider)
                          }
                        >
                          + Add credential
                        </button>
                      )}
                    </div>
                  </div>
                );
              }
              return (
                <button
                  key={m.id}
                  className={`opt-card${draft.modelId === m.id ? ' selected' : ''}`}
                  onClick={() => setDraft((d) => ({ ...d, modelId: m.id }))}
                >
                  <div className="t">{m.display_name}</div>
                </button>
              );
            })}
          </div>
        ))}
      </>
    );
  }

  function renderCheckList(kind: 'tools' | 'skills') {
    const selectedIds = kind === 'tools' ? draft.mcpServerIds : draft.skillIds;
    const rows =
      kind === 'tools'
        ? (mcpServersQ.data ?? []).map((s) => ({
            id: s.id,
            title: s.name,
            detail: s.url,
          }))
        : (skillsQ.data ?? []).map((s) => ({
            id: s.id,
            title: s.name,
            detail: s.description,
          }));
    function toggle(id: string) {
      setDraft((d) => {
        const key = kind === 'tools' ? 'mcpServerIds' : 'skillIds';
        const cur = d[key];
        const next = cur.includes(id)
          ? cur.filter((x) => x !== id)
          : [...cur, id];
        return { ...d, [key]: next };
      });
    }
    return (
      <>
        <div className="hint" style={{ marginBottom: 10 }}>
          {kind === 'tools'
            ? 'MCP servers this coworker may call. Every call still passes the credential proxy, egress gateway, and safety pipeline.'
            : 'Skills the coworker loads at run time.'}
        </div>
        {rows.length === 0 ? (
          <div className="hint">
            {kind === 'tools' ? 'No MCP servers configured yet.' : 'No skills yet.'}
          </div>
        ) : (
          rows.map((r) => (
            <button
              key={r.id}
              className={`check-row${selectedIds.includes(r.id) ? ' selected' : ''}`}
              onClick={() => toggle(r.id)}
            >
              <span className="cb" />
              <span>
                <div className="rt">{r.title}</div>
                <div className="rd">{r.detail}</div>
              </span>
            </button>
          ))
        )}
      </>
    );
  }

  function renderReview() {
    const mcpNames = draft.mcpServerIds.map(
      (id) => mcpServersQ.data?.find((s) => s.id === id)?.name ?? id,
    );
    const skillNames = draft.skillIds.map(
      (id) => skillsQ.data?.find((s) => s.id === id)?.name ?? id,
    );
    const rows: [string, ReactNode][] = [
      ['Name', draft.name],
      ['Slug', <code key="s">{draft.folder}</code>],
      ['Engine', draft.backend ?? '—'],
      ['Model', selectedModel?.display_name ?? '—'],
      ['MCP servers', mcpNames.length ? mcpNames.join(', ') : '—'],
      ['Skills', skillNames.length ? skillNames.join(', ') : '—'],
      ['Instructions', draft.instructions || '—'],
    ];
    return (
      <>
        {rows.map(([k, v]) => (
          <div key={k as string} className="review-row">
            <span className="k">{k}</span>
            <span className="v">{v}</span>
          </div>
        ))}
      </>
    );
  }

  const bodyByStep = [
    renderIdentity,
    renderEngine,
    renderModel,
    () => renderCheckList('tools'),
    () => renderCheckList('skills'),
    renderReview,
  ];
  const isLast = step === WIZARD_STEPS.length - 1;
  const cataloguesLoading = backendsQ.isLoading || modelsQ.isLoading;

  return (
    <div
      className="scrim"
      onClick={(e) => {
        if (e.target === e.currentTarget && !busy) onClose();
      }}
    >
      <div className="dlg" role="dialog" aria-modal="true" aria-label="Coworker wizard">
        <div className="dlg-header">
          <div className="hleft">
            <div className="dlg-brand-icon">
              <BrandMark size={16} />
            </div>
            <h2 className="dlg-title">
              {isEdit ? `Edit ${editing.name}` : 'New coworker'}
            </h2>
          </div>
          <button className="icon-btn" aria-label="Close" disabled={busy} onClick={onClose}>
            <X />
          </button>
        </div>
        {failure ? (
          <div className="wiz-banner" role="alert">
            {failure.message}
            {failure.mcpFailures.length ? (
              <div className="detail">
                Tool bindings failed: {failure.mcpFailures.join(', ')}
              </div>
            ) : null}
            {failure.skillFailures.length ? (
              <div className="detail">
                Skills failed: {failure.skillFailures.join(', ')}
              </div>
            ) : null}
          </div>
        ) : null}
        <div className="stepper">
          {WIZARD_STEPS.map((label, i) => {
            const cls = i === step ? 'current' : i < step ? 'done' : '';
            return (
              <button
                key={label}
                className={`step ${cls}`}
                disabled={i >= step}
                onClick={() => {
                  if (i < step) setStep(i);
                }}
              >
                <span className="tick">✓</span>
                {label}
              </button>
            );
          })}
        </div>
        <div className="wiz-body wizard">
          {cataloguesLoading ? (
            <div className="hint">Loading…</div>
          ) : backendsQ.isError || modelsQ.isError ? (
            <div className="wiz-err">Failed to load catalogues — close and retry.</div>
          ) : (
            bodyByStep[step]()
          )}
        </div>
        <div className="wiz-foot">
          {step > 0 ? (
            <button className="btn-ghost" disabled={busy} onClick={() => setStep(step - 1)}>
              Back
            </button>
          ) : (
            <span />
          )}
          <span className="actions">
            <button
              className="btn-primary"
              disabled={!canAdvance || busy || cataloguesLoading}
              onClick={() => {
                if (isLast) void handleSubmit();
                else setStep(step + 1);
              }}
            >
              {isLast
                ? busy
                  ? isEdit
                    ? 'Saving…'
                    : 'Creating…'
                  : isEdit
                    ? 'Save changes'
                    : 'Create coworker'
                : 'Next'}
            </button>
          </span>
        </div>
      </div>

      {credDialogProvider !== undefined ? (
        <CredentialDialog
          provider={credDialogProvider}
          onClose={() => setCredDialogProvider(undefined)}
        />
      ) : null}
    </div>
  );
}
