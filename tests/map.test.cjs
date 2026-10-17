const assert = require("node:assert/strict");
const { before, after, beforeEach, afterEach, test } = require("node:test");
const { readFile } = require("node:fs/promises");
const path = require("node:path");
const puppeteer = require("puppeteer");
const pa11y = require("pa11y");

const base = "http://127.0.0.1:8081";
const HARD_TIMEOUT = { timeout: 30000 };
const blankTile = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=",
  "base64"
);
let browser;
let page;
let requested = [];
// Per-test tweaks to what the fixture server returns: files to 404 and
// edits to rules.json.
let fixtureOverrides = { missing: [], rules: null };

before(async () => {
  browser = await puppeteer.launch({ args: ["--no-sandbox"] });
}, HARD_TIMEOUT);
after(async () => {
  if (browser) await browser.close();
});
beforeEach(async () => {
  fixtureOverrides = { missing: [], rules: null };
  page = await browser.newPage();
  page.setDefaultTimeout(15000);
  await page.setViewport({ width: 1280, height: 720 });
  await page.setRequestInterception(true);
  page.on("request", async (request) => {
    const url = new URL(request.url());
    if (url.origin === base && url.pathname.startsWith("/data/")) {
      const name = path.basename(url.pathname);
      if (fixtureOverrides.missing.includes(name)) {
        await request.respond({ status: 404, contentType: "text/plain", body: "not found" });
        return;
      }
      try {
        let body = await readFile(path.join(__dirname, "fixtures", "data", name));
        if (name === "rules.json" && fixtureOverrides.rules) {
          body = Buffer.from(JSON.stringify(fixtureOverrides.rules(JSON.parse(body))));
        }
        const range = /^bytes=(\d+)-(\d+)$/.exec(request.headers().range || "");
        if (range) {
          // PMTiles are read with HTTP range requests.
          const [start, end] = [Number(range[1]), Math.min(Number(range[2]), body.length - 1)];
          await request.respond({
            status: 206,
            contentType: "application/octet-stream",
            headers: { "content-range": `bytes ${start}-${end}/${body.length}`, "accept-ranges": "bytes" },
            body: body.subarray(start, end + 1),
          });
        } else {
          await request.respond({
            status: 200,
            contentType: url.pathname.endsWith(".pmtiles") ? "application/octet-stream" : "application/json",
            headers: { "accept-ranges": "bytes" },
            body,
          });
        }
      } catch (err) {
        await request.abort();
        throw err;
      }
    } else if (url.hostname === "tile.openstreetmap.org") {
      // Stub basemap tiles: CI shouldn't depend on (or load) the OSM tile servers.
      await request.respond({ status: 200, contentType: "image/png", body: blankTile });
    } else {
      await request.continue();
    }
  });
  const errors = [];
  requested = [];
  page.on("request", (r) => requested.push(r.url()));
  page.on("pageerror", (err) => errors.push(err.message));
  page.on("requestfailed", (r) => errors.push(`request failed: ${r.url()}`));
  await page.goto(base);
  try {
    await page.waitForFunction(() =>
      Boolean(window.__ff) && !/loading/i.test(document.getElementById("stats").textContent)
    );
  } catch (err) {
    const stats = await page.evaluate(() => document.getElementById("stats")?.textContent);
    throw new Error(`Map never became ready (stats: ${stats}; ${errors.join("; ") || "no page errors"})`, { cause: err });
  }
  // Tests only need the app and fixture data, not painted tiles; map.loaded() is flaky in headless CI.
  await page.evaluate(() => window.__ff.map.stop());
}, HARD_TIMEOUT);
afterEach(async () => {
  if (page) await page.close();
});

for (const fontScale of [1, 2]) {
  test(`popup close control stays compact at ${fontScale * 100}% text size`, HARD_TIMEOUT, async () => {
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
    const results = await pa11y(base, {
      browser, page, ignoreUrl: true,
      runners: ["axe"], standard: "WCAG2AA",
      rootElement: ".maplibregl-popup-content",
      levelCapWhenNeedsReview: "warning",
    });
    assert.deepEqual(results.issues.filter((issue) => issue.type === "error"), []);

    await page.click(selector);
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

test("detail panel shows when the camera was last edited in OSM, and the letter cites it", HARD_TIMEOUT, async () => {
  await page.evaluate(async () => {
    const gj = await (await fetch("data/cameras.geojson")).json();
    window.__ff.openDetail(gj.features.find((f) => f.properties.last_edited));
  });
  const row = await page.$eval("#detail .kv time", (el) => ({
    text: el.textContent, datetime: el.getAttribute("datetime"),
    line: el.parentElement.textContent.replace(/\s+/g, " ").trim(),
  }));
  assert.equal(row.datetime, "2025-03-14");
  assert.match(row.line, /^Last edited: 2025-03-14 \(\d+ (months|years) ago\)$/);
  await page.click("#draft-report");
  assert.match(await page.$eval("#report-text", (el) => el.value),
    /Camera source: .+node\/\d+ \(© OpenStreetMap contributors\), record last edited 2025-03-14\./);
});

test("guideline camera detail panel has a banner, no report letter, and no a11y errors", HARD_TIMEOUT, async () => {
  const opened = await page.evaluate(async () => {
    const gj = await (await fetch("data/cameras.geojson")).json();
    const hits = gj.features.filter((f) => f.properties.status === "guideline");
    hits.forEach((f) => window.__ff.openDetail(f));
    return hits.length;
  });
  assert.ok(opened > 1, "fixture data must include guideline cameras");
  assert.ok(await page.$(".guideline-banner"));
  assert.equal((await page.$$(".report-btn")).length, 0);
  const results = await pa11y(base, {
    browser, page, ignoreUrl: true,
    runners: ["axe"], standard: "WCAG2AA",
    levelCapWhenNeedsReview: "warning",
  });
  assert.deepEqual(results.issues.filter((issue) => issue.type === "error"), []);
});

for (const viewport of [{ width: 1280, height: 720 }, { width: 390, height: 844 }]) {
  test(`zoom-out stops at the US footprint at ${viewport.width}px wide`, HARD_TIMEOUT, async () => {
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

const MODES = [
  { name: "phone", viewport: { width: 390, height: 844, isMobile: true, hasTouch: true, deviceScaleFactor: 2 } },
  { name: "tablet", viewport: { width: 820, height: 1180, isMobile: true, hasTouch: true, deviceScaleFactor: 2 } },
];

async function reloadAs(viewport) {
  await page.setViewport(viewport);
  await page.reload();
  await page.waitForFunction(() =>
    Boolean(window.__ff) && !/loading/i.test(document.getElementById("stats").textContent)
  );
  await page.evaluate(() => window.__ff.map.stop());
}

async function openGuidelineCamera() {
  await page.evaluate(async () => {
    const gj = await (await fetch("data/cameras.geojson")).json();
    window.__ff.openDetail(gj.features.find((f) => f.properties.status === "flagged"));
  });
}

const box = (selector) => page.evaluate((sel) => {
  const el = document.querySelector(sel);
  const r = el.getBoundingClientRect();
  return { top: r.top, left: r.left, right: r.right, bottom: r.bottom,
    width: r.width, height: r.height, display: getComputedStyle(el).display };
}, selector);

test("desktop keeps the always-visible legend and no legend toggle", HARD_TIMEOUT, async () => {
  assert.equal((await box("#legend-toggle")).display, "none");
  assert.equal((await box("#legend")).display, "block");
});

test("view cones load only near their zoom", HARD_TIMEOUT, async () => {
  const coneRequests = () => requested.filter((u) => u.endsWith("/view_cones.geojson")).length;
  assert.equal(coneRequests(), 0, "cones must not load with the national view");

  await page.evaluate(() => window.__ff.map.jumpTo({ zoom: 8 }));
  await new Promise((r) => setTimeout(r, 300));
  assert.equal(coneRequests(), 0, "cones must not load at state zoom");

  await page.evaluate(() => window.__ff.map.jumpTo({ zoom: 12.5 }));
  await page.waitForFunction(() => window.__ff.conesLoaded);
  assert.equal(coneRequests(), 1);
  await page.evaluate(() => window.__ff.map.jumpTo({ zoom: 13.5 }));
  await new Promise((r) => setTimeout(r, 300));
  assert.equal(coneRequests(), 1, "cones are fetched once");
});

test("'Nothing found' cameras are off by default and read from static tiles", HARD_TIMEOUT, async () => {
  const layer = (id) => page.evaluate((i) => ({
    visibility: window.__ff.map.getLayoutProperty(i, "visibility"),
    minzoom: window.__ff.map.getLayer(i).minzoom,
  }), id);
  const status = () => page.$eval("#clear-status", (el) => el.textContent);
  const tileFeatures = (source, sourceLayer) => page.evaluate(
    (s, l) => window.__ff.map.querySourceFeatures(s, { sourceLayer: l }).length, source, sourceLayer);
  const camera = [-76.6346473, 36.9682134]; // fixture camera with no candidate place

  assert.equal(await page.$eval("#clear-toggle", (el) => el.checked), false);
  assert.equal(await page.$eval("#clear-toggle", (el) => el.disabled), false);
  assert.equal(await page.$eval("#clear-toggle", (el) => el.getAttribute("role")), "switch");
  for (const id of ["cams-clear", "cams-clear-far", "view-cones-clear"]) {
    assert.equal((await layer(id)).visibility, "none", `${id} hidden by default`);
  }
  assert.equal((await layer("cams-clear-far")).minzoom, 6);

  // On, but still national: the zoom hint shows.
  await page.click("#layers-btn");
  await page.click("#clear-toggle");
  assert.equal((await layer("cams-clear-far")).visibility, "visible");
  assert.match(await status(), /zoom in/i);

  // State zoom: tiles for the view load and the status updates.
  await page.evaluate((c) => window.__ff.map.jumpTo({ center: c, zoom: 8 }), camera);
  await page.waitForFunction(() => /Showing/.test(document.getElementById("clear-status").textContent));
  await page.waitForFunction(() => window.__ff.map.querySourceFeatures(
    "clear-cams", { sourceLayer: "clear_cameras" }).length > 0);
  const props = await page.evaluate(() => window.__ff.map.querySourceFeatures(
    "clear-cams", { sourceLayer: "clear_cameras" })[0].properties);
  assert.equal(props.camera_id, "osm-node-9451456653");
  assert.equal(props.status, "checked_clear");
  assert.match(props.osm_url, /openstreetmap\.org\/node\/9451456653$/);
  assert.equal(props.brand, "Flock Safety");

  // At state zoom the tile geometry is rounded to tens of meters, but the
  // detail panel must still show the camera's exact position.
  await page.evaluate(() => new Promise((r) => {
    window.__ff.map.once("idle", r);
    window.__ff.map.triggerRepaint(); // idle fires after the next frame even if already settled
  }));
  const lowPt = await page.evaluate((c) => window.__ff.map.project(c), camera);
  await page.mouse.click(lowPt.x, lowPt.y);
  await page.waitForSelector("#detail:not(.hidden)");
  assert.match(await page.$eval("#detail .kv", (el) => el.textContent),
    /Location:\s*36\.96821, -76\.63465/);
  await page.click("#detail-close");

  // Street zoom: the camera's view cone tile loads too.
  await page.evaluate((c) => window.__ff.map.jumpTo({ center: c, zoom: 14 }), camera);
  await page.waitForFunction(() => window.__ff.map.querySourceFeatures(
    "clear-cones", { sourceLayer: "clear_cones" }).length > 0);

  // Clicking a tile-backed camera opens the same detail panel.
  await page.evaluate(() => new Promise((r) => window.__ff.map.once("idle", r)));
  const pt = await page.evaluate((c) => window.__ff.map.project(c), camera);
  await page.mouse.click(pt.x, pt.y);
  await page.waitForSelector("#detail:not(.hidden)");
  assert.match(await page.$eval("#detail", (el) => el.textContent), /nothing found/i);
  assert.match(await page.$eval("#detail", (el) => el.textContent),
    /Last edited:\s*2026-01-05 \(.+\)/);
  assert.match(await page.$eval("#detail .kv", (el) => el.textContent),
    /Location:\s*36\.96821, -76\.63465/);

  await page.click("#layers-btn");
  await page.click("#clear-toggle");
  assert.equal((await layer("cams-clear-far")).visibility, "none");
  assert.equal(await status(), "");
});

test("optional archives the build omitted are never requested", HARD_TIMEOUT, async () => {
  fixtureOverrides = {
    missing: ["places.pmtiles", "clear_cones.pmtiles"],
    rules: (r) => ({ ...r, place_count: 0, clear_cone_count: 0 }),
  };
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("response", (r) => { if (r.status() >= 400) errors.push(`${r.status()} ${r.url()}`); });
  await page.reload();
  await page.waitForFunction(() => Boolean(window.__ff) &&
    !/loading/i.test(document.getElementById("stats").textContent));
  const has = (id) => page.evaluate((i) => Boolean(window.__ff.map.getLayer(i)), id);
  assert.equal(await has("places-pts"), false);
  assert.equal(await has("view-cones-clear"), false);
  assert.equal(await has("cams-clear-far"), true, "clear cameras are still in the build");

  await page.click("#layers-btn");
  await page.click("#clear-toggle");
  await page.evaluate(() => window.__ff.map.jumpTo({ center: [-76.63, 36.97], zoom: 14 }));
  await new Promise((r) => setTimeout(r, 800));
  assert.deepEqual(errors, []);
  assert.ok(!requested.slice(requested.lastIndexOf(base + "/")).some(
    (u) => /places\.pmtiles|clear_cones\.pmtiles/.test(u)), "no request for omitted archives");
});

test("Layers control is a labelled disclosure with a real switch, and the legend stays a pure key", HARD_TIMEOUT, async () => {
  assert.equal(await page.$("#legend input[type=checkbox]"), null, "legend holds no toggles");
  assert.equal(await page.$eval("#layers-panel", (el) => el.hidden), true);
  assert.equal(await page.$eval("#layers-btn", (el) => el.getAttribute("aria-expanded")), "false");

  await page.click("#layers-btn");
  assert.equal(await page.$eval("#layers-panel", (el) => el.hidden), false);
  assert.equal(await page.$eval("#layers-btn", (el) => el.getAttribute("aria-expanded")), "true");
  const name = await page.$eval("#clear-toggle", (el) => el.labels[0].textContent.replace(/\s+/g, " ").trim());
  assert.match(name, /^Cameras with nothing found/);
  const panel = await box("#layers-panel");
  const topbar = await box("#topbar");
  assert.ok(panel.top >= topbar.bottom, "panel sits below the topbar");

  await page.keyboard.press("Escape");
  assert.equal(await page.$eval("#layers-panel", (el) => el.hidden), true);
  assert.equal(await page.evaluate(() => document.activeElement.id), "layers-btn");

  await page.click("#layers-btn");
  await page.mouse.click(700, 600);
  assert.equal(await page.$eval("#layers-panel", (el) => el.hidden), true, "outside click closes it");
});

test("radius slider changes are debounced into a single data update", HARD_TIMEOUT, async () => {
  const calls = await page.evaluate(async () => {
    const src = window.__ff.map.getSource("cameras");
    let n = 0;
    const orig = src.setData.bind(src);
    src.setData = (d) => { n += 1; return orig(d); };
    const slider = document.getElementById("buffer-slider");
    for (const v of [150, 200, 250, 300]) {
      slider.value = v;
      slider.dispatchEvent(new Event("input", { bubbles: true }));
    }
    await new Promise((r) => setTimeout(r, 500));
    return n;
  });
  assert.equal(calls, 1);
});

for (const viewport of [{ width: 1280, height: 500 }, { width: 390, height: 844, isMobile: true, hasTouch: true }]) {
  test(`About close button stays visible while scrolling at ${viewport.width}px`, HARD_TIMEOUT, async () => {
    await reloadAs(viewport);
    await page.click("#about-btn");
    const result = await page.evaluate(() => {
      const body = document.querySelector("#about .modal-body");
      body.scrollTop = body.scrollHeight;
      const r = document.querySelector("#about .modal-close").getBoundingClientRect();
      const card = document.querySelector("#about .modal-card").getBoundingClientRect();
      return { scrollable: body.scrollHeight > body.clientHeight, scrolled: body.scrollTop,
        closeTop: r.top, cardTop: card.top, closeBottom: r.bottom, cardBottom: card.bottom };
    });
    assert.ok(result.scrollable && result.scrolled > 0, "dialog content should scroll");
    assert.ok(result.closeTop >= result.cardTop && result.closeBottom <= result.cardBottom,
      "close button must remain inside the card after scrolling");
  });
}

test("old How & why link lands on the About dialog via #about", HARD_TIMEOUT, async () => {
  assert.equal(await page.$("#faq-link"), null, "no separate How & why link");
  await page.goto(`${base}/#about`);
  await page.waitForFunction(() => !document.getElementById("about").classList.contains("hidden"));
  const headings = await page.$$eval("#about h3", (els) => els.map((e) => e.textContent));
  for (const h of ["How do I use it?", "What do the radius sliders do?", "Where does the data come from?"]) {
    assert.ok(headings.some((t) => t.includes(h)), `missing section: ${h}`);
  }
  await page.keyboard.press("Escape");
  assert.equal(await page.evaluate(() => location.hash), "");
});

for (const mode of MODES) {
  const { width, height } = mode.viewport;

  test(`${mode.name}: page fits the viewport and topbar targets are 44px`, HARD_TIMEOUT, async () => {
    await reloadAs(mode.viewport);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    assert.ok(overflow <= 0, `horizontal overflow of ${overflow}px`);
    for (const sel of ["#about-btn", "#legend-toggle"]) {
      const b = await box(sel);
      assert.ok(b.height >= 44 && b.width >= 44, `${sel} is ${b.width}x${b.height}`);
      assert.ok(b.right <= width && b.left >= 0, `${sel} is off-screen`);
    }
    const topbar = await box("#topbar");
    assert.ok(topbar.bottom < height / 4, "topbar should not dominate the viewport");
  });

  test(`${mode.name}: legend collapses behind a toggle with focus management`, HARD_TIMEOUT, async () => {
    await reloadAs(mode.viewport);
    assert.equal((await box("#legend")).display, "none");
    assert.equal(await page.$eval("#legend-toggle", (el) => el.getAttribute("aria-expanded")), "false");

    await page.tap("#legend-toggle");
    assert.equal(await page.$eval("#legend-toggle", (el) => el.getAttribute("aria-expanded")), "true");
    const legend = await box("#legend");
    assert.notEqual(legend.display, "none");
    assert.ok(legend.left >= 0 && legend.right <= width && legend.bottom <= height && legend.top >= 0);
    assert.equal(await page.evaluate(() => document.activeElement.id), "legend-close");
    const close = await box("#legend-close");
    assert.ok(close.width >= 44 && close.height >= 44);
    const slider = await box("#buffer-slider");
    assert.ok(slider.height >= 24, "slider needs a touch-sized hit area");

    await page.keyboard.press("Escape");
    assert.equal((await box("#legend")).display, "none");
    assert.equal(await page.evaluate(() => document.activeElement.id), "legend-toggle");
  });

  test(`${mode.name}: detail panel is a ${mode.name === "phone" ? "bottom sheet" : "side drawer"} inside the viewport`, HARD_TIMEOUT, async () => {
    await reloadAs(mode.viewport);
    await openGuidelineCamera();
    const d = await box("#detail");
    const topbar = await box("#topbar");
    assert.ok(d.top >= topbar.bottom - 1, "detail must not cover the topbar");
    assert.ok(d.left >= 0 && d.right <= width && d.bottom <= height);
    // The selected camera is centered below the topbar, not under it.
    await page.waitForFunction((min) => window.__ff.map.getPadding().top >= min, {}, Math.floor(topbar.bottom));
    if (mode.name === "phone") {
      assert.equal(Math.round(d.width), width);
      assert.equal(Math.round(d.bottom), height);
      assert.ok(d.height <= height * 0.65, "sheet must leave map visible");
    } else {
      assert.ok(d.width <= width / 2 && d.left > width / 2 - 1, "drawer sits on the right half");
    }
    const close = await box("#detail-close");
    assert.ok(close.width >= 44 && close.height >= 44);
    assert.equal(await page.evaluate(() => document.activeElement.id), "detail-title");

    // Legend and detail are mutually exclusive in compact modes.
    await page.tap("#legend-toggle");
    assert.equal((await box("#detail")).display, "none");
    await openGuidelineCamera();
    assert.equal((await box("#legend")).display, "none");

    await page.keyboard.press("Escape");
    assert.equal((await box("#detail")).display, "none");
  });

  test(`${mode.name}: Layers panel fits, has 44px targets, and yields to the legend`, HARD_TIMEOUT, async () => {
    await reloadAs(mode.viewport);
    const btn = await box("#layers-btn");
    assert.ok(btn.height >= 44 && btn.width >= 44, `layers button is ${btn.width}x${btn.height}`);
    assert.ok((await box("#topbar")).bottom <= btn.top, "button sits below the topbar");

    await page.tap("#layers-btn");
    const panel = await box("#layers-panel");
    assert.ok(panel.left >= 0 && panel.right <= width && panel.bottom <= height);
    const row = await box(".switch-row");
    assert.ok(row.height >= 44, `switch row is ${row.height}px tall`);

    await page.tap("#legend-toggle");
    assert.equal(await page.$eval("#layers-panel", (el) => el.hidden), true);
    await page.tap("#layers-btn");
    // pa11y resets the viewport, so it runs last.
    const results = await pa11y(base, {
      browser, page, ignoreUrl: true,
      runners: ["axe"], standard: "WCAG2AA",
      levelCapWhenNeedsReview: "warning",
    });
    assert.deepEqual(results.issues.filter((issue) => issue.type === "error"), []);
  });

  test(`${mode.name}: About modal fits, traps focus, and closes with Escape`, HARD_TIMEOUT, async () => {
    await reloadAs(mode.viewport);
    await page.tap("#about-btn");
    const card = await box("#about .modal-card");
    assert.ok(card.left >= 0 && card.right <= width);
    assert.equal(await page.evaluate(() => document.activeElement.className), "modal-close");
    await page.keyboard.down("Shift");
    await page.keyboard.press("Tab");
    await page.keyboard.up("Shift");
    assert.ok(await page.evaluate(() => document.querySelector("#about").contains(document.activeElement)));
    await page.keyboard.press("Escape");
    assert.equal((await box("#about")).display, "none");
    assert.equal(await page.evaluate(() => document.activeElement.id), "about-btn");
  });

  test(`${mode.name}: tapping near a camera opens it, and a11y holds with panels open`, HARD_TIMEOUT, async () => {
    await reloadAs(mode.viewport);
    const target = await page.evaluate(async () => {
      const gj = await (await fetch("data/cameras.geojson")).json();
      const f = gj.features.find((x) => x.properties.status === "flagged");
      window.__ff.map.jumpTo({ center: f.geometry.coordinates, zoom: 14 });
      await new Promise((r) => window.__ff.map.once("idle", r));
      const p = window.__ff.map.project(f.geometry.coordinates);
      return { x: p.x + 14, y: p.y + 10, id: f.properties.camera_id };
    });
    await page.touchscreen.tap(target.x, target.y);
    await page.waitForSelector("#detail:not(.hidden)");
    const results = await pa11y(base, {
      browser, page, ignoreUrl: true,
      runners: ["axe"], standard: "WCAG2AA",
      levelCapWhenNeedsReview: "warning",
    });
    assert.deepEqual(results.issues.filter((issue) => issue.type === "error"), []);
  });
}
