/* Heuristic radii: slider wiring, per-camera status recomputation, lazy
   candidate view cones, and the topbar counts. */

import { SLIDER_DEBOUNCE_MS, CONE_LOAD_ZOOM } from "./config.js";
import { map } from "./map.js";
import { state } from "./state.js";
import { fetchJSON } from "./util.js";

// Two heuristic radii, both session-adjustable: "close by" for statutes
// with no distance at all, "on premises" for on-property language.
export function initRadiusControls(SLIDERS) {
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
    (v) => applyBuffer(v, state.ONPREM_M));
  wireSlider("onprem-slider", "onprem-value", SLIDERS.on_premises,
    (v) => applyBuffer(state.CLOSE_M, v));
}

export function renderMethodNotes() {
  const cone = state.META.view_cone_heuristic || { half_angle_deg: 30, range_m: 60 };
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
    `(DeFlock convention). Built ${state.META.generated_at.slice(0, 10)}.</dd>` +
    `</dl>`;
}

/* ------------------------ adjustable heuristic radius ------------------ */

// Effective screening distance for one match: statutory number if the law
// gives one; the "on premises" radius when the rule uses on-property
// language; else the "close by" radius.
export function effectiveBuffer(m) {
  if (m.buffer_specified) return m.buffer_m;
  if (m.rule_heuristic_m) return state.ONPREM_M;
  return state.CLOSE_M;
}

export function applyBuffer(closeM, onpremM) {
  state.CLOSE_M = closeM;
  state.ONPREM_M = onpremM !== undefined ? onpremM : state.ONPREM_M;

  const statusById = {};
  for (const f of state.CAMS.features) {
    const p = f.properties;
    if (p.status !== "possible_exclusion") {
      const within = (state.ALL_MATCHES[p.camera_id] || []).filter(
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
  state.CAM_STATUS = statusById;
  state.CONES_STALE = true;
  const camSrc = map.getSource("cameras");
  if (camSrc) camSrc.setData(state.CAMS);
  syncCones();
  updateStats();
}

// Cones follow camera status. They load on first approach to CONE_MIN_ZOOM
// and are only re-pushed to the map while in range.
export async function syncCones() {
  if (!map.getSource("cones") || map.getZoom() < CONE_LOAD_ZOOM) return;
  if (!state.CONES_LOADED) {
    if (state.CONES_LOADING) return;
    state.CONES_LOADING = true;
    try {
      state.CONES = await fetchJSON("data/view_cones.geojson");
      state.CONES_LOADED = true;
    } catch (err) {
      console.error("View cones failed to load:", err);
      return;
    } finally {
      state.CONES_LOADING = false;
    }
  }
  if (!state.CONES_STALE) return;
  for (const f of state.CONES.features) {
    f.properties.status = state.CAM_STATUS[f.properties.camera_id] || f.properties.status;
  }
  map.getSource("cones").setData(state.CONES);
  state.CONES_STALE = false;
}

export function updateStats() {
  const count = (s) => (state.CAMS
    ? state.CAMS.features.filter((f) => f.properties.status === s).length
    : (s === "flagged" ? state.META.flagged_count : state.META.guideline_count || 0));
  const flagged = count("flagged");
  const guide = count("guideline");
  const gStates = state.META.guideline_states || [];
  document.getElementById("stats").textContent =
    `${flagged.toLocaleString()} flagged / ` +
    `${state.META.camera_count.toLocaleString()} cameras · ${state.META.rule_states.join(", ")}` +
    (gStates.length
      ? ` · ${guide.toLocaleString()} spirit-of (${gStates.join(", ")})`
      : "");
}
