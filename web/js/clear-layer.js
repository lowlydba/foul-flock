/* The off-by-default "Cameras with nothing found" layer switch. */

import { CLEAR_MIN_ZOOM, CLEAR_HIDDEN_CONES } from "./config.js";
import { map } from "./map.js";
import { state } from "./state.js";

export function setClearVisible(on) {
  state.CLEAR_ON = on;
  const visibility = on ? "visible" : "none";
  for (const id of ["cams-clear", "cams-clear-far", "view-cones-clear"]) {
    if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", visibility);
  }
  map.setFilter("view-cones", on ? null : CLEAR_HIDDEN_CONES);
  updateClearStatus();
}

export function updateClearStatus() {
  const text = !state.CLEAR_ON ? ""
    : map.getZoom() < CLEAR_MIN_ZOOM ? "Zoom in to a state to see them."
    : "Showing cameras in view.";
  const el = document.getElementById("clear-status");
  // Skip no-op writes so the live region doesn't re-announce on every pan.
  if (el.textContent !== text) el.textContent = text;
}
