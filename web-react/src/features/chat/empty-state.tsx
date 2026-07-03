// EmptyState — 128px brand mark + copy + optional CTA (spec §6.1).

import { BrandMark } from '../../components/brand-mark';

export function EmptyState({
  info,
  cta,
}: {
  info: string;
  cta?: { label: string; onClick: () => void };
}) {
  return (
    <div className="empty-state">
      <BrandMark size={128} />
      <div className="info">{info}</div>
      {cta ? (
        <button className="btn-primary" onClick={cta.onClick}>
          {cta.label}
        </button>
      ) : null}
    </div>
  );
}
