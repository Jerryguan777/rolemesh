// CoworkerCard — one manage row (spec §C.1 card anatomy, same card
// family as the agent picker). Row management renders only where
// `canManage(c, 'coworker.manage')` holds — the capability OR
// ownership of the row (the backend's ownership escape, mirrored via
// the copied helpers; never re-derived). Others see VIEW ONLY.
//
// All three action icons stopPropagation — card body click means
// Open chat.

import { ArrowRight, Pencil, Trash2, Users } from 'lucide-react';
import type { Coworker, Model } from '../../../api/client';
import { canManage, isOwnResource } from '../../../lib/capabilities';
import { coworkerSubtitle } from '../../../lib/coworker-label';

export function CoworkerCard({
  coworker,
  modelsById,
  shareBusy,
  rowError,
  onOpenChat,
  onToggleShare,
  onEdit,
  onDelete,
}: {
  coworker: Coworker;
  modelsById: Map<string, Model>;
  shareBusy: boolean;
  rowError: string | null;
  onOpenChat: () => void;
  onToggleShare: () => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const c = coworker;
  const manageable = canManage(c, 'coworker.manage');
  const shared = c.visibility === 'shared';
  return (
    <div
      className="card manage"
      role="button"
      tabIndex={0}
      onClick={onOpenChat}
      onKeyDown={(e) => {
        if (e.key === 'Enter') onOpenChat();
      }}
    >
      <div className="card-content">
        <div>
          <div className="provider">{coworkerSubtitle(c, modelsById)}</div>
          <div className="name">{c.name}</div>
        </div>
        <div className="pills">
          <span className={`pill pill-${c.visibility}`}>{c.visibility}</span>
          <span className={`pill pill-${c.status}`}>{c.status}</span>
          <span className="own-tag">
            {isOwnResource(c) ? 'Created by you' : 'Shared by another member'}
          </span>
        </div>
        {c.system_prompt ? <div className="desc">{c.system_prompt}</div> : null}
        <div className="card-actions">
          <button
            className="open-chat"
            onClick={(e) => {
              e.stopPropagation();
              onOpenChat();
            }}
          >
            Open chat
            <ArrowRight />
          </button>
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
                title="Edit coworker"
                onClick={(e) => {
                  e.stopPropagation();
                  onEdit();
                }}
              >
                <Pencil />
              </button>
              <button
                className="icon-btn danger"
                title="Delete coworker"
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
        {rowError ? <div className="row-error">{rowError}</div> : null}
      </div>
    </div>
  );
}
