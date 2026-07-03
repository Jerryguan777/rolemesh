// AppSidebar — brand card + static route nav (spec §5.1). The active
// row is derived from the current route, never from click-state, and
// entries are capability-gated through the Me cache (fail-closed:
// `requires`-gated entries are hidden until a Me is set).

import { useLocation, useNavigate } from 'react-router-dom';
import { BrandMark } from '../../components/brand-mark';
import { hasCapability } from '../../lib/capabilities';
import { NAV_ENTRIES, type NavEntry } from '../../app/nav';

function hrefFor(entry: NavEntry): string {
  return entry.slug === null ? '/' : `/manage/${entry.slug}`;
}

function isActive(entry: NavEntry, pathname: string): boolean {
  if (entry.slug === null) return pathname === '/' || pathname === '';
  return (
    pathname === `/manage/${entry.slug}` ||
    pathname.startsWith(`/manage/${entry.slug}/`)
  );
}

export function AppSidebar() {
  const { pathname } = useLocation();
  const navigate = useNavigate();

  const visible = NAV_ENTRIES.filter(
    (e) => !e.requires || hasCapability(e.requires),
  );

  return (
    <aside className="rm-sidebar">
      <div className="brand-card">
        <div className="brand-logo">
          <BrandMark size={80} />
        </div>
        <div className="brand-names">
          <div className="sub">RoleMesh</div>
          <div className="title">Agent Workspace</div>
        </div>
      </div>
      <nav className="rm-nav" aria-label="Main">
        {visible.map((entry) => (
          <button
            key={entry.label}
            className={isActive(entry, pathname) ? 'active' : ''}
            onClick={() => navigate(hrefFor(entry))}
          >
            {entry.label}
          </button>
        ))}
      </nav>
    </aside>
  );
}
