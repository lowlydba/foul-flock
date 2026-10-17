/* Map sources and layers. Optional PMTiles archives are registered only
   when the build emitted them. */

import { COLORS, CONE_MIN_ZOOM, CLEAR_MIN_ZOOM, CLEAR_HIDDEN_CONES } from "./config.js";
import { map, tileSource } from "./map.js";
import { state } from "./state.js";

export function addSourcesAndLayers() {
  map.addSource("states", tileSource("states.pmtiles"));
  map.addSource("cameras", { type: "geojson", data: state.CAMS });
  map.addSource("cones", { type: "geojson", data: state.CONES });
  // Optional archives are registered only when the build emitted them: an
  // absent one would otherwise 404 on every map load.
  const HAS_PLACES = state.META.place_count > 0;
  const HAS_CLEAR = state.META.clear_camera_count > 0;
  const HAS_CLEAR_CONES = state.META.clear_cone_count > 0;
  if (HAS_PLACES) map.addSource("places", tileSource("places.pmtiles"));
  if (HAS_CLEAR) map.addSource("clear-cams", tileSource("clear_cameras.pmtiles"));
  if (HAS_CLEAR_CONES) map.addSource("clear-cones", tileSource("clear_cones.pmtiles"));

  map.addLayer({
    id: "state-fill",
    type: "fill",
    source: "states",
    "source-layer": "states",
    paint: {
      "fill-color": [
        "match", ["get", "law_status"],
        "siting_law", COLORS.law,
        "guideline", COLORS.guideline,
        COLORS.nolaw,
      ],
      "fill-opacity": [
        "match", ["get", "law_status"],
        "siting_law", 0.16,
        "guideline", 0.26,
        0.22,
      ],
    },
  });
  map.addLayer({
    id: "state-line",
    type: "line",
    source: "states",
    "source-layer": "states",
    paint: {
      "line-color": [
        "match", ["get", "law_status"],
        "siting_law", "#5b21b6",
        "guideline", "#a36b00",
        "#6b7280",
      ],
      "line-width": [
        "match", ["get", "law_status"],
        "siting_law", 1.8,
        1.0,
      ],
    },
  });

  // A build with no protected places in range ships no places archive.
  if (HAS_PLACES) map.addLayer({
    id: "places-pts",
    type: "symbol",
    source: "places",
    "source-layer": "places",
    minzoom: 10,
    layout: {
      "icon-image": [
        "case",
        ["==", ["get", "category"], "place_of_worship"],
        ["match", ["coalesce", ["get", "religion"], ""],
          "christian", "ff-worship-christian",
          "muslim", "ff-worship-muslim",
          "jewish", "ff-worship-jewish",
          "ff-worship",
        ],
        ["match", ["get", "category"],
          "school", "ff-school",
          "healthcare_facility", "ff-health",
          "reproductive_health_clinic", "ff-health",
          "courthouse", "ff-court",
          "food_bank", "ff-food",
          "ff-place",
        ],
      ],
      "icon-size": ["interpolate", ["linear"], ["zoom"], 10, 0.45, 15, 0.7],
      "icon-allow-overlap": true,
    },
  });

  const CONE_PAINT = {
    "fill-color": [
      "match", ["get", "status"],
      "flagged", COLORS.flagged,
      "guideline", COLORS.guideline,
      COLORS.clear,
    ],
    "fill-opacity": 0.18,
    "fill-outline-color": "#555",
  };
  // "Nothing found" cameras are hidden until the legend toggle is on.
  map.addLayer({
    id: "view-cones",
    type: "fill",
    source: "cones",
    minzoom: CONE_MIN_ZOOM,
    filter: CLEAR_HIDDEN_CONES,
    paint: CONE_PAINT,
  });
  if (HAS_CLEAR_CONES) {
    map.addLayer({
      id: "view-cones-clear",
      type: "fill",
      source: "clear-cones",
      "source-layer": "clear_cones",
      minzoom: CONE_MIN_ZOOM,
      layout: { visibility: "none" },
      paint: CONE_PAINT,
    });
  }

  map.addLayer({
    id: "cams-excluded",
    type: "circle",
    source: "cameras",
    filter: ["==", ["get", "status"], "possible_exclusion"],
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 5, 2.2, 12, 5],
      "circle-color": COLORS.excluded,
      "circle-opacity": 0.75,
      "circle-stroke-width": 0.5,
      "circle-stroke-color": "#4d3a12",
    },
  });
  const CLEAR_DOT_PAINT = {
    "circle-radius": ["interpolate", ["linear"], ["zoom"], 5, 2.2, 12, 5],
    "circle-color": COLORS.clear,
    "circle-opacity": 0.75,
    "circle-stroke-width": 0.5,
    "circle-stroke-color": "#123a63",
  };
  // Candidates that a wider radius could flag (loaded with the core file).
  map.addLayer({
    id: "cams-clear",
    type: "circle",
    source: "cameras",
    minzoom: CLEAR_MIN_ZOOM,
    layout: { visibility: "none" },
    filter: ["==", ["get", "status"], "checked_clear"],
    paint: CLEAR_DOT_PAINT,
  });
  // Cameras with no candidate place nearby: static tiles, only in view.
  if (HAS_CLEAR) {
    map.addLayer({
      id: "cams-clear-far",
      type: "circle",
      source: "clear-cams",
      "source-layer": "clear_cameras",
      minzoom: CLEAR_MIN_ZOOM,
      layout: { visibility: "none" },
      paint: CLEAR_DOT_PAINT,
    });
  }
  map.addLayer({
    id: "cams-guideline",
    type: "circle",
    source: "cameras",
    filter: ["==", ["get", "status"], "guideline"],
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 5, 3.5, 12, 7.5],
      "circle-color": COLORS.guideline,
      "circle-opacity": 0.9,
      "circle-stroke-width": 1.4,
      "circle-stroke-color": "#5c3b00",
    },
  });
  map.addLayer({
    id: "cams-flagged",
    type: "circle",
    source: "cameras",
    filter: ["==", ["get", "status"], "flagged"],
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 5, 4, 12, 8],
      "circle-color": COLORS.flagged,
      "circle-opacity": 1,
      "circle-stroke-width": 1.6,
      "circle-stroke-color": "#ffffff",
    },
  });
}
