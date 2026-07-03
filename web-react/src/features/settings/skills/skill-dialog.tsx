// SkillDialog — create AND edit in one component (spec E.2; behavioral
// reference web/src/components/skill-dialog.ts). 640px. The mental
// model (kept from Lit): a skill is a short name + a description + a
// body of instructions. YAML frontmatter is never shown — assembled
// into SKILL.md on save, stripped on load (lib/skill-manifest).
//
// Edit mode: name is immutable (backend read-only on PATCH — omitted
// from the body; input rendered disabled). On open the dialog fetches
// the full Skill (files included) and seeds Instructions + the file
// tree. Save = create POST full files map / edit PATCH full replacement
// (Lit chose one-shot replace over client-side diffing).

import { useEffect, useRef, useState } from 'react';
import { Trash2, X } from 'lucide-react';
import {
  ApiError,
  getApiClient,
  type SkillSummary,
} from '../../../api/client';
import { BrandMark } from '../../../components/brand-mark';
import { parseSkillMd, serializeSkillMd } from '../../../lib/skill-manifest';
import { SKILL_MANIFEST_NAME } from '../../../lib/skill-constants';
import {
  composeTally,
  contentBytes,
  DESCRIPTION_MAX,
  formatBytes,
  groupByFolder,
  ingestUploads,
  MANIFEST_UPLOAD_REJECTED,
  MAX_UPLOAD_BYTES_PER_FILE,
  MAX_UPLOAD_BYTES_TOTAL,
  validateSkillName,
  type ExtraFile,
} from '../../../lib/skill-upload';
import { readDroppedItems, readFilesFromInput } from './read-files';

export function SkillDialog({
  editing,
  onClose,
  onSaved,
}: {
  editing: SkillSummary | null;
  onClose: () => void;
  onSaved: (toast: string) => void;
}) {
  const isEdit = editing !== null;
  const [name, setName] = useState(editing?.name ?? '');
  const [nameTouched, setNameTouched] = useState(false);
  const [description, setDescription] = useState('');
  const [body, setBody] = useState('');
  const [extraFiles, setExtraFiles] = useState<ExtraFile[]>([]);
  const [loadingDetail, setLoadingDetail] = useState(isEdit);
  const [filesOpen, setFilesOpen] = useState(false);
  const [dragHover, setDragHover] = useState(false);
  const [reading, setReading] = useState(false);
  const [uploadToast, setUploadToast] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const uploadToastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const dragDepth = useRef(0);

  function flashUploadToast(msg: string) {
    if (uploadToastTimer.current) clearTimeout(uploadToastTimer.current);
    setUploadToast(msg);
    uploadToastTimer.current = setTimeout(() => setUploadToast(null), 4000);
  }

  // Edit mode: fetch the full Skill (files) and seed body/description
  // from SKILL.md + the extra files from the rest of the map.
  useEffect(() => {
    if (!editing) return;
    let cancelled = false;
    void getApiClient()
      .getSkill(editing.id)
      .then((skill) => {
        if (cancelled) return;
        const manifest = skill.files?.[SKILL_MANIFEST_NAME]?.content ?? '';
        const parsed = parseSkillMd(manifest);
        setDescription(parsed.description || editing.description || '');
        setBody(parsed.body);
        const extras: ExtraFile[] = Object.entries(skill.files ?? {})
          .filter(([p]) => p !== SKILL_MANIFEST_NAME)
          .map(([path, f]) => ({ path, content: f.content }));
        setExtraFiles(extras);
        if (extras.length > 0) setFilesOpen(true);
        setLoadingDetail(false);
      })
      .catch(() => {
        if (!cancelled) {
          // Degrade to the summary's description; body stays empty.
          setDescription(editing.description || '');
          setLoadingDetail(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [editing]);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key !== 'Escape') return;
      e.stopPropagation();
      if (!busy) onClose();
    }
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [busy, onClose]);

  const trimmedName = name.trim();
  const liveNameErr = nameTouched && trimmedName !== '' ? validateSkillName(trimmedName) : null;
  const descLen = description.length;
  const descNearCap = DESCRIPTION_MAX - descLen <= 200;

  // Save gate (Lit isValid): valid non-reserved name + non-empty
  // description within the cap. Name gate applies to create only
  // (immutable + prefilled on edit).
  const nameOk = trimmedName !== '' && validateSkillName(trimmedName) === null;
  const canSave =
    (isEdit || nameOk) &&
    description.trim() !== '' &&
    descLen <= DESCRIPTION_MAX &&
    !busy &&
    !reading &&
    !loadingDetail;

  function applyIngest(incoming: Awaited<ReturnType<typeof readFilesFromInput>>) {
    const result = ingestUploads(extraFiles, incoming);
    setExtraFiles(result.files);
    if (result.disclose) setFilesOpen(true);
    if (result.manifestRejected) {
      flashUploadToast(MANIFEST_UPLOAD_REJECTED);
    } else {
      const t = composeTally(result.tally);
      if (t) flashUploadToast(t);
    }
  }

  async function onPickInput(e: React.ChangeEvent<HTMLInputElement>) {
    const list = e.target.files;
    if (!list || list.length === 0) return;
    setReading(true);
    try {
      applyIngest(await readFilesFromInput(list));
    } finally {
      setReading(false);
      e.target.value = ''; // allow re-picking the same path
    }
  }

  async function onDrop(e: React.DragEvent) {
    e.preventDefault();
    dragDepth.current = 0;
    setDragHover(false);
    if (!e.dataTransfer) return;
    setReading(true);
    try {
      applyIngest(await readDroppedItems(e.dataTransfer.items));
    } finally {
      setReading(false);
    }
  }

  function removeFile(index: number) {
    setExtraFiles((files) => files.filter((_, i) => i !== index));
  }

  async function save() {
    if (!canSave) return;
    setBusy(true);
    setErr(null);
    const manifest = serializeSkillMd(trimmedName, description.trim(), body);
    const files: Record<string, string> = { [SKILL_MANIFEST_NAME]: manifest };
    for (const f of extraFiles) files[f.path] = f.content;
    try {
      const api = getApiClient();
      const saved = isEdit
        ? // name is read-only on PATCH — omit it; send the full file set
          // for atomic replacement.
          await api.updateSkill(editing.id, { enabled: true, files })
        : await api.createSkill({ name: trimmedName, enabled: true, files });
      onSaved(isEdit ? `Saved changes to ${saved.name}` : `Created ${saved.name}`);
      onClose();
    } catch (e) {
      setErr(
        e instanceof ApiError ? (e.body?.message ?? `${e.status}`) : (e as Error).message,
      );
      setBusy(false);
    }
  }

  const controlsDisabled = reading || busy;

  return (
    <div
      className="scrim"
      onClick={(e) => {
        if (e.target === e.currentTarget && !busy) onClose();
      }}
    >
      <div className="dlg" style={{ width: 640 }} role="dialog" aria-modal="true" aria-label="Skill">
        <div className="dlg-header">
          <div className="hleft">
            <div className="dlg-brand-icon">
              <BrandMark size={16} />
            </div>
            <h2 className="dlg-title">{isEdit ? `Edit ${editing.name}` : 'New skill'}</h2>
          </div>
          <button className="icon-btn" aria-label="Close" disabled={busy} onClick={onClose}>
            <X />
          </button>
        </div>
        <div className="wiz-body">
          {loadingDetail ? <div className="page-sub">Loading…</div> : null}
          <div className="field">
            <label htmlFor="sk-name">Name</label>
            <input
              id="sk-name"
              type="text"
              maxLength={64}
              disabled={isEdit || busy}
              placeholder="e.g. pdf-toolkit"
              value={name}
              onChange={(e) => {
                setName(e.target.value);
                setNameTouched(true);
              }}
              onBlur={() => setNameTouched(true)}
            />
            <div className={`hint${liveNameErr ? ' invalid' : ''}`}>
              {liveNameErr ??
                (isEdit
                  ? 'The name is immutable once created — it is the skill folder the runtime mounts.'
                  : 'Lowercase slug; becomes the skill folder the runtime mounts.')}
            </div>
          </div>
          <div className="field">
            <label htmlFor="sk-desc">Description</label>
            <input
              id="sk-desc"
              type="text"
              maxLength={DESCRIPTION_MAX}
              disabled={busy}
              placeholder="Analyzes competitor pricing pages and summarizes trends."
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
            <div className={`counter${descNearCap ? ' warn' : ''}`} data-testid="sk-desc-counter">
              {descLen} / {DESCRIPTION_MAX}
            </div>
          </div>
          <div className="field">
            <label htmlFor="sk-body">Instructions</label>
            <textarea
              id="sk-body"
              style={{ minHeight: 140 }}
              disabled={busy}
              placeholder="What the skill does and how the coworker should use it…"
              value={body}
              onChange={(e) => setBody(e.target.value)}
            />
            <div className="hint">
              Saved as the SKILL.md body — the name/description frontmatter is assembled for you
              and never shown.
            </div>
          </div>

          <details
            className="files"
            open={filesOpen}
            onToggle={(e) => setFilesOpen((e.target as HTMLDetailsElement).open)}
          >
            <summary>Add files or folders the coworker can read</summary>
            <div
              className={`dropzone${dragHover ? ' hover' : ''}`}
              onDragEnter={(e) => {
                e.preventDefault();
                dragDepth.current += 1;
                setDragHover(true);
              }}
              onDragOver={(e) => {
                e.preventDefault();
                if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
              }}
              onDragLeave={(e) => {
                e.preventDefault();
                dragDepth.current -= 1;
                if (dragDepth.current <= 0) {
                  dragDepth.current = 0;
                  setDragHover(false);
                }
              }}
              onDrop={(e) => void onDrop(e)}
            >
              <div className="dz-msg">
                {reading ? 'Reading files…' : 'Drop a folder or files here, or:'}
              </div>
              <div className="dz-btns">
                <label className={`dz-btn${controlsDisabled ? ' off' : ''}`}>
                  Choose folder
                  <input
                    type="file"
                    // @ts-expect-error non-standard attribute, widely supported
                    webkitdirectory=""
                    multiple
                    disabled={controlsDisabled}
                    onChange={(e) => void onPickInput(e)}
                  />
                </label>
                <label className={`dz-btn${controlsDisabled ? ' off' : ''}`}>
                  Choose files
                  <input
                    type="file"
                    multiple
                    disabled={controlsDisabled}
                    onChange={(e) => void onPickInput(e)}
                  />
                </label>
              </div>
              <div className="dz-note">
                Text files only, {formatBytes(MAX_UPLOAD_BYTES_PER_FILE)} per file,{' '}
                {formatBytes(MAX_UPLOAD_BYTES_TOTAL)} total.
              </div>
            </div>
            {uploadToast ? (
              <div className="upload-toast" role="status">
                {uploadToast}
              </div>
            ) : null}
            <div className="tree">
              <div className="tree-title">What this skill includes</div>
              <div className="tree-row" style={{ color: 'var(--rm-text-muted)' }}>
                <span aria-hidden="true">📄</span>
                <span className="t-name">SKILL.md</span>
                <span className="t-main-note">(from Instructions above)</span>
              </div>
              {groupByFolder(extraFiles).map(({ folder, entries }) => (
                <div key={folder || '__root__'}>
                  {folder ? (
                    <div className="tree-row tree-folder">
                      <span aria-hidden="true">📁</span>
                      <span>{folder}/</span>
                    </div>
                  ) : null}
                  {entries.map(({ file, index }) => (
                    <div key={file.path} className={`tree-row${folder ? ' indent' : ''}`}>
                      <span aria-hidden="true">📄</span>
                      <span className="t-name" title={file.path}>
                        {folder ? file.path.slice(folder.length + 1) : file.path}
                      </span>
                      <span className="t-size">{formatBytes(contentBytes(file.content))}</span>
                      <button
                        className="icon-btn danger"
                        title="Remove file"
                        disabled={busy}
                        onClick={() => removeFile(index)}
                      >
                        <Trash2 />
                      </button>
                    </div>
                  ))}
                </div>
              ))}
            </div>
          </details>
        </div>
        <div className="wiz-foot">
          {err ? (
            <span className="wiz-err" role="alert">
              {err}
            </span>
          ) : (
            <span />
          )}
          <span className="actions">
            <button className="btn-ghost" disabled={busy} onClick={onClose}>
              Cancel
            </button>
            <button className="btn-primary" disabled={!canSave} onClick={() => void save()}>
              {busy
                ? isEdit
                  ? 'Saving…'
                  : 'Creating…'
                : isEdit
                  ? 'Save changes'
                  : 'Create skill'}
            </button>
          </span>
        </div>
      </div>
    </div>
  );
}
