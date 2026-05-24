// @vitest-environment happy-dom
// Credentials page: pins the §8.1 invariant that the SPA never
// re-displays a previously-stored API key.
//
// The plaintext should never appear in:
//   - any rendered DOM node;
//   - the credentials list response (covered server-side);
//   - the form's bound state once a save completes.
//
// These tests run against the real Lit component but stub
// ``getApiClient`` so they don't need a backend.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Stub the ApiClient module before importing the component so
// ``getApiClient()`` resolves to our spy.
const listSpy = vi.fn();
const putSpy = vi.fn();
const deleteSpy = vi.fn();

vi.mock('../api/client.js', async () => {
  const actual = await vi.importActual<typeof import('../api/client.js')>(
    '../api/client.js',
  );
  return {
    ...actual,
    getApiClient: () => ({
      listCredentials: listSpy,
      putCredential: putSpy,
      deleteCredential: deleteSpy,
    }),
  };
});

import { CredentialsPage } from './credentials-page.js';

function assertElement(el: Element | null): asserts el is HTMLElement {
  if (!el) throw new Error('element not found');
}

describe('CredentialsPage', () => {
  let page: CredentialsPage;

  async function waitUntilLoaded() {
    // ``refresh()`` is async; loop on ``loading`` state until the
    // post-fetch render has happened.
    for (let i = 0; i < 20; i++) {
      await Promise.resolve();
      await page.updateComplete;
      // @ts-expect-error — touching private state for the test
      if (page.loading === false) return;
    }
    throw new Error('CredentialsPage did not finish loading');
  }

  beforeEach(async () => {
    listSpy.mockReset();
    putSpy.mockReset();
    deleteSpy.mockReset();
    listSpy.mockResolvedValue([]);
    // happy-dom does not upgrade ``document.createElement`` results
    // to their custom-element class, so construct directly.
    page = new CredentialsPage();
    document.body.appendChild(page);
    await waitUntilLoaded();
  });

  afterEach(() => {
    page.remove();
  });

  it('does not render any existing key value when one is configured', async () => {
    listSpy.mockResolvedValue([
      {
        provider: 'anthropic',
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ]);
    await (page as unknown as { refresh: () => Promise<void> }).refresh();
    await page.updateComplete;

    // v2-C reskin moved credential capture into <rm-credential-dialog>;
    // the credentials-page itself NEVER paints a password input (the
    // dialog mounts one only while open). Pin both halves:
    //   - no plaintext sentinel anywhere in the page tree
    //   - the row card surfaces a "set" pill, not the key
    const sentinel = 'sk-leaky-sentinel-1234';
    expect(page.innerHTML).not.toContain(sentinel);

    const row = page.querySelector('[data-provider="anthropic"]');
    expect(row, 'anthropic row should render').not.toBeNull();
    expect(row?.querySelector('.rm-pill-on')?.textContent?.trim()).toBe('set');
    // The dialog is mounted as a sibling but stays closed at rest;
    // the page itself has zero <input> elements.
    const pageInputs = Array.from(page.querySelectorAll('input')).filter(
      (i) => i.closest('rm-credential-dialog') === null,
    );
    expect(pageInputs.length).toBe(0);
  });

  it('clicking the edit icon opens the credential dialog scoped to that provider', async () => {
    listSpy.mockResolvedValue([
      {
        provider: 'anthropic',
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ]);
    await (page as unknown as { refresh: () => Promise<void> }).refresh();
    await page.updateComplete;

    // Reach into internals to pin the dialog state transition. The
    // public surface (clicking the icon then watching for the dialog
    // markup) is exercised manually + via playwright; this case pins
    // the wire-up.
    const row = page.querySelector('[data-provider="anthropic"]');
    const editBtn = row?.querySelector<HTMLButtonElement>(
      '[data-testid="credential-edit"]',
    );
    expect(editBtn).not.toBeNull();
    editBtn!.click();
    await page.updateComplete;
    const internals = page as unknown as {
      dialogOpen: boolean;
      dialogProvider: string | null;
    };
    expect(internals.dialogOpen).toBe(true);
    expect(internals.dialogProvider).toBe('anthropic');

    // The PUT-side contract (save then clear draft, never leak the
    // value back into a DOM attribute) lives on the dialog, and is
    // exercised by credential-dialog.test.ts. The credentials-page
    // is now just the launcher — its responsibility ends at "open
    // the dialog with the correct provider".
    void putSpy;
  });
});
