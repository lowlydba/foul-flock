/* Layout modes (desktop, tablet, phone) and the live topbar height. */

/* ------------------------- responsive modes --------------------------- */

// Keep in sync with the "responsive modes" media queries in css/responsive.css.
export const MODES = {
  phone: window.matchMedia("(max-width: 640px)"),
  tablet: window.matchMedia("(min-width: 641px) and (max-width: 1024px)"),
  touch: window.matchMedia("(pointer: coarse)"),
  reducedMotion: window.matchMedia("(prefers-reduced-motion: reduce)"),
};
export const TAP_TOLERANCE_PX = 22;

export function isPhone() { return MODES.phone.matches; }
export function isCompact() { return MODES.phone.matches || MODES.tablet.matches; }
export function isTouch() { return MODES.touch.matches; }
export function topbarHeight() { return document.getElementById("topbar").offsetHeight; }

// Panels and the map's visible area are offset by the live topbar height,
// which changes when the phone layout wraps it onto two rows.
const topbarObserver = new ResizeObserver(() =>
  document.documentElement.style.setProperty("--topbar-h", `${topbarHeight()}px`)
);
topbarObserver.observe(document.getElementById("topbar"));
