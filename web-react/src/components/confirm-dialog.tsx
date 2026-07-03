// ConfirmDialog — the shared destructive-action confirm (spec §C.5;
// behavioral reference web/src/components/confirm-dialog.ts). Lives in
// components/ because delete confirms recur on every settings page
// (the ≥2-consumers admission rule, pre-satisfied).
//
// Busy semantics: while `busy`, the confirm button shows `busyLabel`
// and BOTH dismiss paths (cancel button, scrim, ESC, X) are inert —
// double-submit and mid-flight dismissal are guarded here, not in the
// caller.

import { useEffect, type ReactNode } from 'react';
import { X } from 'lucide-react';

export function ConfirmDialog({
  title,
  children,
  confirmLabel,
  busyLabel,
  busy,
  disableConfirm = false,
  onConfirm,
  onCancel,
}: {
  title: string;
  children: ReactNode;
  confirmLabel: string;
  busyLabel: string;
  busy: boolean;
  /** Blocks the confirm action while the dialog stays open (e.g. a
   *  binding-aware delete block). Cancel/ESC/scrim still dismiss. */
  disableConfirm?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key !== 'Escape') return;
      e.stopPropagation();
      if (!busy) onCancel();
    }
    // Capture phase so an open confirm wins over page-level ESC
    // handlers (asides, wizard) — prototype's dismissal priority.
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [busy, onCancel]);

  return (
    <div
      className="scrim"
      onClick={(e) => {
        if (e.target === e.currentTarget && !busy) onCancel();
      }}
    >
      <div className="dlg small" role="alertdialog" aria-modal="true" aria-label={title}>
        <div className="dlg-header">
          <div className="hleft">
            <h2 className="dlg-title">{title}</h2>
          </div>
          <button
            className="icon-btn"
            aria-label="Close"
            disabled={busy}
            onClick={onCancel}
          >
            <X />
          </button>
        </div>
        <div className="confirm-body">{children}</div>
        <div className="confirm-foot">
          <button className="btn-ghost" disabled={busy} onClick={onCancel}>
            Cancel
          </button>
          <button
            className="btn-danger"
            disabled={busy || disableConfirm}
            onClick={onConfirm}
          >
            {busy ? busyLabel : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
