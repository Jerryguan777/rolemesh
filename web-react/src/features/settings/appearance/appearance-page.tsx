// AppearancePage — read-only theme surface (spec Part N; behavioral
// reference the shipped Lit appearance-page.ts).
//
// D-N1: the Lit page's premise (both palettes live under
// prefers-color-scheme) doesn't transfer — this app's token set is
// light-only — but its PURPOSE does: preempt "where's the dark-mode
// toggle?" with the truth. The copy states today's reality; the live
// detection row keeps the page forward-compatible (when dark tokens
// land, only the copy changes — use-system-theme.ts already works).
//
// Locked decision carried over: no in-app toggle, no persistence.
// Read-only means ZERO controls — the test suite smoke-asserts this.
// The card styles are inline rather than borrowed from a sibling
// chunk's CSS (.cc-panel) — the load-order trap ui.css exists to
// prevent.

import { ArrowLeft } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import { useSystemTheme } from './use-system-theme';

export function AppearancePage() {
  const navigate = useNavigate();
  const theme = useSystemTheme();
  const dark = theme === 'dark';

  return (
    <div className="page">
      <div>
        <button className="back-link" onClick={() => navigate('/')}>
          <ArrowLeft />
          Back to chat
        </button>
      </div>
      <div className="page-head">
        <div>
          <h1 className="page-title">Appearance</h1>
        </div>
      </div>

      <div className="grid-scroll" style={{ paddingTop: 12 }}>
        <div
          style={{
            border: '1px solid var(--rm-border)',
            borderRadius: 4,
            background: 'var(--rm-card-bg)',
            padding: 16,
            maxWidth: 760, // D-UI2 — one cap for every settings surface

          }}
        >
          <b>Theme</b>
          <div className="hint" style={{ margin: '4px 0 12px' }}>
            This interface ships the light palette today — there is no in-app toggle. A
            dark palette that follows your operating-system setting is planned; the
            detected setting below will drive it automatically once it lands.
          </div>
          <div className="model-row" style={{ marginBottom: 0 }} data-testid="app-theme-row">
            <span aria-hidden="true">{dark ? '🌙' : '☀️'}</span>
            <span>
              <div className="m-name">System: {dark ? 'Dark' : 'Light'}</div>
              <div className="m-sub" style={{ fontFamily: 'var(--font-base)' }}>
                Detected from your OS · updates live · nothing is stored
              </div>
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
