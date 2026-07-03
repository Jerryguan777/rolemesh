// Rendered when the user navigates directly to a settings route their
// capabilities don't permit (nav rows are already hidden; this covers
// typed/bookmarked URLs). Mirrors the Lit access-denied page's role:
// UX courtesy only — the backend still enforces every action.

import { useNavigate } from 'react-router-dom';
import './settings.css';

export function AccessDeniedPage({ label }: { label: string }) {
  const navigate = useNavigate();
  return (
    <div className="settings-page">
      <h1>Access denied</h1>
      <button className="back-link" onClick={() => navigate('/')}>
        ← Back to chat
      </button>
      <div className="settings-panel denied">
        <p>
          Your account doesn't have permission to view <strong>{label}</strong>.
          Ask a workspace admin if you think you should have access.
        </p>
      </div>
    </div>
  );
}
