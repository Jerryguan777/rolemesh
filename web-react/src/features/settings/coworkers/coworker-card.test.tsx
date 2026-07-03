// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { CoworkerCard } from './coworker-card';
import { setMe } from '../../../lib/capabilities';
import type { Coworker, Me } from '../../../api/client';

function me(capabilities: string[], userId = 'u1'): Me {
  return {
    user_id: userId,
    tenant_id: 't1',
    role: 'member',
    plane: 'tenant',
    capabilities,
  };
}

function coworker(overrides: Partial<Coworker> = {}): Coworker {
  return {
    id: 'cw1',
    tenant_id: 't1',
    name: 'Portfolio Manager',
    folder: 'pm',
    agent_backend: 'claude',
    status: 'active',
    max_concurrent_containers: 1,
    visibility: 'private',
    permissions: {},
    created_at: '2026-07-01T00:00:00Z',
    created_by_user_id: 'someone-else',
    system_prompt: 'Handles portfolio intents',
    ...overrides,
  } as Coworker;
}

function renderCard(c: Coworker) {
  return render(
    <CoworkerCard
      coworker={c}
      modelsById={new Map()}
      shareBusy={false}
      rowError={null}
      onOpenChat={vi.fn()}
      onToggleShare={vi.fn()}
      onEdit={vi.fn()}
      onDelete={vi.fn()}
    />,
  );
}

afterEach(() => {
  cleanup();
  setMe(null);
});

// Pins the ownership escape (spec C.2): row management = the
// `coworker.manage` capability OR ownership of the row — mirrored via
// the copied canManage/isOwnResource helpers, never re-derived.
describe('CoworkerCard capability rendering', () => {
  it('member WITHOUT coworker.manage sees VIEW ONLY on others’ rows', () => {
    setMe(me(['coworker.use']));
    renderCard(coworker({ created_by_user_id: 'someone-else' }));
    expect(screen.getByText('View only')).toBeTruthy();
    expect(screen.queryByTitle('Edit coworker')).toBeNull();
  });

  it('member WITHOUT coworker.manage gets full icons on their OWN row', () => {
    setMe(me(['coworker.use'], 'u1'));
    renderCard(coworker({ created_by_user_id: 'u1' }));
    expect(screen.queryByText('View only')).toBeNull();
    expect(screen.getByTitle('Edit coworker')).toBeTruthy();
    expect(screen.getByTitle('Delete coworker')).toBeTruthy();
  });

  it('manager gets icons on any row; ownership tag reflects the creator', () => {
    setMe(me(['coworker.manage']));
    renderCard(coworker({ created_by_user_id: 'someone-else' }));
    expect(screen.getByTitle('Edit coworker')).toBeTruthy();
    expect(screen.getByText('Shared by another member')).toBeTruthy();
  });

  it('system-created rows (null creator) are never "own"', () => {
    setMe(me(['coworker.use'], 'u1'));
    renderCard(coworker({ created_by_user_id: null }));
    expect(screen.getByText('View only')).toBeTruthy();
    expect(screen.getByText('Shared by another member')).toBeTruthy();
  });

  it('share toggle exposes aria-pressed and the shared pill', () => {
    setMe(me(['coworker.manage']));
    renderCard(coworker({ visibility: 'shared' }));
    const share = screen.getByTitle('Make private');
    expect(share.getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByText('shared')).toBeTruthy();
  });
});
