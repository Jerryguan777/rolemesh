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
      stroke-width="1.8"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <path d="M3 12h4l2.5 7 5-14 2.5 7h4"/>
    </svg>
  `;
}

/** Inbox — tray with incoming chevron. v2 topbar approvals trigger
 *  (spec §4.1). Matches the activity/settings stroke convention. */
export function iconInbox(size = 19): SVGTemplateResult {
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
      <path d="M22 12h-6l-2 3h-4l-2-3H2"/>
      <path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>
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
      stroke-width="1.8"
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
      stroke-width="1.8"
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
      stroke-width="1.8"
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
      stroke-width="1.8"
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
      stroke-width="1.8"
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

/** Copy / Duplicate — overlapping sheets. Used by the policy row's
 *  Duplicate hover action (spec §5.4). */
export function iconCopy(size = 15): SVGTemplateResult {
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
      <rect x="9" y="9" width="13" height="13" rx="2"/>
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
    </svg>
  `;
}

/** Settings-nav icons (per the v2 redesign). Sized 16×16 to fit the
 *  .ni rail; stroke-width 1.8
 *  matches the prototype's quiet line weight. */

/** Coworker — head + shoulders silhouette. */
export function iconUser(size = 16): SVGTemplateResult {
  return svg`
    <svg width=${size} height=${size} viewBox="0 0 24 24" fill="none"
      stroke="currentColor" stroke-width="1.8" aria-hidden="true">
      <circle cx="12" cy="8" r="4"/>
      <path d="M4 21v-1a8 8 0 0 1 16 0v1"/>
    </svg>
  `;
}

/** MCP servers — stacked rack units. */
export function iconServer(size = 16): SVGTemplateResult {
  return svg`
    <svg width=${size} height=${size} viewBox="0 0 24 24" fill="none"
      stroke="currentColor" stroke-width="1.8" stroke-linecap="round"
      aria-hidden="true">
      <rect x="3" y="4" width="18" height="7" rx="1.5"/>
      <rect x="3" y="13" width="18" height="7" rx="1.5"/>
      <path d="M7 7.5h.01M7 16.5h.01"/>
    </svg>
  `;
}

/** Skills — book / folder spine. */
export function iconBook(size = 16): SVGTemplateResult {
  return svg`
    <svg width=${size} height=${size} viewBox="0 0 24 24" fill="none"
      stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"
      aria-hidden="true">
      <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
      <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
    </svg>
  `;
}

/** Models — chip / square within square. */
export function iconChip(size = 16): SVGTemplateResult {
  return svg`
    <svg width=${size} height=${size} viewBox="0 0 24 24" fill="none"
      stroke="currentColor" stroke-width="1.8" aria-hidden="true">
      <rect x="5" y="5" width="14" height="14" rx="2"/>
      <rect x="9" y="9" width="6" height="6"/>
      <path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>
    </svg>
  `;
}

/** Credentials — key. */
export function iconKey(size = 16): SVGTemplateResult {
  return svg`
    <svg width=${size} height=${size} viewBox="0 0 24 24" fill="none"
      stroke="currentColor" stroke-width="1.8" aria-hidden="true">
      <circle cx="8" cy="15" r="4"/>
      <path d="m10.85 12.15 8-8M18 6l2 2M14 8l2 2"/>
    </svg>
  `;
}

/** Safety rules — shield. */
export function iconShield(size = 16): SVGTemplateResult {
  return svg`
    <svg width=${size} height=${size} viewBox="0 0 24 24" fill="none"
      stroke="currentColor" stroke-width="1.8" aria-hidden="true">
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
    </svg>
  `;
}

/** Check-in-a-box — Settings → Governance → Approval policies (matches the
 *  prototype nav icon; distinct from the plain shield used by Safety rules). */
export function iconClipboardCheck(size = 16): SVGTemplateResult {
  return svg`
    <svg width=${size} height=${size} viewBox="0 0 24 24" fill="none"
      stroke="currentColor" stroke-width="1.8" aria-hidden="true">
      <path d="M9 11l3 3L22 4"/>
      <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>
    </svg>
  `;
}

/** Document with lines — Settings → Governance → Safety log (matches the
 *  prototype nav icon). */
export function iconFileText(size = 16): SVGTemplateResult {
  return svg`
    <svg width=${size} height=${size} viewBox="0 0 24 24" fill="none"
      stroke="currentColor" stroke-width="1.8" aria-hidden="true">
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
      <path d="M14 2v6h6M16 13H8M16 17H8M10 9H8"/>
    </svg>
  `;
}

/** General — home / house. */
export function iconHome(size = 16): SVGTemplateResult {
  return svg`
    <svg width=${size} height=${size} viewBox="0 0 24 24" fill="none"
      stroke="currentColor" stroke-width="1.8" aria-hidden="true">
      <path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>
    </svg>
  `;
}

/** Members — multi-user. */
export function iconUsers(size = 16): SVGTemplateResult {
  return svg`
    <svg width=${size} height=${size} viewBox="0 0 24 24" fill="none"
      stroke="currentColor" stroke-width="1.8" aria-hidden="true">
      <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>
      <circle cx="9" cy="7" r="4"/>
      <path d="M23 21v-2a4 4 0 0 0-3-3.87"/>
    </svg>
  `;
}

/** Appearance — sun/star burst. */
export function iconSun(size = 16): SVGTemplateResult {
  return svg`
    <svg width=${size} height=${size} viewBox="0 0 24 24" fill="none"
      stroke="currentColor" stroke-width="1.8" aria-hidden="true">
      <circle cx="12" cy="12" r="5"/>
      <path d="M12 1v2M12 21v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M1 12h2M21 12h2"/>
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
      stroke-width="1.8"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <path d="M18 6 6 18M6 6l12 12"/>
    </svg>
  `;
}

/** Link — chain. Used by the Connected channels page. */
export function iconLink(size = 16): SVGTemplateResult {
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
      <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.72"/>
      <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.72-1.72"/>
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
