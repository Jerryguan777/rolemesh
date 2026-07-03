// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppearancePage } from './appearance-page';

type ChangeCb = (e: { matches: boolean }) => void;
let listeners: ChangeCb[] = [];
let systemDark = false;

beforeEach(() => {
  listeners = [];
  systemDark = false;
  vi.stubGlobal('matchMedia', (media: string) => ({
    media,
    get matches() {
      return systemDark;
    },
    addEventListener: (_: string, cb: ChangeCb) => listeners.push(cb),
    removeEventListener: (_: string, cb: ChangeCb) => {
      listeners = listeners.filter((l) => l !== cb);
    },
  }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderPage() {
  return render(
    <MemoryRouter>
      <AppearancePage />
    </MemoryRouter>,
  );
}

describe('AppearancePage', () => {
  it('renders the D-N1 copy and the detected light theme', () => {
    renderPage();
    expect(
      screen.getByText(/ships the light palette today — there is no in-app toggle/),
    ).toBeTruthy();
    expect(screen.getByText('System: Light')).toBeTruthy();
    expect(screen.getByText(/nothing is stored/)).toBeTruthy();
  });

  it('smoke: read-only means zero controls (no inputs/selects, no buttons beyond back-link)', () => {
    const { container } = renderPage();
    expect(container.querySelectorAll('input, select, textarea, [role="switch"]').length).toBe(0);
    const buttons = [...container.querySelectorAll('button')];
    expect(buttons.length).toBe(1);
    expect(buttons[0].className).toContain('back-link');
  });

  it('flips to Dark live when the media query fires mid-session', () => {
    renderPage();
    expect(screen.getByText('System: Light')).toBeTruthy();
    act(() => {
      systemDark = true;
      listeners.forEach((cb) => cb({ matches: true }));
    });
    expect(screen.getByText('System: Dark')).toBeTruthy();
    expect(screen.queryByText('System: Light')).toBeNull();
  });

  it('detects dark on mount and removes the listener on unmount', () => {
    systemDark = true;
    const { unmount } = renderPage();
    expect(screen.getByText('System: Dark')).toBeTruthy();
    expect(listeners.length).toBe(1);
    unmount();
    expect(listeners.length).toBe(0);
  });
});
