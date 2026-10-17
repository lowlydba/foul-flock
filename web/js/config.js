/* Shared constants: colors, labels, layer ids and zoom thresholds. */

export const COLORS = {
  flagged: "#e0332c",
  clear: "#2e7dd1",
  excluded: "#8d6e2f",
  guideline: "#f2a900",
  law: "#8a3ffc",
  nolaw: "#9aa1a9",
};

export const CATEGORY_LABELS = {
  school: "school",
  place_of_worship: "place of worship",
  healthcare_facility: "healthcare facility",
  courthouse: "courthouse",
  food_bank: "food bank",
  reproductive_health_clinic: "reproductive/sexual health facility",
  immigrant_services: "immigrant services provider",
};

export const CAM_LAYERS = ["cams-flagged", "cams-guideline", "cams-clear", "cams-clear-far", "cams-excluded"];
// view_cones.geojson is ~40% of the data payload and invisible below
// CONE_MIN_ZOOM, so it loads on demand (CONE_LOAD_ZOOM leaves time to fetch).
export const CONE_MIN_ZOOM = 13;
export const CONE_LOAD_ZOOM = 12;
// "Nothing found" cameras are ~90% of all points and hidden until the
// Layers switch is on. Those with a candidate place nearby ship in the core
// file (a wider slider radius could flag them); the rest are static vector
// tiles (PMTiles) read by range request, so only tiles in view transfer.
export const CLEAR_MIN_ZOOM = 6;
export const CLEAR_HIDDEN_CONES = ["!=", ["get", "status"], "checked_clear"];
export const SLIDER_DEBOUNCE_MS = 120;
