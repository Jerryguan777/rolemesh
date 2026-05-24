// Shared inline SVG icons for the v2 shell. Centralised so the same
// stroke weight / viewBox / fill convention applies everywhere; the
// chat shell topbar uses these but v2-B (settings forms) and v2-C
// (activity surface) will too — keep them small and consumable.
//
// Each function returns a Lit `TemplateResult`, not a string, so the
// renderer can attach event handlers if the caller wraps the icon
// in a `<button>` (which is the common case). We avoid string
// concatenation to keep the rendering pipeline single-source.

import { svg, type SVGTemplateResult } from 'lit';

/** Activity — heartbeat pulse line. v2 topbar / Activity nav. */
export function iconActivity(size = 19): SVGTemplateResult {
  return svg`
    <svg
      width=${size}
      height=${size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="1.7"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <path d="M3 12h4l2.5 7 5-14 2.5 7h4"/>
    </svg>
  `;
}

/** Approvals — clipboard checkmark. v2 topbar Approvals popover. */
export function iconApprovals(size = 19): SVGTemplateResult {
  return svg`
    <svg
      width=${size}
      height=${size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="1.7"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <path d="M9 11l3 3L22 4"/>
      <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>
    </svg>
  `;
}

/** Settings — gear. v2 topbar + settings shell sidebar entry. */
export function iconSettings(size = 19): SVGTemplateResult {
  return svg`
    <svg
      width=${size}
      height=${size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="1.7"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="3"/>
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
    </svg>
  `;
}

/** Chevron-right — Activity index cards, breadcrumb separators. */
export function iconChevronRight(size = 15): SVGTemplateResult {
  return svg`
    <svg
      width=${size}
      height=${size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <path d="m9 6 6 6-6 6"/>
    </svg>
  `;
}

/** Chevron-down — used by coworker switcher / user pill / menus. */
export function iconChevronDown(size = 15): SVGTemplateResult {
  return svg`
    <svg
      width=${size}
      height=${size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <path d="m6 9 6 6 6-6"/>
    </svg>
  `;
}

/** Plus — "+ New chat", "+ Add MCP server", etc. */
export function iconPlus(size = 16): SVGTemplateResult {
  return svg`
    <svg
      width=${size}
      height=${size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <path d="M12 5v14M5 12h14"/>
    </svg>
  `;
}

/** Magnifying glass — search affordance. */
export function iconSearch(size = 15): SVGTemplateResult {
  return svg`
    <svg
      width=${size}
      height=${size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <circle cx="11" cy="11" r="7"/>
      <path d="m21 21-4.3-4.3"/>
    </svg>
  `;
}

/** Pencil — Edit action on list rows. */
export function iconPencil(size = 15): SVGTemplateResult {
  return svg`
    <svg
      width=${size}
      height=${size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="1.8"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <path d="M12 20h9"/>
      <path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/>
    </svg>
  `;
}

/** Trash — Delete action on list rows. */
export function iconTrash(size = 15): SVGTemplateResult {
  return svg`
    <svg
      width=${size}
      height=${size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="1.8"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <path d="M3 6h18"/>
      <path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2"/>
      <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>
    </svg>
  `;
}

/** X close button — dialog header, popover dismiss. */
export function iconClose(size = 16): SVGTemplateResult {
  return svg`
    <svg
      width=${size}
      height=${size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <path d="M18 6 6 18M6 6l12 12"/>
    </svg>
  `;
}

/** Logout — door arrow. User-pill menu danger action. */
export function iconLogout(size = 15): SVGTemplateResult {
  return svg`
    <svg
      width=${size}
      height=${size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="1.8"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
      <path d="M16 17l5-5-5-5"/>
      <path d="M21 12H9"/>
    </svg>
  `;
}
