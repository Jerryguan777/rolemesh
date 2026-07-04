// RejectNoteForm — the inline reject-with-note expansion (spec O.3 /
// Lit §3.5). A single Reject click on the card opens THIS; only the
// form's Reject button submits (with the trimmed note — an empty
// submit omits the field, the wire's nullable). Cancel collapses with
// no frame sent. Both paths are inert while a decision is in flight
// (the busy-guard lives on the card's canAct, mirrored here).

import { useState } from 'react';

export function RejectNoteForm({
  busy,
  onCancel,
  onReject,
}: {
  busy: boolean;
  onCancel: () => void;
  onReject: (note: string | undefined) => void;
}) {
  const [note, setNote] = useState('');

  return (
    <div className="apr-reject-form" data-testid="approval-reject-form">
      <div className="hint">
        Tell the coworker why (optional). This text becomes the tool-call rejection
        reason — they&rsquo;ll read it and adjust.
      </div>
      <textarea
        data-testid="approval-note"
        maxLength={500}
        placeholder="e.g. amount is too high — needs senior signoff above $5k"
        value={note}
        disabled={busy}
        onChange={(e) => setNote(e.target.value)}
      />
      <div className="apr-acts" style={{ marginTop: 8 }}>
        <button
          className="btn-ghost"
          data-testid="approval-reject-cancel"
          disabled={busy}
          onClick={onCancel}
        >
          Cancel
        </button>
        <button
          className="btn-danger"
          data-testid="approval-reject-confirm"
          disabled={busy}
          onClick={() => onReject(note.trim() || undefined)}
        >
          {busy ? 'Rejecting…' : 'Reject'}
        </button>
      </div>
    </div>
  );
}
