// SkillCard — one catalog row (spec E.1). Manage-card family, but no
// provider line (skills have no backend/model). The bound count comes
// straight off the list payload (SkillSummary.bound_coworker_count) —
// no client fan-out (unlike Part D). Row click opens the edit dialog
// (Lit parity), so the card body is a button.

import { Pencil, Trash2, Users } from 'lucide-react';
import type { SkillSummary } from '../../../api/client';
import { canManage, isOwnResource } from '../../../lib/capabilities';

export function SkillCard({
  skill,
  shareBusy,
  deleteError,
  shareError,
  onOpen,
  onToggleShare,
  onEdit,
  onDelete,
}: {
  skill: SkillSummary;
  shareBusy: boolean;
  deleteError: string | null;
  shareError: string | null;
  onOpen: () => void;
  onToggleShare: () => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const s = skill;
  const manageable = canManage(s, 'skill.manage');
  const shared = s.visibility === 'shared';
  const n = s.bound_coworker_count;
  return (
    <div
      className="card manage"
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === 'Enter') onOpen();
      }}
    >
      <div className="card-content">
        <div className="name">{s.name}</div>
        {s.description ? <div className="desc">{s.description}</div> : null}
        <div className="pills">
          <span className={`pill pill-${s.visibility}`}>{s.visibility}</span>
          {/* disabled pill only when explicitly disabled — enabled is
              the unmarked default (no "enabled" pill). */}
          {!s.enabled ? <span className="pill pill-off">disabled</span> : null}
          <span className="own-tag">
            {isOwnResource(s) ? 'Created by you' : 'Shared by another member'}
          </span>
        </div>
        <div className="card-actions">
          <span className={`usage${n > 0 ? ' bound' : ''}`}>
            {n > 0
              ? `Bound to ${n} coworker${n === 1 ? '' : 's'}`
              : 'Not bound to any coworker'}
          </span>
          {manageable ? (
            <span className="icon-acts">
              <button
                className={`icon-btn${shared ? ' on' : ''}`}
                aria-pressed={shared}
                disabled={shareBusy}
                title={shared ? 'Make private' : 'Share with everyone in this workspace'}
                onClick={(e) => {
                  e.stopPropagation();
                  onToggleShare();
                }}
              >
                <Users />
              </button>
              <button
                className="icon-btn"
                title="Edit skill"
                onClick={(e) => {
                  e.stopPropagation();
                  onEdit();
                }}
              >
                <Pencil />
              </button>
              <button
                className="icon-btn danger"
                title="Delete skill"
                onClick={(e) => {
                  e.stopPropagation();
                  onDelete();
                }}
              >
                <Trash2 />
              </button>
            </span>
          ) : (
            <span className="view-only">View only</span>
          )}
        </div>
        {/* Two error slots (Lit parity): delete and share failures. */}
        {deleteError ? <div className="row-error">{deleteError}</div> : null}
        {shareError ? <div className="row-error">{shareError}</div> : null}
      </div>
    </div>
  );
}
