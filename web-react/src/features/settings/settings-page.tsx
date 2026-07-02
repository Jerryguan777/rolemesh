// Settings route resolver: `#/manage/{slug}` → capability check →
// stub (all entries are stubs in the chat branch; real pages arrive
// per the §1.1 growth rule as lazy chunks keyed by the same slug).

import { Navigate, useParams } from 'react-router-dom';
import { hasCapability } from '../../lib/capabilities';
import { entryForSlug } from '../../app/nav';
import { AccessDeniedPage } from './access-denied-page';
import { StubPage } from './stub-page';

export function SettingsPage() {
  const { slug = '' } = useParams();
  const entry = entryForSlug(slug);
  if (!entry || entry.slug === null) {
    return <Navigate to="/" replace />;
  }
  if (entry.requires && !hasCapability(entry.requires)) {
    return <AccessDeniedPage label={entry.label} />;
  }
  return <StubPage label={entry.label} slug={entry.slug} />;
}
