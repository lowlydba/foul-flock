/* Small helpers shared across modules. */

export async function fetchJSON(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  return res.json();
}

// MapLibre hands null feature properties back as the string "null".
export function cleanProps(props) {
  return Object.fromEntries(
    Object.entries(props).map(([k, v]) => [k, v === "null" ? null : v])
  );
}

// A state's public summary; older data files call it note.
export function stateSummary(p) {
  const s = p.summary ?? p.note;
  return s && s !== "null" ? s : "";
}

// Hostname of an http(s) source URL, or "" for anything else.
export function sourceHost(url) {
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:" ? u.hostname : "";
  } catch {
    return "";
  }
}

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

// ISO date for display, prefixed "about" when the rule marks it approximate.
export function fmtDate(rule) {
  const d = String(rule.effective_date || "?").slice(0, 10);
  return rule.date_approximate ? `about ${d}` : d;
}

// "today", "12 days ago", "7 months ago", "3 years ago" for an ISO date.
export function ageLabel(isoDate) {
  const then = new Date(`${isoDate}T00:00:00Z`);
  const days = Math.floor((Date.now() - then.getTime()) / 86400000);
  if (Number.isNaN(days) || days < 0) return "date unavailable";
  if (days < 1) return "today";
  if (days < 60) return `${days} day${days === 1 ? "" : "s"} ago`;
  if (days < 730) return `${Math.round(days / 30.44)} months ago`;
  return `${Math.floor(days / 365.25)} years ago`;
}
