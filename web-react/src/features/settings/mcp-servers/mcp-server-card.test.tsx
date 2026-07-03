// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { MCPServerCard } from './mcp-server-card';
import type { MCPServer } from '../../../api/client';

function server(overrides: Partial<MCPServer> = {}): MCPServer {
  return {
    id: 'mcp1',
    tenant_id: 't1',
    name: 'records',
    type: 'http',
    url: 'http://records.internal/mcp',
    auth_mode: 'service',
    description: 'Records MCP server',
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    ...overrides,
  } as MCPServer;
}

function renderCard(s: MCPServer, usageCount: number) {
  return render(
    <MCPServerCard
      server={s}
      usageCount={usageCount}
      rowError={null}
      onEdit={vi.fn()}
      onDelete={vi.fn()}
    />,
  );
}

afterEach(cleanup);

describe('MCPServerCard', () => {
  it('renders the provider line, monospace url, and description', () => {
    renderCard(server(), 0);
    expect(screen.getByText('http · auth: service')).toBeTruthy();
    expect(screen.getByText('http://records.internal/mcp').className).toContain('mono');
    expect(screen.getByText('Records MCP server')).toBeTruthy();
  });

  it('shows the bound usage text (info-blue) when N>0', () => {
    renderCard(server(), 3);
    const usage = screen.getByText('Used by 3 coworkers');
    expect(usage.className).toContain('bound');
  });

  it('shows the unbound text (muted) when N=0', () => {
    renderCard(server(), 0);
    const usage = screen.getByText('Not bound to any coworker');
    expect(usage.className).not.toContain('bound');
  });

  it('singularizes "coworker" at N=1', () => {
    renderCard(server(), 1);
    expect(screen.getByText('Used by 1 coworker')).toBeTruthy();
  });

  it('has no visibility/ownership pills (tenant infrastructure)', () => {
    renderCard(server(), 0);
    expect(document.querySelector('.pills')).toBeNull();
    expect(screen.queryByText('View only')).toBeNull();
  });
});
