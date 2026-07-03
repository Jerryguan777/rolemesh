// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { ConfirmDialog } from './confirm-dialog';

function renderDialog(busy = false) {
  const onConfirm = vi.fn();
  const onCancel = vi.fn();
  render(
    <ConfirmDialog
      title="Delete coworker “Mira”?"
      confirmLabel="Delete"
      busyLabel="Deleting…"
      busy={busy}
      onConfirm={onConfirm}
      onCancel={onCancel}
    >
      body copy
    </ConfirmDialog>,
  );
  return { onConfirm, onCancel };
}

afterEach(cleanup);

describe('ConfirmDialog', () => {
  it('renders an alertdialog with title, body, and both actions', () => {
    renderDialog();
    expect(screen.getByRole('alertdialog')).toBeTruthy();
    expect(screen.getByText('body copy')).toBeTruthy();
    expect(screen.getByText('Cancel')).toBeTruthy();
    expect(screen.getByText('Delete')).toBeTruthy();
  });

  it('confirm fires onConfirm; cancel and ESC fire onCancel', () => {
    const { onConfirm, onCancel } = renderDialog();
    fireEvent.click(screen.getByText('Delete'));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByText('Cancel'));
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onCancel).toHaveBeenCalledTimes(2);
  });

  it('busy state shows the busy label and makes every dismiss path inert', () => {
    const { onConfirm, onCancel } = renderDialog(true);
    const confirm = screen.getByText('Deleting…') as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    fireEvent.click(confirm); // double-submit guard
    expect(onConfirm).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText('Cancel'));
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onCancel).not.toHaveBeenCalled();
  });
});
