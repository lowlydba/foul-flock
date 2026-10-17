/* The camera detail panel and the draft inquiry letter. */

import { CATEGORY_LABELS } from "./config.js";
import { state } from "./state.js";
import { esc, fmtDate, ageLabel, cleanProps } from "./util.js";
import { showDetail } from "./panels.js";
import { effectiveBuffer } from "./radius.js";

export function openDetail(feature) {
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
    const exclRule = state.META.rules.find(
      (r) => r.state_code === p.state_code && r.statutory_exclusions
    );
    html += `<h3>Why it's flagged</h3>`;
    for (const m of statutoryMatches) {
      const rule = state.RULES_BY_ID[m.rule_id] || {};
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
    const stateRules = state.META.rules.filter((r) => r.state_code === p.state_code);
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
  const r = state.RULES_BY_ID[p.road_rule_id] || {};
  return String(r.grandfather_cutoff || "pause").slice(0, 10);
}

// Detail body for "spirit-of" cameras: executive actions or pending
// provisions, never a violation claim and never a report letter.
function guidelineHtml(p, matches) {
  let html = `<div class="guideline-banner"><b>Not a statute, not a violation.</b>
    A best-effort "spirit-of" view of an executive action or a provision that
    is not fully operative yet. Verify everything before acting.</div>`;
  const road = state.RULES_BY_ID[p.road_rule_id];
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
      const rule = state.RULES_BY_ID[m.rule_id] || {};
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
  const rule = state.RULES_BY_ID[m.rule_id] || {};
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
