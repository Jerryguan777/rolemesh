// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import './approval-card.js';
import type { ApprovalCard } from './approval-card.js';
import type { ApprovalDecisionDetail } from './approval-card.js';

async function mount(props: Partial<ApprovalCard> = {}): Promise<ApprovalCard> {
  const el = document.createElement('rm-approval-card') as ApprovalCard;
  Object.assign(el, { requestId: 'req-1', status: 'pending', ...props });
  document.body.appendChild(el);
  await el.updateComplete;
  return el;
}

function $<T extends Element>(el: Element, sel: string): T | null {
  return el.querySelector<T>(sel);
}

function $$(el: Element, sel: string): Element[] {
  return Array.from(el.querySelectorAll(sel));
}

function decisions(el: ApprovalCard): ReturnType<typeof vi.fn> {
  const fn = vi.fn();
  el.addEventListener('approval-decision', (e) =>
    fn((e as CustomEvent<ApprovalDecisionDetail>).detail),
  );
  return fn;
}

describe('<rm-approval-card>', () => {
  let el: ApprovalCard | null = null;
  beforeEach(() => {
    el = null;
  });
  afterEach(() => {
    el?.remove();
    vi.useRealTimers();
  });

  it('renders the tool chip and both action buttons when pending', async () => {
    el = await mount({ mcpServerName: 'stripe', toolName: 'charge' });
    expect($(el, '[data-testid="approval-tool"]')?.textContent).toContain(
      'charge',
    );
    expect($(el, '[data-testid="approval-approve"]')).not.toBeNull();
    expect($(el, '[data-testid="approval-reject"]')).not.toBeNull();
  });

  // --- new field rendering ---------------------------------------------------

  it('renders the tool chip from server + tool name', async () => {
    el = await mount({ mcpServerName: 'amazon-ads-api', toolName: 'campaign.pause' });
    expect($(el, '[data-testid="approval-tool"]')?.textContent).toContain(
      'amazon-ads-api · campaign.pause',
    );
  });

  it('renders one params row per entry, with strings quoted and numbers bare', async () => {
    el = await mount({
      params: { campaign_id: 'SP-Auto', amount: 3210, dry_run: false },
    });
    const rows = $$(el, '[data-testid="approval-param-row"]');
    expect(rows).toHaveLength(3);
    const values = $$(el, '[data-testid="approval-param-value"]').map(
      (v) => v.textContent?.trim(),
    );
    expect(values).toContain('"SP-Auto"');
    expect(values).toContain('3210');
    expect(values).toContain('false');
  });

  it('omits the params block entirely when params is empty', async () => {
    el = await mount({ params: {} });
    expect($(el, '[data-testid="approval-params"]')).toBeNull();
  });

  it('collapses params behind a disclosure when over the threshold', async () => {
    const params = Object.fromEntries(
      Array.from({ length: 10 }, (_, i) => [`k${i}`, i]),
    );
    el = await mount({ params });
    // Only the first 6 shown initially, plus a toggle.
    expect($$(el, '[data-testid="approval-param-row"]')).toHaveLength(6);
    const toggle = $<HTMLButtonElement>(el, '[data-testid="approval-params-toggle"]');
    expect(toggle?.textContent).toContain('Show all 10');
    toggle!.click();
    await el.updateComplete;
    expect($$(el, '[data-testid="approval-param-row"]')).toHaveLength(10);
  });

  it('renders the rationale block when present', async () => {
    el = await mount({ rationale: 'Lowest ROAS campaign; frees $3,210/mo.' });
    expect($(el, '[data-testid="approval-rationale"]')?.textContent).toContain(
      'Lowest ROAS campaign',
    );
  });

  it('omits the rationale block when null (absence is meaningful, §3.4)', async () => {
    el = await mount({ rationale: null });
    expect($(el, '[data-testid="approval-rationale"]')).toBeNull();
  });

  it('omits the rationale block when blank whitespace', async () => {
    el = await mount({ rationale: '   ' });
    expect($(el, '[data-testid="approval-rationale"]')).toBeNull();
  });

  // --- countdown (§3.2) ------------------------------------------------------

  it('shows minutes-left and is NOT urgent above 5 minutes', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-05-31T12:00:00Z'));
    const expiresAt = new Date(Date.now() + 6 * 60_000).toISOString();
    el = await mount({ expiresAt });
    const cd = $(el, '[data-testid="approval-countdown"]');
    expect(cd?.textContent).toContain('6m left');
    expect(cd?.getAttribute('data-urgent')).toBe('false');
  });

  it('marks the countdown urgent under the 5-minute threshold', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-05-31T12:00:00Z'));
    const expiresAt = new Date(Date.now() + 4 * 60_000 + 59_000).toISOString();
    el = await mount({ expiresAt });
    const cd = $(el, '[data-testid="approval-countdown"]');
    expect(cd?.textContent).toContain('4m left');
    expect(cd?.getAttribute('data-urgent')).toBe('true');
  });

  it('renders seconds + urgent under one minute, and "expired" past zero', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-05-31T12:00:00Z'));
    el = await mount({
      expiresAt: new Date(Date.now() + 30_000).toISOString(),
    });
    let cd = $(el, '[data-testid="approval-countdown"]');
    expect(cd?.textContent).toContain('30s left');
    expect(cd?.getAttribute('data-urgent')).toBe('true');

    // Advance past expiry; the 1Hz tick should flip it to "expired" — and the
    // buttons must stay live (the SPA never self-resolves, §3.2).
    vi.setSystemTime(new Date('2026-05-31T12:01:00Z'));
    await vi.advanceTimersByTimeAsync(1000);
    await el.updateComplete;
    cd = $(el, '[data-testid="approval-countdown"]');
    expect(cd?.textContent).toContain('expired');
    expect($(el, '[data-testid="approval-approve"]')).not.toBeNull();
  });

  // --- approve path ----------------------------------------------------------

  it('emits approve immediately with no note on the approve button', async () => {
    el = await mount({ requestId: 'abc' });
    const onDecision = decisions(el);
    $<HTMLButtonElement>(el, '[data-testid="approval-approve"]')!.click();
    expect(onDecision).toHaveBeenCalledTimes(1);
    expect(onDecision).toHaveBeenCalledWith({
      requestId: 'abc',
      decision: 'approve',
    });
  });

  // --- reject-with-note path (§3.5) -----------------------------------------

  it('a single Reject click opens the note form and does NOT emit', async () => {
    el = await mount({ requestId: 'abc' });
    const onDecision = decisions(el);
    $<HTMLButtonElement>(el, '[data-testid="approval-reject"]')!.click();
    await el.updateComplete;
    expect($(el, '[data-testid="approval-reject-form"]')).not.toBeNull();
    expect($(el, '[data-testid="approval-note"]')).not.toBeNull();
    expect(onDecision).not.toHaveBeenCalled();
  });

  it('the form Reject submits the trimmed note', async () => {
    el = await mount({ requestId: 'abc' });
    const onDecision = decisions(el);
    $<HTMLButtonElement>(el, '[data-testid="approval-reject"]')!.click();
    await el.updateComplete;
    const ta = $<HTMLTextAreaElement>(el, '[data-testid="approval-note"]')!;
    ta.value = '  amount too high — needs signoff  ';
    $<HTMLButtonElement>(el, '[data-testid="approval-reject-confirm"]')!.click();
    expect(onDecision).toHaveBeenCalledWith({
      requestId: 'abc',
      decision: 'reject',
      note: 'amount too high — needs signoff',
    });
  });

  it('an empty note submits a reject with no note field (nullable, §3.5)', async () => {
    el = await mount({ requestId: 'abc' });
    const onDecision = decisions(el);
    $<HTMLButtonElement>(el, '[data-testid="approval-reject"]')!.click();
    await el.updateComplete;
    $<HTMLButtonElement>(el, '[data-testid="approval-reject-confirm"]')!.click();
    expect(onDecision).toHaveBeenCalledWith({
      requestId: 'abc',
      decision: 'reject',
    });
  });

  it('Cancel collapses the form without emitting', async () => {
    el = await mount({ requestId: 'abc' });
    const onDecision = decisions(el);
    $<HTMLButtonElement>(el, '[data-testid="approval-reject"]')!.click();
    await el.updateComplete;
    $<HTMLButtonElement>(el, '[data-testid="approval-reject-cancel"]')!.click();
    await el.updateComplete;
    expect($(el, '[data-testid="approval-reject-form"]')).toBeNull();
    expect($(el, '[data-testid="approval-reject"]')).not.toBeNull();
    expect(onDecision).not.toHaveBeenCalled();
  });

  // --- busy guard ------------------------------------------------------------

  it('does not open the reject form while busy', async () => {
    el = await mount({ busy: true });
    $<HTMLButtonElement>(el, '[data-testid="approval-reject"]')?.click();
    await el.updateComplete;
    expect($(el, '[data-testid="approval-reject-form"]')).toBeNull();
  });

  it('does not emit approve while busy (double-tap guard)', async () => {
    el = await mount({ busy: true });
    const onDecision = decisions(el);
    $<HTMLButtonElement>(el, '[data-testid="approval-approve"]')?.click();
    expect(onDecision).not.toHaveBeenCalled();
  });

  // --- resolved states (§3.6) ------------------------------------------------

  it.each([
    ['approved', 'Approved'],
    ['rejected', 'Rejected'],
    ['expired', 'Timed out'],
    ['cancelled', 'Cancelled'],
  ] as const)('shows the %s pill and hides the buttons', async (status, label) => {
    el = await mount({ status });
    expect($(el, '[data-testid="approval-approve"]')).toBeNull();
    expect($(el, '[data-testid="approval-reject"]')).toBeNull();
    expect($(el, '[data-testid="approval-status"]')?.textContent).toContain(label);
  });

  it('shows the resolution timestamp from resolvedAt', async () => {
    const resolvedAt = Date.parse('2026-05-31T14:34:00Z');
    el = await mount({ status: 'approved', resolvedAt });
    const ts = $(el, '[data-testid="approval-resolved-time"]');
    expect(ts?.textContent).toContain('at');
    // The exact wall-clock string depends on TZ; assert it carries the rendered
    // local time of resolvedAt rather than hardcoding a zone.
    expect(ts?.textContent).toContain(
      new Date(resolvedAt).toLocaleTimeString([], {
        hour: 'numeric',
        minute: '2-digit',
      }),
    );
  });

  it('echoes the rejection note back as "Your reason" on a rejected card', async () => {
    el = await mount({ status: 'rejected', note: 'over budget' });
    const block = $(el, '[data-testid="approval-resolved-note"]');
    expect(block?.textContent).toContain('Your reason');
    expect(block?.textContent).toContain('over budget');
  });

  it('shows no resolved-note when a rejection carried no note', async () => {
    el = await mount({ status: 'rejected', note: null });
    expect($(el, '[data-testid="approval-resolved-note"]')).toBeNull();
  });

  it('shows no resolved-note on a non-reject outcome even if a note exists', async () => {
    el = await mount({ status: 'approved', note: 'stray' });
    expect($(el, '[data-testid="approval-resolved-note"]')).toBeNull();
  });

  it('does not render decision buttons once resolved (no late tap)', async () => {
    el = await mount({ status: 'rejected' });
    const onDecision = decisions(el);
    expect($(el, '[data-testid="approval-approve"]')).toBeNull();
    expect(onDecision).not.toHaveBeenCalled();
  });
});

describe('<rm-approval-card> safety-triggered banner (§3.10)', () => {
  let el: ApprovalCard;
  afterEach(() => {
    el?.remove();
  });

  it('renders an amber banner with the check label when triggered by a safety rule', async () => {
    el = await mount({
      mcpServerName: 'stripe',
      toolName: 'refund.create',
      triggeredBy: {
        kind: 'safety_rule',
        rule_id: 'sr-1',
        check_id: 'presidio.pii',
        stage: 'post_tool_result',
      },
    });
    const banner = $(el, '[data-testid="approval-safety-banner"]');
    expect(banner).not.toBeNull();
    // Human label, never the raw id; stage intentionally omitted.
    expect(banner?.textContent).toContain('Personal data (Presidio)');
    expect(banner?.textContent).not.toContain('post_tool_result');
    expect($(el, '[data-testid="approval-safety-link"]')).not.toBeNull();
  });

  it('renders no banner for a business-policy approval (triggeredBy null)', async () => {
    el = await mount({ mcpServerName: 'stripe', toolName: 'refund.create', triggeredBy: null });
    expect($(el, '[data-testid="approval-safety-banner"]')).toBeNull();
  });

  it('degrades to no banner on an unknown triggered_by kind (forward-compat)', async () => {
    el = await mount({
      mcpServerName: 'stripe',
      toolName: 'refund.create',
      // A future kind the SPA doesn't know about must not crash or render.
      triggeredBy: { kind: 'scheduled_task', rule_id: 'x', check_id: 'y', stage: 'input_prompt' } as never,
    });
    expect($(el, '[data-testid="approval-safety-banner"]')).toBeNull();
  });

  it('jumps to the settings safety log when "view in safety log" is clicked', async () => {
    el = await mount({
      mcpServerName: 'stripe',
      toolName: 'refund.create',
      triggeredBy: {
        kind: 'safety_rule',
        rule_id: 'sr-1',
        check_id: 'presidio.pii',
        stage: 'post_tool_result',
      },
    });
    location.hash = '#/';
    ($(el, '[data-testid="approval-safety-link"]') as HTMLButtonElement).click();
    expect(location.hash).toBe('#/manage/safety-log');
  });
});
