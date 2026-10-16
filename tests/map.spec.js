const path = require("path");
const { test, expect } = require("@playwright/test");
const { AxeBuilder } = require("@axe-core/playwright");

test.beforeEach(async ({ page }) => {
  await page.route("**/data/*", (route) =>
    route.fulfill({
      path: path.join(__dirname, "fixtures", "data", new URL(route.request().url()).pathname.split("/").pop()),
    })
  );
  await page.goto("/");
  await expect(page.locator("#stats")).not.toContainText(/loading/i);
  await page.waitForFunction(() => Boolean(window.__ff));
});

for (const fontScale of [1, 2]) {
  test(`popup close control stays compact at ${fontScale * 100}% text size`, async ({ page }) => {
    await page.evaluate((scale) => {
      document.documentElement.style.fontSize = `${scale * 100}%`;
      new maplibregl.Popup()
        .setLngLat(window.__ff.map.getCenter())
        .setHTML("<b>Midlothian Middle School</b><br>Protected category: School")
        .addTo(window.__ff.map);
    }, fontScale);

    const popup = page.locator(".maplibregl-popup-content");
    const close = popup.getByRole("button", { name: "Close popup" });
    await expect(close).toBeVisible();
    const buttonBox = await close.boundingBox();
    expect(buttonBox.width).toBe(24);
    expect(buttonBox.height).toBe(24);
    await expect(close).toHaveCSS("padding", "0px");
    await expect(close).toHaveCSS("margin", "0px");
    await expect(close).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
    await expect(close).toHaveCSS("color", "rgb(31, 35, 40)");

    const textRight = await popup.locator("b").evaluate((el) => {
      const range = document.createRange();
      range.selectNodeContents(el);
      return Math.max(...Array.from(range.getClientRects(), (rect) => rect.right));
    });
    expect(textRight).toBeLessThan(buttonBox.x);

    await close.hover();
    await expect(close).toHaveCSS("background-color", "rgba(0, 0, 0, 0.05)");
    await close.focus();
    await page.keyboard.press("Tab");
    await page.keyboard.press("Shift+Tab");
    await expect(close).toBeFocused();
    await expect(close).toHaveCSS("outline-style", "solid");

    const results = await new AxeBuilder({ page })
      .include(".maplibregl-popup-content")
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
      .analyze();
    expect(results.violations).toEqual([]);

    await page.keyboard.press("Enter");
    await expect(popup).toHaveCount(0);

    await page.evaluate(() => {
      new maplibregl.Popup({ closeButton: false, className: "state-tip" })
        .setLngLat(window.__ff.map.getCenter())
        .setText("Reviewed, no siting law found")
        .addTo(window.__ff.map);
    });
    await expect(page.locator(".state-tip .maplibregl-popup-content")).toHaveCSS("padding", "6px 10px");
    await expect(page.locator(".state-tip button")).toHaveCount(0);
  });
}

for (const viewport of [{ width: 1280, height: 720 }, { width: 390, height: 844 }]) {
  test(`zoom-out stops at the US footprint at ${viewport.width}px wide`, async ({ page }) => {
    await page.setViewportSize(viewport);
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
    expect(result.minZoom).toBeCloseTo(result.expected, 6);
    expect(result.minZoom).toBeGreaterThan(0);
    expect(result.zoom).toBeCloseTo(result.minZoom, 6);
    expect(result.afterZoomOut).toBeCloseTo(result.minZoom, 6);
    expect(result.visible).toEqual([true, true]);
    expect(result.zoomedIn).toBeCloseTo(result.minZoom + 2, 6);
  });
}
