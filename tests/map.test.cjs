const assert = require("node:assert/strict");
const { before, after, beforeEach, afterEach, test } = require("node:test");
const { readFile } = require("node:fs/promises");
const path = require("node:path");
const puppeteer = require("puppeteer");
const pa11y = require("pa11y");

const scheme = process.env.A11Y_SCHEME || "light";
assert.ok(["light", "dark"].includes(scheme), `Unsupported A11Y_SCHEME: ${scheme}`);
const base = "http://127.0.0.1:8081";
let browser;
let page;

before(async () => {
  browser = await puppeteer.launch({ args: ["--no-sandbox"] });
});
after(async () => {
  if (browser) await browser.close();
});
beforeEach(async () => {
  page = await browser.newPage();
  await page.setViewport({ width: 1280, height: 720 });
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: scheme }]);
  await page.setRequestInterception(true);
  page.on("request", async (request) => {
    const url = new URL(request.url());
    if (url.origin === base && url.pathname.startsWith("/data/")) {
      try {
        const body = await readFile(path.join(__dirname, "fixtures", "data", path.basename(url.pathname)));
        await request.respond({ status: 200, contentType: "application/json", body });
      } catch (err) {
        await request.abort();
        throw err;
      }
    } else {
      await request.continue();
    }
  });
  await page.goto(base);
  await page.waitForFunction(() =>
    Boolean(window.__ff) && !/loading/i.test(document.getElementById("stats").textContent) &&
    window.__ff.map.isStyleLoaded()
  );
  // Basemap tiles come from the network, so map.loaded() can stall on CI; halt the intro fitBounds instead.
  await page.evaluate(() => window.__ff.map.stop());
});
afterEach(async () => {
  if (page) await page.close();
});

for (const fontScale of [1, 2]) {
  test(`popup close control stays compact at ${fontScale * 100}% text size (${scheme})`, async () => {
    const layout = await page.evaluate((scale) => {
      window.__ff.map.stop();
      new maplibregl.Popup()
        .setLngLat(window.__ff.map.getCenter())
        .setHTML("<b>Midlothian Middle School</b><br>Protected category: School")
        .addTo(window.__ff.map);
      const popup = document.querySelector(".maplibregl-popup-content");
      const close = popup.querySelector("button");
      const originalContentSize = parseFloat(getComputedStyle(popup).fontSize);
      const originalGlyphSize = parseFloat(getComputedStyle(close).fontSize);
      if (scale !== 1) {
        popup.style.fontSize = `${originalContentSize * scale}px`;
        close.style.fontSize = `${originalGlyphSize * scale}px`;
      }
      const style = getComputedStyle(close);
      const box = close.getBoundingClientRect();
      const range = document.createRange();
      range.selectNodeContents(popup.querySelector("b"));
      return {
        originalContentSize, originalGlyphSize,
        contentSize: getComputedStyle(popup).fontSize,
        textSize: getComputedStyle(popup.querySelector("b")).fontSize,
        glyphSize: style.fontSize,
        width: box.width, height: box.height, left: box.left,
        padding: style.padding, margin: style.margin,
        background: style.backgroundColor, color: style.color,
        textRight: Math.max(...Array.from(range.getClientRects(), (rect) => rect.right)),
      };
    }, fontScale);
    assert.equal(layout.originalContentSize, 12.5);
    assert.equal(layout.originalGlyphSize, 20);
    assert.equal(layout.contentSize, `${12.5 * fontScale}px`);
    assert.equal(layout.textSize, `${12.5 * fontScale}px`);
    assert.equal(layout.glyphSize, `${20 * fontScale}px`);
    assert.equal(layout.width, 24);
    assert.equal(layout.height, 24);
    assert.equal(layout.padding, "0px");
    assert.equal(layout.margin, "0px");
    assert.equal(layout.background, "rgba(0, 0, 0, 0)");
    assert.equal(layout.color, "rgb(31, 35, 40)");
    assert.ok(layout.textRight < layout.left, "popup text must not overlap the close control");

    const selector = ".maplibregl-popup-close-button";
    await page.hover(selector);
    await page.waitForFunction((selector) =>
      getComputedStyle(document.querySelector(selector)).backgroundColor === "rgba(0, 0, 0, 0.05)", {}, selector);
    await page.focus(selector);
    await page.keyboard.press("Tab");
    await page.keyboard.down("Shift");
    await page.keyboard.press("Tab");
    await page.keyboard.up("Shift");
    assert.ok(await page.$eval(selector, (el) => el === document.activeElement));
    assert.equal(await page.$eval(selector, (el) => getComputedStyle(el).outlineStyle), "solid");

    const results = await pa11y(base, {
      browser, page, ignoreUrl: true,
      runners: ["axe"], standard: "WCAG2AA",
      rootElement: ".maplibregl-popup-content",
      levelCapWhenNeedsReview: "warning",
    });
    assert.deepEqual(results.issues.filter((issue) => issue.type === "error"), []);

    await page.keyboard.press("Enter");
    assert.equal(await page.$(".maplibregl-popup-content"), null);
    const tooltip = await page.evaluate(() => {
      new maplibregl.Popup({ closeButton: false, className: "state-tip" })
        .setLngLat(window.__ff.map.getCenter())
        .setText("Reviewed, no siting law found")
        .addTo(window.__ff.map);
      const el = document.querySelector(".state-tip .maplibregl-popup-content");
      return { padding: getComputedStyle(el).padding, buttons: el.querySelectorAll("button").length };
    });
    assert.equal(tooltip.padding, "6px 10px");
    assert.equal(tooltip.buttons, 0);
  });
}

for (const viewport of [{ width: 1280, height: 720 }, { width: 390, height: 844 }]) {
  test(`zoom-out stops at the US footprint at ${viewport.width}px wide (${scheme})`, async () => {
    await page.setViewport(viewport);
    const result = await page.evaluate(() => {
      const map = window.__ff.map;
      map.stop();
      map.resize();
      const bounds = [[-188, 18.5], [-66.5, 72]];
      const camera = map.cameraForBounds(bounds);
      const minZoom = map.getMinZoom();
      map.jumpTo({ center: camera.center, zoom: -10 });
      const zoom = map.getZoom();
      const visible = bounds.map((point) => {
        const p = map.project(point);
        return p.x >= -1 && p.x <= map.getContainer().clientWidth + 1 &&
          p.y >= -1 && p.y <= map.getContainer().clientHeight + 1;
      });
      map.zoomOut({ duration: 0 });
      const afterZoomOut = map.getZoom();
      map.jumpTo({ zoom: minZoom + 2 });
      return { minZoom, expected: camera.zoom, zoom, afterZoomOut, visible, zoomedIn: map.getZoom() };
    });
    assert.ok(result.minZoom > 0);
    for (const zoom of [result.expected, result.zoom, result.afterZoomOut]) {
      assert.ok(Math.abs(zoom - result.minZoom) < 1e-6);
    }
    assert.deepEqual(result.visible, [true, true]);
    assert.ok(Math.abs(result.zoomedIn - result.minZoom - 2) < 1e-6);
  });
}
