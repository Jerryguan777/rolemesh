// The two-diamond brand-mark placeholder (spec D-3) — swap-point for
// the final RoleMesh logo asset. Sized by the parent via `size`.

export function BrandMark({ size }: { size: number | string }) {
  return (
    <svg
      viewBox="0 0 28 28"
      role="img"
      aria-label="RoleMesh"
      width={size}
      height={size}
    >
      <rect
        x="14"
        y="2.5"
        width="10"
        height="19"
        rx="0.6"
        fill="var(--rm-mark-primary)"
        transform="rotate(45 19 12)"
      />
      <rect
        x="3.2"
        y="12.2"
        width="8.2"
        height="11"
        rx="0.6"
        fill="var(--rm-mark-secondary)"
        fillOpacity="0.8"
        transform="rotate(-45 7.3 17.7)"
      />
    </svg>
  );
}
