// WCAG 2.1 A/AA scan of the static site with axe-core, run once per color
// scheme (A11Y_SCHEME=light|dark). Expects tests/serve.cjs on :8081.
const scheme = process.env.A11Y_SCHEME || "light";
if (!["light", "dark"].includes(scheme)) {
  throw new Error(`Unsupported A11Y_SCHEME: ${scheme}`);
}

const base = "http://127.0.0.1:8081";

const phone = { width: 390, height: 844, deviceScaleFactor: 2, isMobile: true, hasTouch: true };
const tablet = { width: 820, height: 1180, deviceScaleFactor: 2, isMobile: true, hasTouch: true };
const openAbout = [
  "click element #about-btn",
  "wait for element #about .modal-card to be visible",
];

module.exports = {
  defaults: {
    runners: ["axe"],
    standard: "WCAG2AA",
    timeout: 60000,
    // axe "needs review" results (e.g. text over the raster map) are not
    // violations; cap them below error so only definite failures fail CI.
    levelCapWhenNeedsReview: "warning",
    chromeLaunchConfig: {
      args: [
        "--no-sandbox",
        // Blink's enum is inverted from intuition: 0 is dark, 1 is light.
        `--blink-settings=preferredColorScheme=${scheme === "dark" ? 0 : 1}`,
      ],
    },
  },
  // Desktop is the default viewport; phone and tablet exercise the
  // responsive layouts. The About dialog holds the how-it-works prose, so
  // it is scanned open at each size too. pa11y-ci-action rejects repeated
  // URLs, so each scenario gets its own (ignored) query string.
  urls: [
    { url: `${base}/` },
    { url: `${base}/?scan=phone`, viewport: phone },
    { url: `${base}/?scan=tablet`, viewport: tablet },
    { url: `${base}/?scan=about`, actions: openAbout },
    { url: `${base}/?scan=about-phone`, viewport: phone, actions: openAbout },
    { url: `${base}/?scan=about-tablet`, viewport: tablet, actions: openAbout },
  ],
};
