// WCAG 2.1 A/AA checks via axe-core, in both light and dark color schemes
// (see playwright.config.js projects). The map canvas itself is out of
// axe's scope (raster), so these assert the HTML chrome: topbar, legend,
// detail panel, report drafting UI, and the About modal.
const { test, expect } = require("@playwright/test");
const { AxeBuilder } = require("@axe-core/playwright");

const AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];

async function scan(page) {
  const results = await new AxeBuilder({ page })
    .withTags(AXE_TAGS)
    // Raster map canvas: nothing for axe to read there.
    .exclude(".maplibregl-canvas-container")
    .analyze();
  return results.violations;
}

function pretty(violations) {
  return violations
    .map(
      (v) =>
        `${v.id} (${v.impact}): ${v.help}\n` +
        v.nodes.map((n) => `  - ${n.target.join(" ")}`).join("\n")
    )
    .join("\n\n");
}

async function loadApp(page) {
  await page.goto("/");
  // Stats line flips from "loading..." once data + rules are in.
  await expect(page.locator("#stats")).not.toContainText(/loading/i, {
    timeout: 30_000,
  });
}

test("base map chrome has no WCAG A/AA violations", async ({ page }) => {
  await loadApp(page);
  const violations = await scan(page);
  expect(violations, pretty(violations)).toEqual([]);
});

test("legend method notes expanded", async ({ page }) => {
  await loadApp(page);
  await page.locator("#method-notes summary").click();
  const violations = await scan(page);
  expect(violations, pretty(violations)).toEqual([]);
});

test("heuristic radius slider updates results", async ({ page }) => {
  await loadApp(page);
  const before = await page.textContent("#stats");
  await page.evaluate(() => {
    const s = document.getElementById("buffer-slider");
    s.value = s.min;
    s.dispatchEvent(new Event("input"));
  });
  await expect(page.locator("#buffer-value")).toContainText("50 m");
  const after = await page.textContent("#stats");
  expect(after).not.toEqual(before);
  const violations = await scan(page);
  expect(violations, pretty(violations)).toEqual([]);
});

test("on-premises radius slider updates results", async ({ page }) => {
  await loadApp(page);
  const before = await page.textContent("#stats");
  await page.evaluate(() => {
    const s = document.getElementById("onprem-slider");
    s.value = s.max;
    s.dispatchEvent(new Event("input"));
  });
  await expect(page.locator("#onprem-value")).toContainText("150 m");
  const after = await page.textContent("#stats");
  expect(after).not.toEqual(before);
  const violations = await scan(page);
  expect(violations, pretty(violations)).toEqual([]);
});

test("faq page has no WCAG A/AA violations", async ({ page }) => {
  await page.goto("/faq.html");
  await expect(page.locator("h1")).toContainText("How & why");
  const violations = await new AxeBuilder({ page })
    .withTags(AXE_TAGS)
    .analyze()
    .then((r) => r.violations);
  expect(violations, pretty(violations)).toEqual([]);
});

test("about modal", async ({ page }) => {
  await loadApp(page);
  await page.locator("#about-btn").click();
  await expect(page.locator("#about")).toBeVisible();
  const violations = await scan(page);
  expect(violations, pretty(violations)).toEqual([]);
});

test("flagged camera detail panel and report letter", async ({ page }) => {
  await loadApp(page);
  // Deterministically open the detail panel for the first flagged camera
  // via the window.__ff test hook instead of clicking map pixels.
  const opened = await page.evaluate(async () => {
    const res = await fetch("data/cameras.geojson");
    const gj = await res.json();
    const flagged = gj.features.find((f) => f.properties.status === "flagged");
    if (!flagged) return false;
    window.__ff.openDetail(flagged);
    return true;
  });
  expect(opened, "fixture data must include a flagged camera").toBe(true);
  await expect(page.locator("#detail")).toBeVisible();

  let violations = await scan(page);
  expect(violations, pretty(violations)).toEqual([]);

  // Draft-report flow, textarea and action buttons included.
  await page.locator(".report-btn").click();
  await expect(page.locator(".report-box textarea")).toBeVisible();
  violations = await scan(page);
  expect(violations, pretty(violations)).toEqual([]);
});
