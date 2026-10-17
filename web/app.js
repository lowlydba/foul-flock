/* ALPR Siting Compliance Map - static frontend (MapLibre GL). */
"use strict";

const COLORS = {
  flagged: "#e0332c",
  clear: "#2e7dd1",
  excluded: "#8d6e2f",
  guideline: "#f2a900",
  law: "#8a3ffc",
  nolaw: "#9aa1a9",
};

const CATEGORY_LABELS = {
  school: "school",
  place_of_worship: "place of worship",
  healthcare_facility: "healthcare facility",
  courthouse: "courthouse",
  food_bank: "food bank",
  reproductive_health_clinic: "reproductive/sexual health facility",
  immigrant_services: "immigrant services provider",
};

const CAM_LAYERS = ["cams-flagged", "cams-guideline", "cams-clear", "cams-clear-far", "cams-excluded"];

let META = null; // rules.json content
let RULES_BY_ID = {};
let CAMS = null;   // cameras.geojson, mutated when the radius sliders move
// view_cones.geojson is ~40% of the data payload and invisible below
// CONE_MIN_ZOOM, so it loads on demand (CONE_LOAD_ZOOM leaves time to fetch).
const CONE_MIN_ZOOM = 13;
const CONE_LOAD_ZOOM = 12;
// "Nothing found" cameras are ~90% of all points and hidden until the
// Layers switch is on. Those with a candidate place nearby ship in the core
// file (a wider slider radius could flag them); the rest are static vector
// tiles (PMTiles) read by range request, so only tiles in view transfer.
const CLEAR_MIN_ZOOM = 6;
const CLEAR_HIDDEN_CONES = ["!=", ["get", "status"], "checked_clear"];
let CLEAR_ON = false;
const SLIDER_DEBOUNCE_MS = 120;
let CONES = { type: "FeatureCollection", features: [] }; // synced to camera status
let CONES_LOADED = false;
let CONES_LOADING = false;
let CONES_STALE = false; // statuses changed while cones were out of range
let CAM_STATUS = {}; // camera_id -> current status
let ALL_MATCHES = {}; // camera_id -> all candidate matches (unfiltered)
let CLOSE_M = 100;  // "close by" heuristic radius (statute gives no number)
let ONPREM_M = 50;  // "on premises" heuristic radius (on-property language)

/* Protected-place icons: Maki (Mapbox's CC0 map icon set), drawn as a
   white glyph on an orange badge. web/assets/icons/ holds the SVGs. */
const PLACE_ICONS = {
  "ff-school": "school",
  "ff-health": "hospital",
  "ff-court": "town-hall",
  "ff-food": "grocery",
  "ff-worship": "place-of-worship",
  "ff-worship-christian": "religious-christian",
  "ff-worship-muslim": "religious-muslim",
  "ff-worship-jewish": "religious-jewish",
  "ff-place": "marker",
};
const BADGE_FILL = "#c2620d";
const BADGE_RING = "rgba(0, 0, 0, .35)";

async function loadPlaceIcon(map, name, file) {
  const svgText = await (await fetch(`assets/icons/${file}.svg`)).text();
  const white = svgText.replace(
    "<svg ",
    '<svg fill="#ffffff" '
  );
  const glyph = new Image();
  await new Promise((resolve, reject) => {
    glyph.onload = resolve;
    glyph.onerror = reject;
    glyph.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(white);
  });
  const size = 56;
  const c = document.createElement("canvas");
  c.width = size;
  c.height = size;
  const ctx = c.getContext("2d");
  const r = 10; // corner radius: a rounded square reads as a place, not a camera dot
  const m = 2;
  ctx.beginPath();
  ctx.moveTo(m + r, m);
  ctx.arcTo(size - m, m, size - m, size - m, r);
  ctx.arcTo(size - m, size - m, m, size - m, r);
  ctx.arcTo(m, size - m, m, m, r);
  ctx.arcTo(m, m, size - m, m, r);
  ctx.closePath();
  ctx.fillStyle = BADGE_FILL;
  ctx.fill();
  ctx.lineWidth = 2;
  ctx.strokeStyle = BADGE_RING;
  ctx.stroke();
  const g = 32; // glyph box
  ctx.drawImage(glyph, (size - g) / 2, (size - g) / 2, g, g);
  map.addImage(name, ctx.getImageData(0, 0, size, size), { pixelRatio: 2 });
}

// Static layers (states, places, clear cameras and their cones) are PMTiles
// archives in web/data, read by HTTP range request through this protocol.
maplibregl.addProtocol("pmtiles", new pmtiles.Protocol().tile);
const tileSource = (name) => ({
  type: "vector",
  url: `pmtiles://${new URL(`data/${name}`, location.href).href}`,
});

const map = new maplibregl.Map({
  container: "map",
  style: {
    version: 8,
    sources: {
      osm: {
        type: "raster",
        tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
        tileSize: 256,
        attribution: "© OpenStreetMap contributors",
      },
    },
    layers: [{ id: "osm", type: "raster", source: "osm" }],
  },
  center: [-98, 39],
  zoom: 4,
  attributionControl: {
    compact: false,
    customAttribution:
      '<a href="https://overturemaps.org" target="_blank" rel="noopener">&copy; Overture Maps Foundation</a> | ' +
      'Made by <a href="https://github.com/lowlydba" target="_blank" rel="noopener">lowlydba</a>',
  },
});
const US_BOUNDS = [[-188, 18.5], [-66.5, 72]];
function updateMinZoom() {
  const camera = map.cameraForBounds(US_BOUNDS);
  if (camera) map.setMinZoom(camera.zoom);
}
updateMinZoom();
map.on("resize", updateMinZoom);
map.addControl(new maplibregl.NavigationControl({ visualizePitch: false }), "bottom-right");
map.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: "metric" }), "bottom-left");
map.addControl(new maplibregl.FullscreenControl(), "bottom-right");

async function fetchJSON(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  return res.json();
}

map.on("load", async () => {
  // Data files are generated by build/build.py and absent until a data
  // refresh lands; fail with a visible message instead of a broken map.
  try {
    META = await fetchJSON("data/rules.json");
    CAMS = await fetchJSON("data/cameras.geojson");
  } catch (err) {
    console.error("Map data failed to load:", err);
    document.getElementById("stats").textContent =
      "Map data unavailable (data files missing or failed to load)";
    return;
  }
  for (const r of [...META.rules, ...(META.guideline_rules || [])]) {
    // Accept statute_name/statute_citation, the field names older data files use.
    r.title ??= r.statute_name;
    r.citation ??= r.statute_citation;
    RULES_BY_ID[r.rule_id] = r;
  }
  const SLIDERS = META.sliders || {
    close_by: { min: 50, max: 400, step: 25, default: META.default_heuristic_buffer_m || 100 },
    on_premises: { min: 10, max: 150, step: 10, default: 50 },
  };
  CLOSE_M = SLIDERS.close_by.default;
  ONPREM_M = SLIDERS.on_premises.default;

  // Cameras live in memory so the radius sliders can restyle them without
  // a rebuild. Cameras with no candidate matches omit the property.
  for (const f of CAMS.features) {
    if (f.properties.matches) {
      ALL_MATCHES[f.properties.camera_id] = JSON.parse(f.properties.matches);
    }
  }

  await Promise.all(
    Object.entries(PLACE_ICONS).map(([name, file]) => loadPlaceIcon(map, name, file))
  );

  updateStats();
  document.getElementById("stats").title =
    `Data built ${META.generated_at.slice(0, 10)}`;

  // Two heuristic radii, both session-adjustable: "close by" for statutes
  // with no distance at all, "on premises" for on-property language.
  const wireSlider = (id, outId, cfg, onChange) => {
    const el = document.getElementById(id);
    el.min = cfg.min;
    el.max = cfg.max;
    el.step = cfg.step;
    el.value = cfg.default;
    document.getElementById(outId).textContent = `${cfg.default} m`;
    el.addEventListener("input", () => {
      document.getElementById(outId).textContent = `${el.value} m`;
      // Re-tiling every camera is costly; wait for the drag to settle.
      clearTimeout(sliderTimers[id]);
      sliderTimers[id] = setTimeout(() => onChange(Number(el.value)), SLIDER_DEBOUNCE_MS);
    });
  };
  const sliderTimers = {};
  wireSlider("buffer-slider", "buffer-value", SLIDERS.close_by,
    (v) => applyBuffer(v, ONPREM_M));
  wireSlider("onprem-slider", "onprem-value", SLIDERS.on_premises,
    (v) => applyBuffer(CLOSE_M, v));

  const cone = META.view_cone_heuristic || { half_angle_deg: 30, range_m: 60 };
  document.getElementById("buffer-note").innerHTML =
    `<dl>` +
    `<dt>Distances</dt><dd>A statute's own number is used when it gives one. ` +
    `Otherwise the radii above stand in, always labeled, never a legal threshold.</dd>` +
    `<dt>Spirit-of (amber)</dt><dd>Not statutes: best-effort views of executive ` +
    `actions or provisions not fully in effect. Cameras that predate a rule's ` +
    `cutoff are filtered out; later ones are only <em>possibly</em> a problem.</dd>` +
    `<dt>View cones</dt><dd>Zoom ≥ 13. An illustrative ` +
    `${cone.half_angle_deg * 2}° × ${cone.range_m} m spread from the mapped ` +
    `bearing, not true field of view.</dd>` +
    `<dt>Data</dt><dd>OSM <code>surveillance:type=ALPR</code> ` +
    `(DeFlock convention). Built ${META.generated_at.slice(0, 10)}.</dd>` +
    `</dl>`;

  // Status is settled before the sources are created so the map tiles the
  // cameras once, not twice.
  applyBuffer(CLOSE_M, ONPREM_M);

  map.addSource("states", tileSource("states.pmtiles"));
  map.addSource("cameras", { type: "geojson", data: CAMS });
  map.addSource("cones", { type: "geojson", data: CONES });
  // Optional archives are registered only when the build emitted them: an
  // absent one would otherwise 404 on every map load.
  const HAS_PLACES = META.place_count > 0;
  const HAS_CLEAR = META.clear_camera_count > 0;
  const HAS_CLEAR_CONES = META.clear_cone_count > 0;
  if (HAS_PLACES) map.addSource("places", tileSource("places.pmtiles"));
  if (HAS_CLEAR) map.addSource("clear-cams", tileSource("clear_cameras.pmtiles"));
  if (HAS_CLEAR_CONES) map.addSource("clear-cones", tileSource("clear_cones.pmtiles"));
  map.on("moveend", syncCones);
  map.on("zoomend", updateClearStatus);

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

  for (const layer of CAM_LAYERS) {
    // Touch input is handled by the tolerant tap handler below.
    map.on("click", layer, (e) => { if (!isTouch()) openDetail(e.features[0]); });
    map.on("mouseenter", layer, () => (map.getCanvas().style.cursor = "pointer"));
    map.on("mouseleave", layer, () => (map.getCanvas().style.cursor = ""));
  }

  // Camera dots are a few px wide; fingers are not. On touch, open the
  // nearest camera within a fingertip-sized radius of the tap.
  map.on("click", (e) => {
    if (!isTouch()) return;
    const hit = cameraNear(e.point);
    if (hit) openDetail(hit);
  });

  // Hover tooltip while browsing nationally: state name + review status +
  // when the law entry was last checked.
  const stateTip = new maplibregl.Popup({
    closeButton: false,
    closeOnClick: false,
    className: "state-tip",
    maxWidth: "260px",
  });
  map.on("mousemove", "state-fill", (e) => {
    if (map.getZoom() > 6.5) { stateTip.remove(); return; }
    const p = e.features[0].properties;
    const status = p.law_status === "siting_law"
      ? "Camera-siting law in effect"
      : p.law_status === "guideline"
        ? "No siting statute; spirit-of guideline mapped"
        : "Reviewed, no siting law found";
    const checked = p.last_verified
      ? `<br><span class="pop-fine">Last checked ${esc(p.last_verified)}</span>`
      : "";
    let detail = "";
    if (p.law_status !== "siting_law") {
      const note = stateSummary(p);
      const short = note.length > 220 ? `${note.slice(0, 220).trimEnd()}...` : note;
      const host = sourceHost(p.source_url);
      const gl = p.guideline_title && p.guideline_title !== "null"
        ? `<br><span class="pop-fine"><b>Guideline:</b> ${esc(p.guideline_title)}</span>` : "";
      detail = gl + (short ? `<br><span class="pop-fine">${esc(short)}</span>` : "") +
        (host ? `<br><span class="pop-fine">Source: ${esc(host)}</span>` : "");
    }
    stateTip
      .setLngLat(e.lngLat)
      .setHTML(`<b>${esc(p.name)}</b><br>${status}${checked}${detail}`)
      .addTo(map);
  });
  map.on("mouseleave", "state-fill", () => stateTip.remove());

  map.on("click", "state-fill", (e) => {
    // State-status popups only while browsing nationally; suppress when
    // zoomed in so empty clicks aren't spammy.
    if (map.getZoom() > 6.5) return;
    // Only when no camera was clicked on top
    if (cameraNear(e.point)) return;
    const p = e.features[0].properties;
    const html = stateStatusHtml(p);
    new maplibregl.Popup().setLngLat(e.lngLat).setHTML(html).addTo(map);
  });

  if (HAS_PLACES) map.on("click", "places-pts", (e) => {
    if (cameraNear(e.point)) return;
    const p = e.features[0].properties;
    const cat = CATEGORY_LABELS[p.category] || p.category;
    const rel = p.religion && p.religion !== "null" ? ` (${p.religion})` : "";
    new maplibregl.Popup()
      .setLngLat(e.lngLat)
      .setHTML(`<b>${esc(p.name)}</b><br>Protected category: ${esc(cat)}${esc(rel)}`)
      .addTo(map);
  });

  // Zoom to the siting-law states; the build precomputes their bounds so the
  // states layer isn't downloaded just to measure it.
  if (META.rule_bounds) {
    const [w, s, e, n] = META.rule_bounds;
    map.fitBounds([[w, s], [e, n]], { padding: isCompact()
      ? { top: topbarHeight() + 24, bottom: 24, left: 24, right: 24 }
      : 60 });
  }

  const clearToggle = document.getElementById("clear-toggle");
  clearToggle.checked = false;
  clearToggle.disabled = false;
  clearToggle.addEventListener("change", () => setClearVisible(clearToggle.checked));
});

/* ----------------------------- "Nothing found" ------------------------- */

function setClearVisible(on) {
  CLEAR_ON = on;
  const visibility = on ? "visible" : "none";
  for (const id of ["cams-clear", "cams-clear-far", "view-cones-clear"]) {
    if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", visibility);
  }
  map.setFilter("view-cones", on ? null : CLEAR_HIDDEN_CONES);
  updateClearStatus();
}

function updateClearStatus() {
  const text = !CLEAR_ON ? ""
    : map.getZoom() < CLEAR_MIN_ZOOM ? "Zoom in to a state to see them."
    : "Showing cameras in view.";
  const el = document.getElementById("clear-status");
  // Skip no-op writes so the live region doesn't re-announce on every pan.
  if (el.textContent !== text) el.textContent = text;
}

/* ------------------------ adjustable heuristic radius ------------------ */

// Effective screening distance for one match: statutory number if the law
// gives one; the "on premises" radius when the rule uses on-property
// language; else the "close by" radius.
function effectiveBuffer(m) {
  if (m.buffer_specified) return m.buffer_m;
  if (m.rule_heuristic_m) return ONPREM_M;
  return CLOSE_M;
}

function applyBuffer(closeM, onpremM) {
  CLOSE_M = closeM;
  ONPREM_M = onpremM !== undefined ? onpremM : ONPREM_M;

  const statusById = {};
  for (const f of CAMS.features) {
    const p = f.properties;
    if (p.status !== "possible_exclusion") {
      const within = (ALL_MATCHES[p.camera_id] || []).filter(
        (m) => m.distance_m <= effectiveBuffer(m)
      );
      const statutory = within.filter((m) => !m.guideline);
      p.status = statutory.length ? "flagged"
        : (within.length || p.road_flag) ? "guideline"
        : "checked_clear";
      // Absent properties keep the per-tile payload small for the ~90% of
      // cameras with nothing in range.
      if (within.length) {
        p.match_count = within.length;
        p.matches = JSON.stringify(within);
      } else {
        delete p.match_count;
        delete p.matches;
      }
    }
    statusById[p.camera_id] = p.status;
  }
  CAM_STATUS = statusById;
  CONES_STALE = true;
  const camSrc = map.getSource("cameras");
  if (camSrc) camSrc.setData(CAMS);
  syncCones();
  updateStats();
}

// Cones follow camera status. They load on first approach to CONE_MIN_ZOOM
// and are only re-pushed to the map while in range.
async function syncCones() {
  if (!map.getSource("cones") || map.getZoom() < CONE_LOAD_ZOOM) return;
  if (!CONES_LOADED) {
    if (CONES_LOADING) return;
    CONES_LOADING = true;
    try {
      CONES = await fetchJSON("data/view_cones.geojson");
      CONES_LOADED = true;
    } catch (err) {
      console.error("View cones failed to load:", err);
      return;
    } finally {
      CONES_LOADING = false;
    }
  }
  if (!CONES_STALE) return;
  for (const f of CONES.features) {
    f.properties.status = CAM_STATUS[f.properties.camera_id] || f.properties.status;
  }
  map.getSource("cones").setData(CONES);
  CONES_STALE = false;
}

function updateStats() {
  const count = (s) => (CAMS
    ? CAMS.features.filter((f) => f.properties.status === s).length
    : (s === "flagged" ? META.flagged_count : META.guideline_count || 0));
  const flagged = count("flagged");
  const guide = count("guideline");
  const gStates = META.guideline_states || [];
  document.getElementById("stats").textContent =
    `${flagged.toLocaleString()} flagged / ` +
    `${META.camera_count.toLocaleString()} cameras · ${META.rule_states.join(", ")}` +
    (gStates.length
      ? ` · ${guide.toLocaleString()} spirit-of (${gStates.join(", ")})`
      : "");
}

function stateStatusHtml(p) {
  if (p.law_status === "guideline") {
    const gl = (META.guideline_rules || []).filter((r) => r.state_code === p.state_code);
    const items = gl.map((r) => `<li><b>${esc(r.title)}</b>${r.distance_tbd ? " <b>(distance TBD)</b>" : ""}<br>${esc(r.summary)}</li>`);
    const host = sourceHost(p.source_url);
    return `<b>${esc(p.name)}</b><br>No siting statute. <b>Spirit-of guideline</b>
      (not a legal threshold):<ul class="pop-list">${items.join("")}</ul>
      <span class="pop-fine">${esc(stateSummary(p))}
      ${p.last_verified ? `Last checked ${esc(p.last_verified)}.` : ""}</span>${host ? `<br><span class="pop-fine">Source: <a href="${esc(p.source_url)}" target="_blank" rel="noopener noreferrer">${esc(host)}</a></span>` : ""}`;
  }
  if (p.law_status === "siting_law") {
    const rules = META.rules.filter((r) => r.state_code === p.state_code);
    const names = [...new Set(rules.map((r) => r.title))].join("; ");
    return `<b>${esc(p.name)}</b><br>Siting law in effect: ${esc(names)}
      ${p.last_verified ? `<br><span class="pop-fine">Last checked ${esc(p.last_verified)}</span>` : ""}`;
  }
  const host = sourceHost(p.source_url);
  const src = host
    ? `<br><span class="pop-fine">Source: <a href="${esc(p.source_url)}" target="_blank" rel="noopener noreferrer">${esc(host)}</a></span>`
    : "";
  return `<b>${esc(p.name)}</b><br>Reviewed - no siting-specific ALPR law found.<br>
    <span class="pop-fine">${esc(stateSummary(p))}
    ${p.last_verified ? `Last checked ${esc(p.last_verified)}.` : ""}</span>${src}`;
}

// MapLibre hands null feature properties back as the string "null".
function cleanProps(props) {
  return Object.fromEntries(
    Object.entries(props).map(([k, v]) => [k, v === "null" ? null : v])
  );
}

// A state's public summary; older data files call it note.
function stateSummary(p) {
  const s = p.summary ?? p.note;
  return s && s !== "null" ? s : "";
}

// Hostname of an http(s) source URL, or "" for anything else.
function sourceHost(url) {
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:" ? u.hostname : "";
  } catch {
    return "";
  }
}

/* ------------------------------- detail panel ------------------------- */

function openDetail(feature) {
  const p = cleanProps(feature.properties);
  const matches = JSON.parse(p.matches || "[]");
  const statutoryMatches = matches.filter((m) => !m.guideline);
  const coords = feature.properties.lat_e7 != null
    ? [feature.properties.lon_e7 / 1e7, feature.properties.lat_e7 / 1e7]
    : feature.geometry.coordinates; // clear-camera tiles carry exact coordinates
  const el = document.getElementById("detail-body");

  let html = `
    <h2 id="detail-title" tabindex="-1">ALPR camera</h2>
    <span class="status-badge ${p.status}">
      ${p.status === "flagged" ? "⚑ Flagged for review"
        : p.status === "guideline" ? "◐ Spirit-of guideline (not a statute)"
        : p.status === "possible_exclusion" ? "Possibly exempt device type"
        : "Checked - nothing found"}
    </span>
    <div class="kv"><b>Location:</b> ${coords[1].toFixed(5)}, ${coords[0].toFixed(5)} (${esc(p.state_code)})</div>
    ${p.operator ? `<div class="kv"><b>Operator:</b> ${esc(p.operator)}</div>` : ""}
    ${p.brand ? `<div class="kv"><b>Brand:</b> ${esc(p.brand)}</div>` : ""}
    ${p.direction ? `<div class="kv"><b>Facing:</b> ${esc(p.direction)}°</div>` : ""}
    ${p.last_edited ? `<div class="kv"><b>Last edited:</b> <time datetime="${esc(p.last_edited)}">${esc(p.last_edited)}</time> (${esc(ageLabel(p.last_edited))})</div>` : ""}
    <div class="kv"><b>Source:</b> <a href="${esc(p.osm_url)}" target="_blank" rel="noopener">OpenStreetMap node</a> (crowdsourced)</div>
  `;

  if (p.status === "flagged") {
    const exclRule = META.rules.find(
      (r) => r.state_code === p.state_code && r.statutory_exclusions
    );
    html += `<h3>Why it's flagged</h3>`;
    for (const m of statutoryMatches) {
      const rule = RULES_BY_ID[m.rule_id] || {};
      html += `
        <div class="match-card">
          <div><span class="dist">${Math.round(m.distance_m)} m</span> from
            <span class="place">${esc(m.place_name)}</span>
            (${esc(CATEGORY_LABELS[m.place_category] || m.place_category)})
            ${m.buffer_specified ? "" : `<span class="heuristic-tag">heuristic ${effectiveBuffer(m)} m - not a statutory distance</span>`}
          </div>
          ${m.buffer_specified || !rule.statutory_language ? "" : `
          <div class="statute-quote">What the law actually says${rule.language_verbatim === false ? " (paraphrased)" : ""}:
            <span class="lang">${esc(rule.statutory_language)}</span></div>`}
          <div class="cite">
            ${esc(rule.title || m.rule_id)} - ${esc(rule.citation || "")}<br>
            Effective ${esc(fmtDate(rule))} ·
            <a href="${esc(rule.source_url || "#")}" target="_blank" rel="noopener">statute source</a>
          </div>
        </div>`;
    }
    html += `
      <div class="disclaimer">
        Screening result - <b>not legal advice, not a violation determination</b>.
        Verify the camera, the place, and the current statute text before reporting.
        ${exclRule ? `<details><summary>Device-type exclusions</summary>${esc(exclRule.statutory_exclusions)}</details>` : ""}
      </div>
      <button class="report-btn" id="draft-report">✉ Draft a report / inquiry letter</button>
      <div class="report-box hidden" id="report-box">
        <textarea id="report-text" spellcheck="false" aria-label="Draft report letter, editable"></textarea>
        <div class="report-actions">
          <button id="copy-report">Copy to clipboard</button>
          <button id="dl-report">Download .txt</button>
        </div>
      </div>`;
  } else if (p.status === "guideline") {
    html += guidelineHtml(p, matches);
  } else if (p.status === "possible_exclusion") {
    html += `
      <h3>Why it isn't flagged</h3>
      <p>The mapped vendor (${esc(p.brand || "unknown")}) mainly makes camera types this
      state's statute excludes from its ALPR definition (speed / school-bus / traffic
      safety), so it was screened out to avoid a false positive.</p>
      <p class="fine">Vendor tags are crowdsourced. If this is actually an ALPR, fix its
      OSM tags via the source link above and the next build will check it.</p>`;
  } else {
    const stateRules = META.rules.filter((r) => r.state_code === p.state_code);
    const cats = [...new Set(stateRules.map((r) => CATEGORY_LABELS[r.restricted_category] || r.restricted_category))];
    html += `
      <h3>What was checked</h3>
      <p>${stateRules.length
        ? `This state has a siting law. No mapped ${esc(cats.join(", "))} was found within
      the screened distance.`
        : `This state has no siting statute. Nothing matched its spirit-of guideline.`}</p>
      ${p.road_phase === "existing" ? `<p>On a state road (${esc(p.road_ref)}), but already mapped before the
      ${esc(guidelineCutoff(p))} permit pause, so it is treated as an existing installation
      and not flagged.</p>` : ""}
      <p class="fine">Mapped data is incomplete - absence of a flag is not a compliance
      certification.</p>`;
  }

  el.innerHTML = html;
  showDetail(coords);

  const btn = document.getElementById("draft-report");
  if (btn) {
    btn.addEventListener("click", () => {
      const box = document.getElementById("report-box");
      box.classList.remove("hidden");
      document.getElementById("report-text").value = draftReport(p, coords, statutoryMatches);
      box.scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
    document.getElementById("copy-report")?.addEventListener("click", async (ev) => {
      await navigator.clipboard.writeText(document.getElementById("report-text").value);
      ev.target.textContent = "Copied ✓";
      setTimeout(() => (ev.target.textContent = "Copy to clipboard"), 1500);
    });
    document.getElementById("dl-report")?.addEventListener("click", () => {
      const blob = new Blob([document.getElementById("report-text").value], { type: "text/plain" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `alpr-siting-inquiry-${p.camera_id}.txt`;
      a.click();
      URL.revokeObjectURL(a.href);
    });
  }
}

function guidelineCutoff(p) {
  const r = RULES_BY_ID[p.road_rule_id] || {};
  return String(r.grandfather_cutoff || "pause").slice(0, 10);
}

// Detail body for "spirit-of" cameras: executive actions or pending
// provisions, never a violation claim and never a report letter.
function guidelineHtml(p, matches) {
  let html = `<div class="guideline-banner"><b>Not a statute, not a violation.</b>
    A best-effort "spirit-of" view of an executive action or a provision that
    is not fully operative yet. Verify everything before acting.</div>`;
  const road = RULES_BY_ID[p.road_rule_id];
  if (p.road_flag && road) {
    const cutoff = guidelineCutoff(p);
    const why = p.road_phase === "new"
      ? `First appears in OpenStreetMap after the ${esc(cutoff)} pause. It may be a new
         installation, or an older camera that was mapped late: <b>possibly</b> a problem.`
      : p.road_phase === "unknown"
        ? `The pre-pause map snapshot was unavailable, so we cannot tell whether this camera
           already existed on ${esc(cutoff)}.`
        : `Removal from state highways was required by
           ${esc(fmtDate({ effective_date: road.compliance_deadline || road.effective_date, date_approximate: !road.compliance_deadline && road.date_approximate }))}.
           A camera still mapped here is either stale map data or still installed.`;
    html += `<h3>Why it's shown</h3>
      <div class="match-card guideline">
        <div>Within ${esc(road.road_buffer_m)} m of a mapped state road${p.road_ref ? ` (<span class="place">${esc(p.road_ref)}</span>)` : ""}. That does not establish it sits in the right-of-way.</div>
        <div class="statute-quote">${why}</div>
        <div class="cite">${esc(road.title)} · effective ${esc(fmtDate(road))} ·
          <a href="${esc(road.source_url || "#")}" target="_blank" rel="noopener">source</a><br>
          ${esc(road.summary)}</div>
      </div>`;
  }
  const poi = matches.filter((m) => m.guideline);
  if (poi.length) {
    html += `<h3>Why it's shown</h3>
      <div class="guideline-banner tbd"><b>Distance TBD.</b> Connecticut's POST Council
      has not set the protective distance, and only facilities that registered with the
      Council are covered, so these places may not be covered at all. The
      ${effectiveBuffer(poi[0])} m radius is ours, and these categories are best-effort
      matches.</div>`;
    for (const m of poi) {
      const rule = RULES_BY_ID[m.rule_id] || {};
      html += `
        <div class="match-card guideline">
          <div><span class="dist">${Math.round(m.distance_m)} m</span> from
            <span class="place">${esc(m.place_name)}</span>
            (${esc(CATEGORY_LABELS[m.place_category] || m.place_category)})
            <span class="heuristic-tag">heuristic ${effectiveBuffer(m)} m - distance TBD</span>
          </div>
          <div class="cite">${esc(rule.title || m.rule_id)} ·
            <a href="${esc(rule.source_url || "#")}" target="_blank" rel="noopener">statute source</a></div>
        </div>`;
    }
  }
  html += `<p class="fine">Crowdsourced camera data. A fixed reader is assumed; OSM does not
    reliably distinguish mobile units.</p>`;
  return html;
}

function draftReport(p, coords, matches) {
  const m = matches[0];
  const rule = RULES_BY_ID[m.rule_id] || {};
  const lines = [
    "[DRAFT - starting point only. Not legal advice. Replace all bracketed",
    " fields, and independently verify every fact below before sending.]",
    "",
    "To: [Agency / city council / police department public records officer]",
    "Subject: Inquiry regarding ALPR camera placement near a protected location",
    "",
    "Dear [Name/Office],",
    "",
    "I am writing regarding an automated license plate reader (ALPR) camera",
    `located at approximately ${coords[1].toFixed(5)}, ${coords[0].toFixed(5)}` +
      (p.operator ? ` (operator recorded as: ${p.operator})` : "") + ".",
    "",
    "Publicly available, crowdsourced map data indicates this camera is",
    `approximately ${Math.round(m.distance_m)} meters from ${m.place_name},`,
    `a ${CATEGORY_LABELS[m.place_category] || m.place_category}.`,
    "",
    `${rule.title || "State law"} (${rule.citation || "citation"},`,
    `effective ${rule.effective_date ? fmtDate(rule) : "[date]"}) restricts the placement of ALPR`,
    `equipment on or near locations of this kind.`,
  ];
  if (!m.buffer_specified) {
    const paraphrased = rule.language_verbatim === false;
    lines.push(
      "",
      paraphrased
        ? "Note: the bill summary I reviewed does not specify a numeric distance; the"
        : "Note: the statute does not specify a numeric distance; the",
      `${effectiveBuffer(m)}-meter figure above is a proximity estimate used for screening,`,
      "not a statutory threshold. The relevant statutory language is:",
      `  ${rule.statutory_language || "[quote the statute's placement language here]"}`,
      ...(paraphrased
        ? ["  (paraphrased from a summary of the bill; not checked against the enacted text)"]
        : []),
      "I raise this for your review of whether the placement complies with",
      "that language."
    );
  }
  if (rule.statutory_exclusions) {
    lines.push(
      "",
      "I acknowledge the statute excludes certain device types (school bus",
      "safety, speed safety, and automated traffic safety camera systems) from",
      "its ALPR definition. If this device falls within one of those exclusions,",
      "I would appreciate confirmation of that classification."
    );
  }
  lines.push(
    "",
    "I respectfully request:",
    "  1. Confirmation of whether this camera is operated by or on behalf of",
    "     your agency;",
    "  2. Records sufficient to show its installation date, exact location, and",
    "     any siting/compliance review performed under the statute above;",
    "  3. If the placement is found inconsistent with the statute, information",
    "     on the process and timeline for relocation or removal.",
    "",
    "This request is made in good faith based on public data that I understand",
    "may be incomplete or out of date; I welcome any correction.",
    "",
    "Sincerely,",
    "[Your name]",
    "[Your contact information]",
    "",
    "---",
    `Generated by ALPR Siting Compliance Map on ${new Date().toISOString().slice(0, 10)}.`,
    `Camera source: ${p.osm_url} (© OpenStreetMap contributors)` +
      (p.last_edited ? `, record last edited ${p.last_edited}.` : "."),
    `Statute source: ${rule.source_url || "[verify]"}.`
  );
  return lines.join("\n");
}

/* ------------------------- responsive modes --------------------------- */

// Keep in sync with the "responsive modes" media queries in style.css.
const MODES = {
  phone: window.matchMedia("(max-width: 640px)"),
  tablet: window.matchMedia("(min-width: 641px) and (max-width: 1024px)"),
  touch: window.matchMedia("(pointer: coarse)"),
  reducedMotion: window.matchMedia("(prefers-reduced-motion: reduce)"),
};
const TAP_TOLERANCE_PX = 22;

function isPhone() { return MODES.phone.matches; }
function isCompact() { return MODES.phone.matches || MODES.tablet.matches; }
function isTouch() { return MODES.touch.matches; }
function topbarHeight() { return document.getElementById("topbar").offsetHeight; }

// Panels and the map's visible area are offset by the live topbar height,
// which changes when the phone layout wraps it onto two rows.
const topbarObserver = new ResizeObserver(() =>
  document.documentElement.style.setProperty("--topbar-h", `${topbarHeight()}px`)
);
topbarObserver.observe(document.getElementById("topbar"));

// Nearest camera within a fingertip radius (touch) or exactly under the
// pointer (mouse).
function cameraNear(point) {
  const r = isTouch() ? TAP_TOLERANCE_PX : 0;
  const hits = map.queryRenderedFeatures(
    r ? [[point.x - r, point.y - r], [point.x + r, point.y + r]] : point,
    { layers: CAM_LAYERS.filter((id) => map.getLayer(id)) }
  );
  if (!r || hits.length <= 1) return hits[0] || null;
  const d2 = (f) => {
    const q = map.project(f.geometry.coordinates);
    return (q.x - point.x) ** 2 + (q.y - point.y) ** 2;
  };
  return hits.reduce((best, f) => (d2(f) < d2(best) ? f : best));
}

const legendEl = document.getElementById("legend");
const legendToggle = document.getElementById("legend-toggle");
const detailEl = document.getElementById("detail");
const aboutEl = document.getElementById("about");
const layersEl = document.getElementById("layers");
const layersBtn = document.getElementById("layers-btn");
const layersPanel = document.getElementById("layers-panel");

// Disclosure popover for optional map layers.
function setLayersOpen(open, { restoreFocus = false } = {}) {
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

function setLegendOpen(open, { restoreFocus = false } = {}) {
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

function showDetail(coords) {
  detailEl.classList.remove("hidden");
  if (isCompact()) {
    setLegendOpen(false);
    setLayersOpen(false);
  }
  detailEl.scrollTop = 0;
  document.getElementById("detail-title")?.focus({ preventScroll: true });
  if (isCompact()) padMapForDetail(coords);
}

function closeDetail({ restoreFocus = false } = {}) {
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

/* ---------------------------------- misc ------------------------------ */

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

// ISO date for display, prefixed "about" when the rule marks it approximate.
function fmtDate(rule) {
  const d = String(rule.effective_date || "?").slice(0, 10);
  return rule.date_approximate ? `about ${d}` : d;
}

// "today", "12 days ago", "7 months ago", "3 years ago" for an ISO date.
function ageLabel(isoDate) {
  const then = new Date(`${isoDate}T00:00:00Z`);
  const days = Math.floor((Date.now() - then.getTime()) / 86400000);
  if (Number.isNaN(days) || days < 0) return "date unavailable";
  if (days < 1) return "today";
  if (days < 60) return `${days} day${days === 1 ? "" : "s"} ago`;
  if (days < 730) return `${Math.round(days / 30.44)} months ago`;
  return `${Math.floor(days / 365.25)} years ago`;
}

document.getElementById("detail-close").addEventListener("click", () =>
  closeDetail({ restoreFocus: true })
);

let aboutOpener = null;
function openAbout() {
  aboutOpener = document.activeElement;
  aboutEl.classList.remove("hidden");
  aboutEl.querySelector(".modal-close").focus();
}
function closeAbout() {
  aboutEl.classList.add("hidden");
  if (location.hash === "#about") {
    history.replaceState(null, "", location.pathname + location.search);
  }
  aboutOpener?.focus();
  aboutOpener = null;
}
document.getElementById("about-btn").addEventListener("click", openAbout);
aboutEl.querySelector(".modal-close").addEventListener("click", closeAbout);
aboutEl.addEventListener("click", (e) => {
  if (e.target.id === "about") closeAbout();
});
// Old How & why links redirect to index.html#about.
const openAboutFromHash = () => {
  if (location.hash === "#about" && aboutEl.classList.contains("hidden")) openAbout();
};
openAboutFromHash();
window.addEventListener("hashchange", openAboutFromHash);

// Escape closes the topmost transient surface; Tab stays inside the modal.
document.addEventListener("keydown", (e) => {
  const aboutOpen = !aboutEl.classList.contains("hidden");
  if (e.key === "Escape") {
    if (aboutOpen) closeAbout();
    else if (!layersPanel.hidden) setLayersOpen(false, { restoreFocus: true });
    else if (legendEl.classList.contains("is-open") && isCompact()) {
      setLegendOpen(false, { restoreFocus: true });
    } else if (detailEl.contains(document.activeElement)) {
      closeDetail({ restoreFocus: true });
    }
  } else if (e.key === "Tab" && aboutOpen) {
    const items = [...aboutEl.querySelectorAll("a[href], button")];
    const first = items[0];
    const last = items[items.length - 1];
    if (!aboutEl.contains(document.activeElement)) {
      e.preventDefault();
      first.focus();
    } else if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }
});

/* Test hook: lets the a11y suite render the detail panel deterministically. */
window.__ff = { openDetail, applyBuffer, map, get cams() { return CAMS; },
  get conesLoaded() { return CONES_LOADED; } };
