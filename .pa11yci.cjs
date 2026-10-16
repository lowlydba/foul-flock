// WCAG 2.1 A/AA scan of the static site with axe-core, run once per color
// scheme (A11Y_SCHEME=light|dark). Expects tests/serve.cjs on :8081.
const scheme = process.env.A11Y_SCHEME || "light";
if (!["light", "dark"].includes(scheme)) {
  throw new Error(`Unsupported A11Y_SCHEME: ${scheme}`);
}

const base = "http://127.0.0.1:8081";

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
  urls: [`${base}/`, `${base}/faq.html`],
};
