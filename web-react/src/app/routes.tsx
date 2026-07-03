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

const MCPServersPage = lazy(() =>
  import('../features/settings/mcp-servers/mcp-servers-page').then((m) => ({
    default: m.MCPServersPage,
  })),
);

const SkillsPage = lazy(() =>
  import('../features/settings/skills/skills-page').then((m) => ({
    default: m.SkillsPage,
  })),
);

const ModelsPage = lazy(() =>
  import('../features/settings/models/models-page').then((m) => ({
    default: m.ModelsPage,
  })),
);

const CredentialsPage = lazy(() =>
  import('../features/settings/credentials/credentials-page').then((m) => ({
    default: m.CredentialsPage,
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
        path="/manage/mcp-servers/*"
        element={
          <Suspense fallback={null}>
            <MCPServersPage />
          </Suspense>
        }
      />
      <Route
        path="/manage/skills/*"
        element={
          <Suspense fallback={null}>
            <SkillsPage />
          </Suspense>
        }
      />
      <Route
        path="/manage/credentials/*"
        element={
          <Suspense fallback={null}>
            <CredentialsPage />
          </Suspense>
        }
      />
      <Route
        path="/manage/models/*"
        element={
          <Suspense fallback={null}>
            <ModelsPage />
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
