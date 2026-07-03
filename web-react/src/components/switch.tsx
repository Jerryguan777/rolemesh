// Switch — the labelled toggle primitive (prototype `.switch` family).
// New to components/ with Part H: its first two consumers are the
// policy card's always-visible enable toggle and the policy dialog's
// Status field (≥2-consumers admission rule). Styles live in ui.css.
//
// Renders `role="switch"` with the on/off word as its visible label —
// the label IS part of the control (prototype anatomy), not a separate
// element.

export function Switch({
  on,
  disabled = false,
  onToggle,
  onLabel = 'Enabled',
  offLabel = 'Disabled',
  title,
}: {
  on: boolean;
  disabled?: boolean;
  onToggle: () => void;
  onLabel?: string;
  offLabel?: string;
  title?: string;
}) {
  return (
    <button
      type="button"
      className={`switch${on ? ' on' : ''}`}
      role="switch"
      aria-checked={on}
      title={title}
      disabled={disabled}
      onClick={(e) => {
        e.stopPropagation();
        onToggle();
      }}
    >
      <span className="track" />
      {on ? onLabel : offLabel}
    </button>
  );
}
