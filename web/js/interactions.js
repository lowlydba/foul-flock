/* Pointer and touch interactions: camera taps, state tooltips and popups,
   place popups. */

import { CATEGORY_LABELS, CAM_LAYERS } from "./config.js";
import { map } from "./map.js";
import { state } from "./state.js";
import { esc, stateSummary, sourceHost } from "./util.js";
import { isTouch, TAP_TOLERANCE_PX } from "./layout.js";
import { openDetail } from "./detail.js";

// Nearest camera within a fingertip radius (touch) or exactly under the
// pointer (mouse).
export function cameraNear(point) {
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

export function stateStatusHtml(p) {
  if (p.law_status === "guideline") {
    const gl = (state.META.guideline_rules || []).filter((r) => r.state_code === p.state_code);
    const items = gl.map((r) => `<li><b>${esc(r.title)}</b>${r.distance_tbd ? " <b>(distance TBD)</b>" : ""}<br>${esc(r.summary)}</li>`);
    const host = sourceHost(p.source_url);
    return `<b>${esc(p.name)}</b><br>No siting statute. <b>Spirit-of guideline</b>
      (not a legal threshold):<ul class="pop-list">${items.join("")}</ul>
      <span class="pop-fine">${esc(stateSummary(p))}
      ${p.last_verified ? `Last checked ${esc(p.last_verified)}.` : ""}</span>${host ? `<br><span class="pop-fine">Source: <a href="${esc(p.source_url)}" target="_blank" rel="noopener noreferrer">${esc(host)}</a></span>` : ""}`;
  }
  if (p.law_status === "siting_law") {
    const rules = state.META.rules.filter((r) => r.state_code === p.state_code);
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

export function registerInteractions() {
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

  if (map.getLayer("places-pts")) map.on("click", "places-pts", (e) => {
    if (cameraNear(e.point)) return;
    const p = e.features[0].properties;
    const cat = CATEGORY_LABELS[p.category] || p.category;
    const rel = p.religion && p.religion !== "null" ? ` (${p.religion})` : "";
    new maplibregl.Popup()
      .setLngLat(e.lngLat)
      .setHTML(`<b>${esc(p.name)}</b><br>Protected category: ${esc(cat)}${esc(rel)}`)
      .addTo(map);
  });
}
