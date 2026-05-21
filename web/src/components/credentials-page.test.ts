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
    // Force a refresh.
    await (page as unknown as { refresh: () => Promise<void> }).refresh();
    await page.updateComplete;

    // Render should NOT contain the literal plaintext anywhere.
    // We use a sentinel that nothing in the component template
    // would legitimately produce.
    const sentinel = 'sk-leaky-sentinel-1234';
    expect(page.innerHTML).not.toContain(sentinel);

    // The password input must not have its value reset to the key —
    // the placeholder should hint "Enter a new key to rotate" rather
    // than leaking length information.
    const inputs = page.querySelectorAll('input[type="password"]');
    expect(inputs.length).toBeGreaterThan(0);
    for (const i of inputs) {
      const inp = i as HTMLInputElement;
      expect(inp.value).toBe('');
      const ph = inp.getAttribute('placeholder') ?? '';
      // The placeholder must not contain digits the user might
      // mistake for a real-length hint (e.g. "20 chars stored").
      expect(/\d/.test(ph)).toBe(false);
    }
  });

  it('clears the draft after a successful PUT', async () => {
    // No existing rows on the list response.
    listSpy.mockResolvedValue([]);
    putSpy.mockResolvedValue({
      provider: 'anthropic',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });

    // Find the input for ``anthropic`` and inject a draft via the
    // public state hook so we exercise the same store the component
    // updates.
    const inputs = page.querySelectorAll('input[type="password"]');
    assertElement(inputs[0] as HTMLElement);
    const target = inputs[0] as HTMLInputElement;
    target.value = 'sk-ant-test-1234';
    target.dispatchEvent(new Event('input'));
    await page.updateComplete;

    // Click Save.
    const saveBtn = Array.from(page.querySelectorAll('button')).find(
      (b) => b.textContent?.trim() === 'Save',
    );
    expect(saveBtn).toBeTruthy();
    saveBtn!.click();
    // Two microtask flushes: one for the await chain inside save,
    // then refresh+render.
    await Promise.resolve();
    await Promise.resolve();
    await page.updateComplete;

    // The PUT must have received the plaintext exactly once.
    expect(putSpy).toHaveBeenCalledWith('anthropic', {
      api_key: 'sk-ant-test-1234',
    });
    // After save, the draft must be cleared in DOM — no recoverable
    // plaintext lingering in the input's ``value`` attribute.
    const updatedInputs = page.querySelectorAll(
      'input[type="password"]',
    ) as NodeListOf<HTMLInputElement>;
    for (const i of updatedInputs) {
      expect(i.value).toBe('');
    }
  });
});
