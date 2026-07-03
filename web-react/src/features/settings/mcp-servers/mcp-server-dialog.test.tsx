// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MCPServerDialog } from './mcp-server-dialog';
import { authModeDescription } from './auth-modes';

function renderDialog() {
  const onSaved = vi.fn();
  const onClose = vi.fn();
  render(<MCPServerDialog editing={null} onClose={onClose} onSaved={onSaved} />);
  return { onSaved, onClose };
}

afterEach(cleanup);

describe('MCPServerDialog', () => {
  it('gates Create on name AND url (defaults cover type/auth_mode)', () => {
    renderDialog();
    const create = screen.getByText('Create') as HTMLButtonElement;
    expect(create.disabled).toBe(true); // empty name + url

    fireEvent.change(screen.getByPlaceholderText('e.g. records-mcp'), {
      target: { value: 'records' },
    });
    expect(create.disabled).toBe(true); // url still empty

    fireEvent.change(
      screen.getByPlaceholderText('http://records-mcp.rolemesh-system.svc:8080/mcp'),
      { target: { value: 'http://records.internal/mcp' } },
    );
    expect(create.disabled).toBe(false);
  });

  it('auth-mode hint updates with the selected mode', () => {
    renderDialog();
    // default is service
    expect(screen.getByText(authModeDescription('service'))).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Auth mode'), {
      target: { value: 'both' },
    });
    expect(screen.getByText(authModeDescription('both'))).toBeTruthy();
  });

  it('transport option-cards toggle the selected class', () => {
    renderDialog();
    const sse = screen.getByText('sse').closest('button')!;
    const http = screen.getByText('http').closest('button')!;
    expect(http.className).toContain('selected'); // default http
    expect(sse.className).not.toContain('selected');
    fireEvent.click(sse);
    expect(sse.className).toContain('selected');
    expect(http.className).not.toContain('selected');
  });
});
