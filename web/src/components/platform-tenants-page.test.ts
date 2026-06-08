// @vitest-environment happy-dom
// <rm-platform-tenants-page> — pins the list / suspend / resume /
// provision wiring (RBAC UI spec §4). We stub ONLY the api client
// boundary (the four platform-tenant methods) and run the real Lit
// component against it.
//
// Behaviours pinned:
//   - the list renders one row per PlatformTenantResponse
//   - an ACTIVE row shows Suspend; clicking it calls suspendTenant(id)
//   - a SUSPENDED row shows Resume; clicking it calls resumeTenant(id)
//   - the provision dialog submit calls provisionTenant({name, slug?})
//     with the entered values
//   - a blank-name provision is rejected without an API call
//   - the empty + error states render

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const listSpy = vi.fn();
const provisionSpy = vi.fn();
const suspendSpy = vi.fn();
const resumeSpy = vi.fn();

vi.mock('../api/client.js', async () => {
  const actual = await vi.importActual<typeof import('../api/client.js')>(
    '../api/client.js',
  );
  return {
    ...actual,
    getApiClient: () => ({
      listTenants: listSpy,
      provisionTenant: provisionSpy,
      suspendTenant: suspendSpy,
      resumeTenant: resumeSpy,
    }),
  };
});

import { PlatformTenantsPage } from './platform-tenants-page.js';
import type { PlatformTenantResponse } from '../api/client.js';

function makeTenant(
  over: Partial<PlatformTenantResponse> = {},
): PlatformTenantResponse {
  return {
    id: 't-1',
    name: 'Acme Corp',
    slug: 'acme',
    plan: null,
    max_concurrent_containers: 5,
    status: 'active',
    created_at: new Date().toISOString(),
    ...over,
  };
}

describe('PlatformTenantsPage', () => {
  let page: PlatformTenantsPage;

  async function waitUntilLoaded() {
    for (let i = 0; i < 20; i++) {
      await Promise.resolve();
      await page.updateComplete;
      // @ts-expect-error — reading private state for the test
      if (page.loading === false) return;
    }
    throw new Error('PlatformTenantsPage did not finish loading');
  }

  async function mountWith(rows: PlatformTenantResponse[]) {
    listSpy.mockResolvedValue(rows);
    page = new PlatformTenantsPage();
    document.body.appendChild(page);
    await waitUntilLoaded();
  }

  beforeEach(() => {
    listSpy.mockReset();
    provisionSpy.mockReset();
    suspendSpy.mockReset();
    resumeSpy.mockReset();
    listSpy.mockResolvedValue([]);
    provisionSpy.mockResolvedValue(makeTenant());
    suspendSpy.mockResolvedValue(makeTenant({ status: 'suspended' }));
    resumeSpy.mockResolvedValue(makeTenant({ status: 'active' }));
  });

  afterEach(() => {
    page?.remove();
  });

  it('renders one row per tenant from listTenants()', async () => {
    await mountWith([
      makeTenant({ id: 't-1', name: 'Acme Corp' }),
      makeTenant({ id: 't-2', name: 'Globex', status: 'suspended' }),
    ]);
    const rows = page.querySelectorAll('[data-testid="tenant-row"]');
    expect(rows).toHaveLength(2);
    expect(page.textContent).toContain('Acme Corp');
    expect(page.textContent).toContain('Globex');
  });

  it('renders the empty state when there are no tenants', async () => {
    await mountWith([]);
    expect(page.querySelector('[data-testid="tenants-empty"]')).not.toBeNull();
    expect(page.querySelectorAll('[data-testid="tenant-row"]')).toHaveLength(0);
  });

  it('renders the error banner when listTenants() rejects', async () => {
    listSpy.mockRejectedValue(new Error('boom'));
    page = new PlatformTenantsPage();
    document.body.appendChild(page);
    await waitUntilLoaded();
    const banner = page.querySelector('[data-testid="tenants-error"]');
    expect(banner).not.toBeNull();
    expect(banner?.textContent).toContain('boom');
  });

  it('an active row shows Suspend and clicking it calls suspendTenant(id)', async () => {
    await mountWith([makeTenant({ id: 't-active', status: 'active' })]);
    const row = page.querySelector('[data-tenant-id="t-active"]');
    const btn = row?.querySelector<HTMLButtonElement>(
      '[data-testid="tenant-suspend"]',
    );
    expect(btn, 'active row must show a Suspend button').not.toBeNull();
    // The Resume affordance must NOT be present on an active row.
    expect(row?.querySelector('[data-testid="tenant-resume"]')).toBeNull();
    btn!.click();
    await page.updateComplete;
    expect(suspendSpy).toHaveBeenCalledTimes(1);
    expect(suspendSpy).toHaveBeenCalledWith('t-active');
    expect(resumeSpy).not.toHaveBeenCalled();
  });

  it('a suspended row shows Resume and clicking it calls resumeTenant(id)', async () => {
    await mountWith([makeTenant({ id: 't-susp', status: 'suspended' })]);
    const row = page.querySelector('[data-tenant-id="t-susp"]');
    const btn = row?.querySelector<HTMLButtonElement>(
      '[data-testid="tenant-resume"]',
    );
    expect(btn, 'suspended row must show a Resume button').not.toBeNull();
    expect(row?.querySelector('[data-testid="tenant-suspend"]')).toBeNull();
    btn!.click();
    await page.updateComplete;
    expect(resumeSpy).toHaveBeenCalledTimes(1);
    expect(resumeSpy).toHaveBeenCalledWith('t-susp');
    expect(suspendSpy).not.toHaveBeenCalled();
  });

  it('refreshes the list after a successful suspend', async () => {
    await mountWith([makeTenant({ id: 't-active', status: 'active' })]);
    expect(listSpy).toHaveBeenCalledTimes(1);
    const btn = page.querySelector<HTMLButtonElement>(
      '[data-testid="tenant-suspend"]',
    );
    btn!.click();
    // Let toggleStatus -> refresh() settle.
    for (let i = 0; i < 20; i++) {
      await Promise.resolve();
      await page.updateComplete;
    }
    // One initial load + one refresh after the suspend.
    expect(listSpy).toHaveBeenCalledTimes(2);
  });

  it('provision dialog submit calls provisionTenant with the entered name + slug', async () => {
    await mountWith([]);
    // Open the dialog.
    page
      .querySelector<HTMLButtonElement>('[data-testid="provision-tenant"]')!
      .click();
    await page.updateComplete;

    const nameInput = page.querySelector<HTMLInputElement>(
      '[data-testid="provision-name"]',
    );
    const slugInput = page.querySelector<HTMLInputElement>(
      '[data-testid="provision-slug"]',
    );
    expect(nameInput, 'name field must render').not.toBeNull();
    nameInput!.value = 'Initech';
    nameInput!.dispatchEvent(new Event('input'));
    slugInput!.value = 'initech';
    slugInput!.dispatchEvent(new Event('input'));
    await page.updateComplete;

    page
      .querySelector<HTMLButtonElement>('[data-testid="provision-submit"]')!
      .click();
    await page.updateComplete;

    expect(provisionSpy).toHaveBeenCalledTimes(1);
    expect(provisionSpy).toHaveBeenCalledWith({
      name: 'Initech',
      slug: 'initech',
    });
  });

  it('omits slug from the provision body when left blank (slug? is optional)', async () => {
    await mountWith([]);
    page
      .querySelector<HTMLButtonElement>('[data-testid="provision-tenant"]')!
      .click();
    await page.updateComplete;

    const nameInput = page.querySelector<HTMLInputElement>(
      '[data-testid="provision-name"]',
    );
    nameInput!.value = 'NoSlug Inc';
    nameInput!.dispatchEvent(new Event('input'));
    await page.updateComplete;

    page
      .querySelector<HTMLButtonElement>('[data-testid="provision-submit"]')!
      .click();
    await page.updateComplete;

    expect(provisionSpy).toHaveBeenCalledTimes(1);
    // No empty-string slug — the key is absent entirely.
    expect(provisionSpy).toHaveBeenCalledWith({ name: 'NoSlug Inc' });
    const arg = provisionSpy.mock.calls[0][0] as Record<string, unknown>;
    expect('slug' in arg).toBe(false);
  });

  it('rejects a blank-name provision without calling provisionTenant', async () => {
    await mountWith([]);
    page
      .querySelector<HTMLButtonElement>('[data-testid="provision-tenant"]')!
      .click();
    await page.updateComplete;

    // Submit with the name field still empty.
    page
      .querySelector<HTMLButtonElement>('[data-testid="provision-submit"]')!
      .click();
    await page.updateComplete;

    expect(provisionSpy).not.toHaveBeenCalled();
    expect(
      page.querySelector('[data-testid="provision-error"]')?.textContent,
    ).toContain('required');
  });
});
