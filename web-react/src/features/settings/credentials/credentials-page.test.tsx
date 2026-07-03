// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { CredentialsPage } from './credentials-page';
import type { CredentialResponse } from '../../../api/client';

function renderPage(seed: CredentialResponse[]) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  qc.setQueryData(['credentials'], seed);
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <CredentialsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(cleanup);

const anthropicSet: CredentialResponse = {
  provider: 'anthropic',
  mode: 'byok',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-06-28T10:00:00Z',
};

describe('CredentialsPage', () => {
  it('renders four FIXED provider rows regardless of what exists', () => {
    renderPage([]);
    for (const label of ['Anthropic', 'AWS Bedrock', 'Google', 'OpenAI']) {
      expect(screen.getByText(label)).toBeTruthy();
    }
    // Nothing configured → four "missing" pills, no delete buttons.
    expect(screen.getAllByText('missing').length).toBe(4);
    expect(screen.queryByText('set')).toBeNull();
    expect(screen.queryByLabelText('Delete credential')).toBeNull();
  });

  it('a configured provider shows set + a delete button; others stay missing', () => {
    renderPage([anthropicSet]);
    expect(screen.getByText('set')).toBeTruthy();
    expect(screen.getAllByText('missing').length).toBe(3);
    // Exactly one delete button (only the set row).
    expect(screen.getAllByLabelText('Delete credential').length).toBe(1);
    // The set row's pencil reads "Rotate", missing rows read "Add".
    expect(screen.getByLabelText('Rotate credential')).toBeTruthy();
    expect(screen.getAllByLabelText('Add credential').length).toBe(3);
  });

  it('header Add credential opens the dialog with the provider select', () => {
    const { container } = renderPage([]);
    // Header primary button (row pencils share the "Add credential"
    // name, so disambiguate by the primary-button class).
    fireEvent.click(container.querySelector('.page-head .btn-primary')!);
    expect(screen.getByLabelText('Provider')).toBeTruthy();
  });

  it('row pencil opens the dialog locked to that provider (no select)', () => {
    renderPage([anthropicSet]);
    fireEvent.click(screen.getByLabelText('Rotate credential'));
    expect(screen.getByText('Add Anthropic credential')).toBeTruthy();
    expect(screen.queryByLabelText('Provider')).toBeNull();
  });

  it('delete opens the confirm dialog naming the provider', () => {
    renderPage([anthropicSet]);
    fireEvent.click(screen.getByLabelText('Delete credential'));
    expect(screen.getByText('Delete Anthropic credential?')).toBeTruthy();
    const dialog = screen.getByRole('alertdialog');
    expect(within(dialog).getByText('Delete')).toBeTruthy();
  });
});
