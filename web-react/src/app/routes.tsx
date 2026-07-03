// Route table (spec §9): chat at `#/`, flat settings under
// `#/manage/{slug}`, legacy v1.1 bookmarks rewritten via the ported
// redirect map. Settings load via React.lazy — the structural
// realization of lint-no-admin-chat's goal (a lean chat bundle).

import { Suspense, lazy } from 'react';
import { Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { applyLegacyRedirect } from './legacy-redirects';
import { ChatPage } from '../features/chat/chat-page';

const SettingsPage = lazy(() =>
  import('../features/settings/settings-page').then((m) => ({
    default: m.SettingsPage,
  })),
);

// Grown settings pages get their own lazy chunk keyed by slug (§1.1
// settings growth rule) — the static route ranks above /manage/:slug.
const CoworkersPage = lazy(() =>
  import('../features/settings/coworkers/coworkers-page').then((m) => ({
    default: m.CoworkersPage,
  })),
);

/** Catch-all: rewrite v1.1 flat bookmarks (query strings survive the
 *  redirect); anything else falls back to chat — same default the Lit
 *  topLevelShell() uses. */
function LegacyRedirect() {
  const loc = useLocation();
  const target = applyLegacyRedirect(`#${loc.pathname}${loc.search}`);
  return <Navigate to={target ? target.slice(1) : '/'} replace />;
}

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<ChatPage />} />
      <Route
        path="/manage/coworkers/*"
        element={
          <Suspense fallback={null}>
            <CoworkersPage />
          </Suspense>
        }
      />
      <Route
        path="/manage/:slug/*"
        element={
          <Suspense fallback={null}>
            <SettingsPage />
          </Suspense>
        }
      />
      <Route path="*" element={<LegacyRedirect />} />
    </Routes>
  );
}
