// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest';

import './combobox.js';
import type { Combobox } from './combobox.js';

async function mount(options: string[], value = ''): Promise<Combobox> {
  const el = document.createElement('rm-combobox') as Combobox;
  el.options = options;
  el.value = value;
  el.testid = 'cb';
  document.body.appendChild(el);
  await el.updateComplete;
  return el;
}

const input = (el: Combobox) => el.querySelector('input')!;
const menu = (el: Combobox): string[] =>
  Array.from(el.querySelectorAll('[data-combobox-option]')).map(
    (n) => n.getAttribute('data-combobox-option') ?? '',
  );

function type(el: Combobox, value: string): void {
  const i = input(el);
  i.value = value;
  i.dispatchEvent(new Event('input'));
}

describe('rm-combobox', () => {
  afterEach(() => {
    document.body.innerHTML = '';
  });

  it('opens on focus and lists all options', async () => {
    const el = await mount(['stripe', 'mock-fs']);
    input(el).dispatchEvent(new Event('focus'));
    await el.updateComplete;
    expect(menu(el).sort()).toEqual(['mock-fs', 'stripe']);
  });

  it('filters by case-insensitive substring as you type', async () => {
    const el = await mount(['stripe', 'mock-fs', 'amazon']);
    type(el, 'M'); // matches mock-fs and amazon (both contain 'm')
    await el.updateComplete;
    expect(menu(el)).toEqual(['mock-fs', 'amazon']);
  });

  it('typing emits change and never constrains the value (free text)', async () => {
    const el = await mount(['stripe']);
    let last = '';
    el.addEventListener('change', (e) => {
      last = (e as CustomEvent<{ value: string }>).detail.value;
    });
    type(el, 'server-not-in-the-list');
    await el.updateComplete;
    expect(last).toBe('server-not-in-the-list');
    expect(el.value).toBe('server-not-in-the-list');
  });

  it('picking a suggestion sets the value, emits change, and closes', async () => {
    const el = await mount(['stripe', 'mock-fs']);
    input(el).dispatchEvent(new Event('focus'));
    await el.updateComplete;
    let last = '';
    el.addEventListener('change', (e) => {
      last = (e as CustomEvent<{ value: string }>).detail.value;
    });
    const opt = el.querySelector<HTMLElement>('[data-combobox-option="mock-fs"]')!;
    // mousedown (not click) so it fires before input blur — the real flow.
    opt.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
    await el.updateComplete;
    expect(last).toBe('mock-fs');
    expect(el.value).toBe('mock-fs');
    expect(menu(el)).toEqual([]); // closed after pick
  });

  it('Escape closes the menu', async () => {
    const el = await mount(['stripe']);
    input(el).dispatchEvent(new Event('focus'));
    await el.updateComplete;
    expect(menu(el).length).toBe(1);
    input(el).dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    await el.updateComplete;
    expect(menu(el)).toEqual([]);
  });
});
