// MCPServerCard — one registry row (spec D.1). Same manage-card family
// as the coworkers page, but no visibility/ownership (tenant
// infrastructure — the whole page is gated by its nav capability, not
// per-row). The URL is the key operational datum, rendered monospace.

import { Pencil, Trash2 } from 'lucide-react';
import type { MCPServer } from '../../../api/client';

export function MCPServerCard({
  server,
  usageCount,
  rowError,
  onEdit,
  onDelete,
}: {
  server: MCPServer;
  usageCount: number;
  rowError: string | null;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const bound = usageCount > 0;
  return (
    <div className="card manage">
      <div className="card-content">
        <div>
          <div className="provider">
            {server.type} · auth: {server.auth_mode}
          </div>
          <div className="name">{server.name}</div>
        </div>
        <div className="mono">{server.url}</div>
        {server.description ? <div className="desc">{server.description}</div> : null}
        <div className="card-actions">
          <span className={`usage${bound ? ' bound' : ''}`}>
            {bound
              ? `Used by ${usageCount} coworker${usageCount === 1 ? '' : 's'}`
              : 'Not bound to any coworker'}
          </span>
          <span className="icon-acts">
            <button className="icon-btn" title="Edit MCP server" onClick={onEdit}>
              <Pencil />
            </button>
            <button
              className="icon-btn danger"
              title="Delete MCP server"
              onClick={onDelete}
            >
              <Trash2 />
            </button>
          </span>
        </div>
        {rowError ? <div className="row-error">{rowError}</div> : null}
      </div>
    </div>
  );
}
