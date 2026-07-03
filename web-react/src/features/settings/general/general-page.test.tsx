// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { ApiError, type TenantResponse } from '../../../api/client';
import { GeneralPage } from './general-page';

const TENANT: TenantResponse = {
  id: 't-1',
  name: 'Acme Capital',
  slug: 'acme-capital',
  plan: 'team',
  max_concurrent_containers: 2,
  created_at: '2026-04-02T09:00:00Z',
} as TenantResponse;

function renderPage(seed: TenantResponse | 'forbidden') {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  if (seed === 'forbidden') {
    // Pre-populate the query cache with a settled 403 error state.
    void qc.prefetchQuery({
      queryKey: ['tenant'],
      queryFn: () =>
        Promise.reject(new ApiError(403, { code: 'FORBIDDEN', message: 'nope' }, 'nope')),
      retry: false,
    });
  } else {
    qc.setQueryData(['tenant'], seed);
  }
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <GeneralPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(cleanup);

describe('GeneralPage', () => {
  it('renders the wire fields: editable name/mcc, disabled slug, plan pill, created date', () => {
    renderPage(TENANT);
    expect((screen.getByLabelText('Workspace name') as HTMLInputElement).value).toBe(
      'Acme Capital',
    );
    const slug = screen.getByLabelText('Workspace slug') as HTMLInputElement;
    expect(slug.disabled).toBe(true);
    expect(slug.value).toBe('acme-capital');
    expect(screen.getByText('team').className).toContain('pill-active');
    expect(
      (screen.getByLabelText('Concurrent coworker tasks') as HTMLInputElement).value,
    ).toBe('2');
  });

  it('Save disabled when clean; dirty enables Save + reveals Revert; Revert restores', () => {
    renderPage(TENANT);
    const save = screen.getByTestId('gen-save') as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    expect(screen.queryByTestId('gen-revert')).toBeNull();
    fireEvent.change(screen.getByLabelText('Workspace name'), {
      target: { value: 'Acme Capital LLC' },
    });
    expect(save.disabled).toBe(false);
    fireEvent.click(screen.getByTestId('gen-revert'));
    expect((screen.getByLabelText('Workspace name') as HTMLInputElement).value).toBe(
      'Acme Capital',
    );
    expect((screen.getByTestId('gen-save') as HTMLButtonElement).disabled).toBe(true);
  });

  it('validation replaces the hint line in danger color and blocks Save', () => {
    renderPage(TENANT);
    fireEvent.change(screen.getByLabelText('Workspace name'), { target: { value: '  ' } });
    expect(screen.getByText('Workspace name is required.')).toBeTruthy();
    expect((screen.getByTestId('gen-save') as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('Workspace name'), {
      target: { value: 'Acme' },
    });
    fireEvent.change(screen.getByLabelText('Concurrent coworker tasks'), {
      target: { value: '0' },
    });
    expect(screen.getByText('Must be a whole number of at least 1.')).toBeTruthy();
    expect((screen.getByTestId('gen-save') as HTMLButtonElement).disabled).toBe(true);
    // Non-numeric input also blocks (NaN sentinel).
    fireEvent.change(screen.getByLabelText('Concurrent coworker tasks'), {
      target: { value: 'abc' },
    });
    expect((screen.getByTestId('gen-save') as HTMLButtonElement).disabled).toBe(true);
  });

  it('nullable slug/plan degrade gracefully', () => {
    renderPage({ ...TENANT, slug: null, plan: null } as TenantResponse);
    expect((screen.getByLabelText('Workspace slug') as HTMLInputElement).value).toBe('—');
    expect(screen.queryByText('team')).toBeNull();
  });

  it('GET 403 renders the friendly owner-only notice, not a raw error', async () => {
    renderPage('forbidden');
    expect(
      await screen.findByText('Only the workspace owner can view general settings.'),
    ).toBeTruthy();
    expect(screen.queryByLabelText('Workspace name')).toBeNull();
  });
});
