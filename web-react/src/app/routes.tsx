// Route table (spec §9): chat at `#/`, flat settings under
// `#/manage/{slug}`, legacy v1.1 bookmarks rewritten via the ported
// redirect map. Settings load via React.lazy — the structural
// realization of lint-no-admin-chat's goal (a lean chat bundle).

import { Suspense, lazy, type ReactNode } from 'react';
import { Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { applyLegacyRedirect } from './legacy-redirects';
import { entryForSlug } from './nav';
import { hasCapability } from '../lib/capabilities';
import { ChatPage } from '../features/chat/chat-page';

const SettingsPage = lazy(() =>
  import('../features/settings/settings-page').then((m) => ({
    default: m.SettingsPage,
  })),
);

const AccessDeniedPage = lazy(() =>
  import('../features/settings/access-denied-page').then((m) => ({
    default: m.AccessDeniedPage,
  })),
);

/** Capability gate for GROWN settings routes (spec §8: direct navigation
 *  to a denied route renders the access-denied page). The generic
 *  `/manage/:slug` stub route already checks inside SettingsPage; grown
 *  pages have their own static routes, so the check lives here. */
function Gated({ slug, children }: { slug: string; children: ReactNode }) {
  const entry = entryForSlug(slug);
  if (entry?.requires && !hasCapability(entry.requires)) {
    return <AccessDeniedPage label={entry.label} />;
  }
  return <>{children}</>;
}

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

const ApprovalPoliciesPage = lazy(() =>
  import('../features/settings/approval-policies/approval-policies-page').then(
    (m) => ({ default: m.ApprovalPoliciesPage }),
  ),
);

const SafetyRulesPage = lazy(() =>
  import('../features/settings/safety-rules/safety-rules-page').then((m) => ({
    default: m.SafetyRulesPage,
  })),
);

const SafetyLogPage = lazy(() =>
  import('../features/settings/safety-log/safety-log-page').then((m) => ({
    default: m.SafetyLogPage,
  })),
);

const GeneralPage = lazy(() =>
  import('../features/settings/general/general-page').then((m) => ({
    default: m.GeneralPage,
  })),
);

const MembersPage = lazy(() =>
  import('../features/settings/members/members-page').then((m) => ({
    default: m.MembersPage,
  })),
);

const AppearancePage = lazy(() =>
  import('../features/settings/appearance/appearance-page').then((m) => ({
    default: m.AppearancePage,
  })),
);

const ConnectedChannelsPage = lazy(() =>
  import('../features/settings/connected-channels/connected-channels-page').then(
    (m) => ({ default: m.ConnectedChannelsPage }),
  ),
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
            <Gated slug="mcp-servers">
              <MCPServersPage />
            </Gated>
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
            <Gated slug="credentials">
              <CredentialsPage />
            </Gated>
          </Suspense>
        }
      />
      <Route
        path="/manage/approval-policies/*"
        element={
          <Suspense fallback={null}>
            <Gated slug="approval-policies">
              <ApprovalPoliciesPage />
            </Gated>
          </Suspense>
        }
      />
      <Route
        path="/manage/safety/*"
        element={
          <Suspense fallback={null}>
            <Gated slug="safety">
              <SafetyRulesPage />
            </Gated>
          </Suspense>
        }
      />
      <Route
        path="/manage/safety-log/*"
        element={
          <Suspense fallback={null}>
            <Gated slug="safety-log">
              <SafetyLogPage />
            </Gated>
          </Suspense>
        }
      />
      <Route
        path="/manage/general/*"
        element={
          <Suspense fallback={null}>
            <Gated slug="general">
              <GeneralPage />
            </Gated>
          </Suspense>
        }
      />
      {/* Personal page — no capability gate (nav requires: null).
          With this route every named settings entry is grown (A–N);
          the /manage/:slug stub is now unknown-slug fallback only. */}
      <Route
        path="/manage/appearance/*"
        element={
          <Suspense fallback={null}>
            <AppearancePage />
          </Suspense>
        }
      />
      {/* Personal page — no capability gate (nav requires: null). */}
      <Route
        path="/manage/connected-channels/*"
        element={
          <Suspense fallback={null}>
            <ConnectedChannelsPage />
          </Suspense>
        }
      />
      <Route
        path="/manage/members/*"
        element={
          <Suspense fallback={null}>
            <Gated slug="members">
              <MembersPage />
            </Gated>
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
