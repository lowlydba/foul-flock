/* Shared mutable app state. ES module bindings are read-only for importers,
   so values that change after load live on this one object. */
export const state = {
  META: null,          // rules.json content
  RULES_BY_ID: {},
  CAMS: null,          // cameras.geojson, mutated when the radius sliders move
  // view_cones.geojson loads on demand and is kept in sync with camera status.
  CONES: { type: "FeatureCollection", features: [] },
  CONES_LOADED: false,
  CONES_LOADING: false,
  CONES_STALE: false,  // statuses changed while cones were out of range
  CAM_STATUS: {},      // camera_id -> current status
  ALL_MATCHES: {},     // camera_id -> all candidate matches (unfiltered)
  CLOSE_M: 100,        // "close by" heuristic radius (statute gives no number)
  ONPREM_M: 50,        // "on premises" heuristic radius (on-property language)
  CLEAR_ON: false,     // "Cameras with nothing found" layer switch
};
