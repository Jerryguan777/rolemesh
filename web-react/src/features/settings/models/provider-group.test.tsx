// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { ProviderGroup } from './provider-group';
import type { Model } from '../../../api/client';
import type { ProviderGroup as ProviderGroupData } from '../../../lib/models-grouping';

function model(overrides: Partial<Model> = {}): Model {
  return {
    id: 'm1',
    provider: 'anthropic',
    model_id: 'claude-sonnet-5',
    model_family: 'claude',
    display_name: 'Claude Sonnet 5',
    is_active: true,
    ...overrides,
  } as Model;
}

function group(over: Partial<ProviderGroupData> = {}): ProviderGroupData {
  return {
    provider: 'anthropic',
    hasCredential: true,
    credentialUpdatedAt: null,
    models: [model()],
    ...over,
  };
}

function renderGroup(g: ProviderGroupData) {
  const onConnect = vi.fn();
  render(<ProviderGroup group={g} onConnect={onConnect} />);
  return { onConnect };
}

afterEach(cleanup);

describe('ProviderGroup', () => {
  it('credentialed group: "credential set" pill, no Connect, model "ready"', () => {
    renderGroup(group({ hasCredential: true }));
    expect(screen.getByText('credential set')).toBeTruthy();
    expect(screen.queryByText('Connect')).toBeNull();
    expect(screen.getByText('ready')).toBeTruthy();
    expect(document.querySelector('.model-row')?.className).not.toContain('dim');
  });

  it('uncredentialed group: "no credential" + Connect, model "needs credential" + dim', () => {
    const { onConnect } = renderGroup(group({ hasCredential: false }));
    expect(screen.getByText('no credential')).toBeTruthy();
    expect(screen.getByText('needs credential')).toBeTruthy();
    expect(document.querySelector('.model-row')?.className).toContain('dim');
    fireEvent.click(screen.getByText('Connect'));
    expect(onConnect).toHaveBeenCalledWith('anthropic');
  });

  it('inactive model: "inactive" pill + dim even when the provider is credentialed', () => {
    renderGroup(group({ hasCredential: true, models: [model({ is_active: false })] }));
    expect(screen.getByText('inactive')).toBeTruthy();
    expect(screen.queryByText('ready')).toBeNull();
    expect(document.querySelector('.model-row')?.className).toContain('dim');
  });

  it('renders the monospace model_id · family sub-line', () => {
    renderGroup(group());
    expect(screen.getByText('claude-sonnet-5 · claude')).toBeTruthy();
  });
});
