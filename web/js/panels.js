/* Legend, Layers popover and camera detail panel open/close behavior. */

import { map } from "./map.js";
import { MODES, isPhone, isCompact, topbarHeight } from "./layout.js";

const legendEl = document.getElementById("legend");
const legendToggle = document.getElementById("legend-toggle");
const detailEl = document.getElementById("detail");
const layersEl = document.getElementById("layers");
const layersBtn = document.getElementById("layers-btn");
const layersPanel = document.getElementById("layers-panel");

// Disclosure popover for optional map layers.
export function setLayersOpen(open, { restoreFocus = false } = {}) {
  layersPanel.hidden = !open;
  layersBtn.setAttribute("aria-expanded", String(open));
  if (open && isCompact()) {
    setLegendOpen(false);
    closeDetail();
  } else if (!open && restoreFocus) {
    layersBtn.focus();
  }
}
layersBtn.addEventListener("click", () => setLayersOpen(layersPanel.hidden));
document.addEventListener("pointerdown", (e) => {
  if (!layersPanel.hidden && !layersEl.contains(e.target)) setLayersOpen(false);
});

export function setLegendOpen(open, { restoreFocus = false } = {}) {
  legendEl.classList.toggle("is-open", open);
  legendToggle.setAttribute("aria-expanded", String(open));
  if (open && isCompact()) {
    closeDetail();
    setLayersOpen(false);
    document.getElementById("legend-close").focus();
  } else if (!open && restoreFocus) {
    legendToggle.focus();
  }
}
legendToggle.addEventListener("click", () =>
  setLegendOpen(!legendEl.classList.contains("is-open"))
);
document.getElementById("legend-close").addEventListener("click", () =>
  setLegendOpen(false, { restoreFocus: true })
);

// Slide the map so the selected camera stays visible beside or above the
// open panel, and below the topbar, instead of hiding underneath them.
function padMapForDetail(coords) {
  const open = !detailEl.classList.contains("hidden");
  const padding = { top: 0, right: 0, bottom: 0, left: 0 };
  if (open && isCompact()) padding.top = topbarHeight();
  if (open && isPhone()) padding.bottom = detailEl.offsetHeight;
  else if (open && MODES.tablet.matches) padding.right = detailEl.offsetWidth;
  const opts = { padding, duration: MODES.reducedMotion.matches ? 0 : 250 };
  if (open && coords) opts.center = coords;
  map.easeTo(opts);
}

export function showDetail(coords) {
  detailEl.classList.remove("hidden");
  if (isCompact()) {
    setLegendOpen(false);
    setLayersOpen(false);
  }
  detailEl.scrollTop = 0;
  document.getElementById("detail-title")?.focus({ preventScroll: true });
  if (isCompact()) padMapForDetail(coords);
}

export function closeDetail({ restoreFocus = false } = {}) {
  if (detailEl.classList.contains("hidden")) return;
  detailEl.classList.add("hidden");
  if (isCompact()) padMapForDetail(null);
  if (restoreFocus) map.getCanvas().focus();
}

// Re-evaluate the layout when the viewport crosses a breakpoint (rotation,
// window resize): collapse the legend and drop stale map padding.
const onModeChange = () => {
  setLegendOpen(false);
  if (!detailEl.classList.contains("hidden")) padMapForDetail(null);
};
MODES.phone.addEventListener("change", onModeChange);
MODES.tablet.addEventListener("change", onModeChange);

export { legendEl, detailEl, layersPanel };
