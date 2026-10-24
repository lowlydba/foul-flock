/* About dialog (with the #about deep link), the detail close button, and
   global keyboard handling (Escape, Tab trap). */

import { isCompact } from "./layout.js";
import { legendEl, detailEl, layersPanel, setLayersOpen, setLegendOpen, closeDetail } from "./panels.js";

const aboutEl = document.getElementById("about");

document.getElementById("detail-close").addEventListener("click", () =>
  closeDetail({ restoreFocus: true })
);

// First-visit orientation: shown until dismissed or a camera is opened.
const hintEl = document.getElementById("hint");
const HINT_KEY = "ff-hint-dismissed";
const hintSeen = () => {
  try { return localStorage.getItem(HINT_KEY) === "1"; } catch { return false; }
};
export function dismissHint() {
  hintEl.hidden = true;
  try { localStorage.setItem(HINT_KEY, "1"); } catch { /* storage unavailable */ }
}
if (!hintSeen()) hintEl.hidden = false;
document.getElementById("hint-close").addEventListener("click", dismissHint);
new MutationObserver(() => {
  if (!detailEl.classList.contains("hidden")) dismissHint();
}).observe(detailEl, { attributes: true, attributeFilter: ["class"] });

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
    const items = [...aboutEl.querySelectorAll("a[href], button")]
      .filter((el) => el.offsetParent !== null);
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
