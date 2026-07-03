// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import type { Me, UserPage, UserResponse } from '../../../api/client';
import { setMe } from '../../../lib/capabilities';
import { MembersPage } from './members-page';

const OWNER_ME: Me = {
  user_id: 'u-jerry',
  tenant_id: 't-1',
  name: 'Jerry',
  role: 'owner',
  plane: 'tenant',
  capabilities: ['user.manage'],
} as Me;

const USERS: UserResponse[] = [
  {
    id: 'u-jerry',
    tenant_id: 't-1',
    name: 'Jerry Guan',
    email: 'jerry@acme.example',
    role: 'owner',
    channel_ids: { telegram: '12345' },
    created_at: '2026-04-02T09:00:00Z',
  },
  {
    id: 'u-sam',
    tenant_id: 't-1',
    name: 'Sam Ortiz',
    email: null,
    role: 'member',
    channel_ids: {},
    created_at: '2026-05-03T09:00:00Z',
  },
] as UserResponse[];

function renderPage(me: Me = OWNER_ME, users: UserResponse[] = USERS, total?: number) {
  setMe(me);
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  const pageData: UserPage = {
    items: users,
    total: total ?? users.length,
    limit: 20,
    offset: 0,
  };
  qc.setQueryData(['users', 0, 20], pageData);
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <MembersPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  setMe(null);
});

describe('MembersPage', () => {
  it('renders rows: You tag on own row, channel chips, email fallback, role pills', () => {
    renderPage();
    expect(screen.getByText('You')).toBeTruthy();
    expect(screen.getByText('telegram linked').className).toContain('chan-tag');
    expect(screen.getByText('no email')).toBeTruthy();
    expect(screen.getByText('owner').className).toContain('role-pill--owner');
    expect(screen.getByText('member').className).toContain('role-pill--member');
  });

  it('Remove is absent on the caller\'s own row, present on others', () => {
    renderPage();
    const own = screen.getByTestId('mem-row-u-jerry');
    const other = screen.getByTestId('mem-row-u-sam');
    expect(own.querySelector('[title="Remove from workspace"]')).toBeNull();
    expect(other.querySelector('[title="Remove from workspace"]')).toBeTruthy();
    expect(own.querySelector('[title="Edit member"]')).toBeTruthy();
  });

  it('remove confirm states resources remain; owner target adds the last-owner caution', () => {
    const owners = [
      ...USERS,
      { ...USERS[0], id: 'u-lena', name: 'Lena Fis', channel_ids: {} } as UserResponse,
    ];
    renderPage(OWNER_ME, owners);
    // Non-owner target: no caution.
    fireEvent.click(
      screen
        .getByTestId('mem-row-u-sam')
        .querySelector('[title="Remove from workspace"]')!,
    );
    expect(screen.getByText(/resources they created remain/)).toBeTruthy();
    expect(screen.queryByText(/is an owner/)).toBeNull();
    fireEvent.click(screen.getByText('Cancel'));
    // Owner target: caution appended.
    fireEvent.click(
      screen
        .getByTestId('mem-row-u-lena')
        .querySelector('[title="Remove from workspace"]')!,
    );
    expect(screen.getByText(/Lena Fis is an owner\./)).toBeTruthy();
    expect(screen.getByText(/does not block removing the last one/)).toBeTruthy();
  });

  it('add dialog for an admin caller: owner option disabled with the server-enforces hint', () => {
    renderPage({ ...OWNER_ME, user_id: 'u-ada', role: 'admin' } as Me);
    fireEvent.click(screen.getByTestId('mem-add'));
    const owner = screen.getByText('owner (owners only)') as HTMLOptionElement;
    expect(owner.disabled).toBe(true);
    expect(
      screen.getByText('Only owners can grant the owner role — the server enforces this.'),
    ).toBeTruthy();
    // Save gated on name.
    const save = screen.getByTestId('mem-save') as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'New Person' } });
    expect(save.disabled).toBe(false);
  });

  it('owner editing their own row to a lower role sees the advisory', () => {
    renderPage();
    fireEvent.click(
      screen.getByTestId('mem-row-u-jerry').querySelector('[title="Edit member"]')!,
    );
    expect(screen.getByText('Edit Jerry Guan')).toBeTruthy();
    // Email-clear hint is edit-mode only.
    expect(screen.getByText('Clearing this field removes the stored email.')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Role'), { target: { value: 'member' } });
    expect(
      screen.getByText('⚠ Lowering your own role may remove your access to this page.'),
    ).toBeTruthy();
  });

  it('pager appears only when total exceeds the page size', () => {
    renderPage(OWNER_ME, USERS, 45);
    const pager = screen.getByTestId('mem-pager');
    expect(pager.textContent).toContain('Showing 1–2 of 45');
    cleanup();
    renderPage(OWNER_ME, USERS, 2);
    expect(screen.queryByTestId('mem-pager')).toBeNull();
  });
});
