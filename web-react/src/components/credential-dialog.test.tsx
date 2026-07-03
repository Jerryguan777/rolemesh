// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { CredentialDialog } from './credential-dialog';
import type { CredentialResponse, ModelProvider } from '../api/client';

function withClient(node: ReactNode, seedCredentials?: CredentialResponse[]) {
  const qc = new QueryClient({
    // staleTime Infinity keeps the seeded cache authoritative so the
    // ['credentials'] query never background-fetches the real backend.
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  qc.setQueryData(['credentials'], seedCredentials ?? []);
  return render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>);
}

function renderDialog(
  provider: ModelProvider | null,
  seed?: CredentialResponse[],
) {
  const onClose = vi.fn();
  const onSaved = vi.fn();
  withClient(
    <CredentialDialog provider={provider} onClose={onClose} onSaved={onSaved} />,
    seed,
  );
  return { onClose, onSaved };
}

afterEach(cleanup);

describe('CredentialDialog', () => {
  it('null provider → renders the provider <select> with all four schemas', () => {
    renderDialog(null);
    const select = screen.getByLabelText('Provider') as HTMLSelectElement;
    expect(select).toBeTruthy();
    expect(select.querySelectorAll('option').length).toBe(4);
    // Default = first schema (anthropic) → its blurb shows.
    expect(
      screen.getByText('Anthropic Claude API key. Used directly by the Claude proxy.'),
    ).toBeTruthy();
  });

  it('switching the select swaps blurb + labels', () => {
    renderDialog(null);
    fireEvent.change(screen.getByLabelText('Provider'), {
      target: { value: 'bedrock' },
    });
    expect(screen.getByText('Bedrock API key')).toBeTruthy();
    expect(screen.getByText('Region')).toBeTruthy();
  });

  it('locked provider → no select, provider-specific title', () => {
    renderDialog('openai');
    expect(screen.queryByLabelText('Provider')).toBeNull();
    expect(screen.getByText('Add OpenAI credential')).toBeTruthy();
    // OpenAI carries an optional api_base extra.
    expect(screen.getByText('API base URL (optional)')).toBeTruthy();
  });

  it('bedrock seeds the region extra with its default', () => {
    renderDialog('bedrock');
    const region = screen.getByLabelText('Region') as HTMLInputElement;
    expect(region.value).toBe('us-east-1');
  });

  it('save gate: api_key required (validation blocks before any PUT)', () => {
    renderDialog('anthropic');
    fireEvent.click(screen.getByText('Save'));
    expect(screen.getByRole('alert').textContent).toBe('API key is required.');
  });

  it('save gate: required extra (bedrock region) must be non-empty', () => {
    renderDialog('bedrock');
    fireEvent.change(screen.getByLabelText('Bedrock API key'), {
      target: { value: 'ABSK-xyz' },
    });
    fireEvent.change(screen.getByLabelText('Region'), { target: { value: '  ' } });
    fireEvent.click(screen.getByText('Save'));
    expect(screen.getByRole('alert').textContent).toBe('Region is required.');
  });

  it('rotation affordance: an existing credential shows the stored-key placeholder', () => {
    renderDialog('anthropic', [
      {
        provider: 'anthropic',
        mode: 'byok',
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-06-28T10:00:00Z',
      },
    ]);
    const key = screen.getByLabelText('API key') as HTMLInputElement;
    expect(key.placeholder).toBe('•••••••• (stored — typing replaces it)');
  });

  it('fresh provider: placeholder is the schema example, not the stored hint', () => {
    renderDialog('anthropic', []);
    const key = screen.getByLabelText('API key') as HTMLInputElement;
    expect(key.placeholder).toBe('sk-ant-…');
  });
});
