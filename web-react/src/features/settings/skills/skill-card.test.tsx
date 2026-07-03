// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { SkillCard } from './skill-card';
import { setMe } from '../../../lib/capabilities';
import type { Me, SkillSummary } from '../../../api/client';

function me(caps: string[], userId = 'u1'): Me {
  return { user_id: userId, tenant_id: 't1', role: 'member', plane: 'tenant', capabilities: caps };
}
function skill(overrides: Partial<SkillSummary> = {}): SkillSummary {
  return {
    id: 's1',
    tenant_id: 't1',
    name: 'pdf-toolkit',
    description: 'Handles PDFs',
    enabled: true,
    bound_coworker_count: 0,
    visibility: 'private',
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    created_by_user_id: 'other',
    ...overrides,
  } as SkillSummary;
}
function renderCard(s: SkillSummary, over: Partial<Parameters<typeof SkillCard>[0]> = {}) {
  return render(
    <SkillCard
      skill={s}
      shareBusy={false}
      deleteError={null}
      shareError={null}
      onOpen={vi.fn()}
      onToggleShare={vi.fn()}
      onEdit={vi.fn()}
      onDelete={vi.fn()}
      {...over}
    />,
  );
}

afterEach(() => {
  cleanup();
  setMe(null);
});

describe('SkillCard', () => {
  it('shows the bound count from the payload (no fan-out), singular at 1', () => {
    setMe(me([]));
    renderCard(skill({ bound_coworker_count: 1 }));
    const usage = screen.getByText('Bound to 1 coworker');
    expect(usage.className).toContain('bound');
  });

  it('shows unbound text when count is 0', () => {
    setMe(me([]));
    renderCard(skill({ bound_coworker_count: 0 }));
    expect(screen.getByText('Not bound to any coworker').className).not.toContain('bound');
  });

  it('renders the disabled pill only when enabled === false; no "enabled" pill', () => {
    setMe(me([]));
    const { rerender } = renderCard(skill({ enabled: true }));
    expect(screen.queryByText('disabled')).toBeNull();
    rerender(
      <SkillCard
        skill={skill({ enabled: false })}
        shareBusy={false}
        deleteError={null}
        shareError={null}
        onOpen={vi.fn()}
        onToggleShare={vi.fn()}
        onEdit={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    expect(screen.getByText('disabled')).toBeTruthy();
  });

  it('member without skill.manage sees VIEW ONLY on others’ rows', () => {
    setMe(me(['skill.use'], 'u1'));
    renderCard(skill({ created_by_user_id: 'other' }));
    expect(screen.getByText('View only')).toBeTruthy();
    expect(screen.queryByTitle('Edit skill')).toBeNull();
  });

  it('ownership escape: manage icons on own rows even without the capability', () => {
    setMe(me(['skill.use'], 'u1'));
    renderCard(skill({ created_by_user_id: 'u1' }));
    expect(screen.getByTitle('Edit skill')).toBeTruthy();
    expect(screen.getByTitle('Delete skill')).toBeTruthy();
  });

  it('renders both error slots and share aria-pressed', () => {
    setMe(me(['skill.manage']));
    renderCard(skill({ visibility: 'shared' }), {
      deleteError: 'del boom',
      shareError: 'share boom',
    });
    expect(screen.getByTitle('Make private').getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByText('del boom')).toBeTruthy();
    expect(screen.getByText('share boom')).toBeTruthy();
  });

  it('edit/delete icons stopPropagation (do not trigger row open)', () => {
    setMe(me(['skill.manage']));
    const onOpen = vi.fn();
    const onEdit = vi.fn();
    renderCard(skill({ created_by_user_id: 'u1' }), { onOpen, onEdit });
    fireEvent.click(screen.getByTitle('Edit skill'));
    expect(onEdit).toHaveBeenCalledTimes(1);
    expect(onOpen).not.toHaveBeenCalled();
  });
});
