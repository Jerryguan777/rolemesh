// Shared stub for every not-yet-built settings entry (spec §8): title
// header + a "classic UI" panel so users are never dead-ended. A real
// page replaces this route with its own features/settings/<slug>/
// folder + lazy chunk (§1.1 settings growth rule).

import { useNavigate } from 'react-router-dom';
import './settings.css';

export function StubPage({ label, slug }: { label: string; slug: string }) {
  const navigate = useNavigate();
  return (
    <div className="settings-page">
      <h1>{label}</h1>
      <button className="back-link" onClick={() => navigate('/')}>
        ← Back to chat
      </button>
      <div className="settings-panel">
        <p>
          This page is available in the classic UI. Open the Lit app (served
          with <code>WEB_UI_DIST=web/dist</code>) and navigate to{' '}
          <code>{`#/manage/${slug}`}</code>.
        </p>
      </div>
    </div>
  );
}
