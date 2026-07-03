// AgentPickerModal — single-select coworker picker (spec §7). Visual
// anatomy = prototype .dlg verbatim; behavioral deltas: selecting is
// COMMITTING (one click = close + navigate), the active coworker's
// card renders pre-selected, and the Assistants tab is a placeholder
// until a curated-assistants concept exists on the wire (D-4).

import { useState } from 'react';
import { Search, X } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import type { Coworker, Model } from '../../../api/client';
import { BrandMark } from '../../../components/brand-mark';
import { hasCapability } from '../../../lib/capabilities';
import { COPY } from '../../../app/copy';
import { AgentCard } from './agent-card';
import './agent-picker.css';

type Tab = 'assistants' | 'agents';

export function AgentPickerModal({
  coworkers,
  modelsById,
  activeAgentId,
  onSelect,
  onClose,
}: {
  coworkers: readonly Coworker[];
  modelsById: Map<string, Model>;
  activeAgentId: string | null;
  onSelect: (id: string) => void;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<Tab>('agents');
  const [query, setQuery] = useState('');
  const navigate = useNavigate();

  const q = query.trim().toLowerCase();
  const visible = coworkers.filter(
    (c) =>
      !q ||
      `${c.name} ${c.routing_description ?? ''}`.toLowerCase().includes(q),
  );

  // Creation is gated on `coworker.create` (spec C.2) — `coworker.manage`
  // is the row-management capability, a different grant.
  const canCreate = hasCapability('coworker.create');

  return (
    <div
      className="scrim"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="dlg picker" role="dialog" aria-modal="true" aria-label="RoleMesh assistants and agents">
        <div className="dlg-header">
          <div className="hleft">
            <div className="dlg-brand-icon">
              <BrandMark size={16} />
            </div>
            <h2 className="dlg-title">{COPY.pickerTitle}</h2>
          </div>
          <button className="icon-btn" aria-label="Close" onClick={onClose}>
            <X />
          </button>
        </div>
        <div className="tabs-shell">
          <div className="tabs-bar" role="tablist">
            <button
              className={`tab${tab === 'assistants' ? ' active' : ''}`}
              role="tab"
              aria-selected={tab === 'assistants'}
              onClick={() => setTab('assistants')}
            >
              {COPY.tabAssistants}
            </button>
            <button
              className={`tab${tab === 'agents' ? ' active' : ''}`}
              role="tab"
              aria-selected={tab === 'agents'}
              onClick={() => setTab('agents')}
            >
              {COPY.tabAgents}
            </button>
          </div>
          <div className="tabs-content">
            {tab === 'assistants' ? (
              <div className="tab-empty">{COPY.assistantsPlaceholder}</div>
            ) : (
              <>
                <div className="search-row">
                  <div className="search-field">
                    <input
                      type="text"
                      placeholder="Search"
                      value={query}
                      autoFocus
                      onChange={(e) => setQuery(e.target.value)}
                    />
                    <span className="search-ic">
                      <Search />
                    </span>
                  </div>
                </div>
                <div className="masonry-scroll">
                  {coworkers.length === 0 ? (
                    <div className="tab-empty">
                      {canCreate ? (
                        <>
                          <p>No agents yet.</p>
                          <button
                            className="btn-primary"
                            onClick={() => {
                              onClose();
                              navigate('/manage/coworkers');
                            }}
                          >
                            Create a coworker
                          </button>
                        </>
                      ) : (
                        <p>No agents yet — ask a workspace admin to create one.</p>
                      )}
                    </div>
                  ) : visible.length === 0 ? (
                    <div className="tab-empty">No agents match your search.</div>
                  ) : (
                    <div className="masonry" role="radiogroup" aria-label="Agents">
                      {visible.map((c) => (
                        <AgentCard
                          key={c.id}
                          coworker={c}
                          modelsById={modelsById}
                          selected={c.id === activeAgentId}
                          onSelect={() => onSelect(c.id)}
                        />
                      ))}
                    </div>
                  )}
                </div>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
