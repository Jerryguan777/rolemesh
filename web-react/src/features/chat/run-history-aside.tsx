// RunHistoryAside — the Debug Panel stub (spec §5.3, D-6): chrome +
// empty state only in v1. Populating it from GET /runs and
// event.delegation.* is a tracked stretch item.

import { X } from 'lucide-react';
import { COPY } from '../../app/copy';

export function RunHistoryAside({ onClose }: { onClose: () => void }) {
  return (
    <aside className="aside-panel debug" aria-label="Run history">
      <div className="aside-header">
        <div>
          <h3 className="aside-title">{COPY.debugTitle}</h3>
        </div>
        <button className="icon-btn" aria-label="Close" onClick={onClose}>
          <X />
        </button>
      </div>
      <div className="aside-body">
        <p className="aside-empty">{COPY.debugEmpty}</p>
      </div>
    </aside>
  );
}
