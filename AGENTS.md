# AGENTS.md - Maintenance guide for the ALPR Siting Compliance Map

Instructions for any agent (or human) performing maintenance on this repo.
Model-agnostic: assumes only web search, file editing, and a shell.

## What this project is

A static map flagging ALPR cameras near legally-protected locations
(schools, worship, healthcare, courts, food banks) under state siting
statutes. The legal core is `rules/alpr_state_rules.yaml` (hand-curated,
never scraped). `build/build.py` fetches cameras from OSM/Overpass and
places + state boundaries from Overture Maps, matches, and emits
`web/data/*.geojson` consumed by the static MapLibre app in `web/`.

Read `README.md` first. Honesty rules are non-negotiable:

- Never invent a numeric distance and present it as statutory. Statutes with
  vague language get `buffer_meters: null`, `buffer_specified: false`, and a
  quoted `statutory_language` field. The UI labels heuristic distances.
- Every legal claim needs `source_url` and `last_verified`.
- A flag is a screening candidate, never a violation determination.

## Recurring task 1: quarterly state-law re-review

Cadence: quarterly, aligned to legislative sessions (Jan-Jun is peak).
Last full 50-state + DC survey: **2026-07-18** (archived output referenced
below). At that time only WA and VA had siting/placement laws.

Procedure:

1. For each state, determine whether it has a statute restricting the
   PHYSICAL PLACEMENT/SITING of ALPRs (near protected location categories or
   on certain road types). This is distinct from retention/access/use rules,
   which this project does not track beyond a one-line note.
2. Start from the canonical secondary sources (below), then confirm against
   the primary statute text whenever a state's status changed.
3. Update `rules/alpr_state_rules.yaml`:
   - New siting law: add one rule entry per (state, restricted_category)
     under `rules:` (copy a WA rule as a template) and REMOVE that state's
     entry from `states_reviewed_no_siting_law`.
   - Still no siting law: update the state's `note` if its ALPR statute
     changed, and bump `last_verified`.
   - Always bump `last_verified` on anything you actually checked.
4. Rebuild and verify (see "Rebuild and verify" below).

States most likely to change (active bills as of mid-2026): CA (vetoed
SB 274 may return), CO (SB 26-070 lost, may return), and any state listed
with pending bills in the archived survey.

## Canonical sources (validated 2026-07-18)

Secondary trackers, in order of usefulness:

- LAPPA 50-state summary (updated periodically):
  https://legislativeanalysis.org/automatic-license-plate-recognition-systems-summary-of-state-laws/
  (PDF: legislativeanalysis.org/wp-content/uploads/2025/09/Automatic-License-Plate-Recognition-Systems-Summary-of-State-Laws.pdf)
- ObscureIQ state legality map: https://www.obscureiq.com/license-plate-surveillance-legality/
- Stateline legislative coverage (search "license plate reader"):
  https://stateline.org
- RecordingLaw ALPR overview: https://www.recordinglaw.com/us-laws/automated-license-plate-readers/
- unflocked.org (advocacy tracker)
- Land Line Media legislative tracking: https://landline.media

Primary sources for the two current siting laws:

- WA SB 6002 "Driver Privacy Act" (eff. 2026-03-30):
  https://app.leg.wa.gov/billsummary?BillNumber=6002&Year=2026
  Key language: "on or immediately surrounding" protected premises; no
  numeric distance; 21-day retention; EXCLUDES school bus safety, speed
  safety, and automated traffic safety cameras per RCW 46.63.210.
- VA HB 2724 (eff. 2025-07-01):
  https://lis.virginia.gov/bill-details/20251/HB2724
  Key language: "on or within the boundaries of" education/healthcare/
  worship property absent written institutional consent (on-property
  standard, not a radius).

Full research notes from the 2026-07-18 survey (per-state verdicts,
citations, sources) are archived at
`docs/research/state-law-survey-2026-07-18.txt`; condensed entries live in
`rules/alpr_state_rules.yaml` under `states_reviewed_no_siting_law`. Archive
future surveys the same way (`docs/research/state-law-survey-YYYY-MM-DD.txt`).

## Recurring task 2: data refresh

Automated: `.github/workflows/data-refresh.yml` runs on two crons plus
workflow_dispatch (with a scope input): weekly it refreshes cameras only
(`--refresh cameras`; crowdsourced OSM moves fast), monthly it refreshes
everything (`--refresh all`; Overture only cuts a release monthly, so more
often is wasted transfer). Each run force-pushes the regenerated
`web/data/*` to the `auto/data-refresh` branch as an auto-PR. Review the
flagged-count delta in the workflow log before merging; a big jump usually
means an upstream data change, not new cameras.

Manual, on demand:

```powershell
python build\build.py --refresh              # everything fresh
python build\build.py --refresh cameras      # just cameras
python build\build.py --refresh places,divisions   # just Overture data
```

Overpass etiquette (cameras): the script rotates mirrors (overpass-api.de,
kumi.systems, private.coffee) with backoff; do not tighten the sleep. If a
mirror hangs, delete the partial cache file and rerun - completed queries
are cached and skipped.

Overture notes (places, divisions): streamed from the public S3 release
via the official `overturemaps` package; anonymous reads, no credentials.
Places are cached per state AFTER category filtering, and the cache file
records the category codes used, so editing `category_overture_mapping`
self-invalidates the cache on the next online build. Divisions are probed
with a tiny bbox at an interior point per state (INTERIOR_POINTS in
build.py); geometry is simplified (~1 km) for the national overlay. The
vendored taxonomy CSV and its refresh procedure live in `build/vendor/`.

## Rules-file field reference (beyond the obvious)

- `buffer_meters` - ONLY a number that appears in the statute itself.
- `buffer_specified` - false when the law is vague/on-property; drives the
  "heuristic" labeling in the UI.
- `heuristic_buffer_meters` - per-rule screening override when the global
  default is wrong for the statute's standard. Example: VA rules use 50
  because "on or within the boundaries" is an on-property standard and the
  wider global default produced heavy false positives. Any rule with this
  field is governed by the UI's "on premises" slider; rules with neither a
  statutory number nor this field use the "close by" slider.
- `statutory_language` - short quote/paraphrase of the placement language,
  shown to users whenever a heuristic distance is used. Note in the text if
  it is paraphrased pending primary verification.
- `statutory_exclusions` - device types the statute carves out of its ALPR
  definition (see WA / RCW 46.63.210). Presence of this field on any rule in
  a state activates vendor-watchlist screening for that state.
- `exclusion_manufacturer_watchlist` (top level) - vendors primarily known
  for excluded camera types (speed/school-bus/red-light). Cameras from these
  vendors in exclusion states are marked `possible_exclusion` instead of
  flagged. Do NOT add general ALPR/toll vendors (Flock, Motorola, Neology,
  Kapsch, TransCore) - toll ALPRs are still ALPRs.
- `confidence` / `last_verified` - maintainer provenance. Deliberately NOT
  shown in the UI (product decision 2026-07-18: the tool surfaces candidates
  heuristically; users are told to verify everything regardless).
- `state_division_id` - the state's Overture divisions GERS ID (stable
  across releases), recorded for provenance and future division-based
  filtering. Populated by checking the `division_id` property in
  `web/data/states.geojson` after a build.
- `category_overture_mapping` (top level) - protected category -> Overture
  taxonomy anchor codes. A place matches when its categories.primary code
  is an anchor or a taxonomy descendant of one (elementary_school matches
  via school). Taxonomy is vendored at build/vendor/overture_categories.csv
  (CC-BY-4.0); see build/vendor/README.md before changing anchors.

## Rebuild and verify

```powershell
pip install pyyaml overturemaps
python build\build.py --offline                     # cache-only rebuild
python -m http.server 8080 -d web                   # serve locally
```

(`--offline` needs only pyyaml; the overturemaps package is required for
any non-cached fetch.)

Checks after any rules change:

1. Build completes; note the flagged count and sanity-check the delta
   against your change (e.g., tightening a buffer should lower it).
2. `web/data/rules.json` contains your new/changed entries.
3. `states.geojson` law_status distribution matches expectations:
   `curl -s http://localhost:8080/data/states.geojson` piped through a JSON
   counter on `properties.law_status`.
4. Load the map: click a flagged camera in each affected state; confirm the
   citation, quoted statutory language, and report-letter draft render.
5. `node --check web/app.js` if you touched the frontend.

## UI conventions (keep consistent if editing web/)

- Status taxonomy: flagged / checked_clear / possible_exclusion (cameras),
  siting_law / reviewed_no_law (states). Unreviewed states get NO feature in
  states.geojson (no overlay = no claim); never paint an unreviewed state as
  "reviewed, no law".
- State-status popups only at zoom <= 6.5 (national browsing); empty clicks
  when zoomed in must do nothing. Hovering a state at national zoom shows a
  small tooltip with its status and last-checked date (last_verified,
  emitted by the build from the rules file).
- Keep prose out of the map chrome: legend rows stay one line; method
  details live in the collapsible "Method notes"; long caveats go in the
  About modal or collapsible sections of the detail panel.
- Every path a user can take toward contacting an agency must pass a
  not-legal-advice / verify-yourself disclaimer, and drafted letters must
  use good-faith inquiry language, not accusations.
- No em dashes anywhere in the repo (user style rule). No runtime API calls;
  everything must work as static files.

## Theming and accessibility (enforced by CI)

- Theme sits on Pico CSS (classless build, vendored at
  web/assets/pico.classless.min.css, loaded before style.css): Pico owns
  base typography, links, buttons, form controls, details, and the
  light/dark switch. style.css owns the theme tokens (fonts + teal accent
  set via --pico-* variables in :root, dark overrides mirror Pico's
  `:root:not([data-theme=light])` selector) and the map chrome. To
  restyle, change the --pico-* variables; don't reintroduce base-element
  CSS. To update Pico, re-download the classless build and rerun the axe
  suite.
- Contrast rules baked into the palette: Pico's primary (teal) carries
  links/buttons; status colors split decorative vs ink (--flagged dot
  fill vs --flagged-ink text/badges; --flagcard-muted exists because
  Pico's dark muted misses AA on the match-card background). MapLibre
  popups and the attribution bar stay light in both schemes, so their ink
  is pinned dark in CSS.
- WCAG 2.1 A/AA is asserted by axe-core + Playwright: `npm ci`,
  `npx playwright install chromium`, then `npm run test:a11y`. Tests run in
  light AND dark schemes and cover the base chrome, legend, About modal,
  and the flagged-camera detail/report flow (via the window.__ff test hook
  at the bottom of app.js).
- CI: .github/workflows/ci.yml runs pytest (tests/python, offline unit
  tests for build.py: geometry, taxonomy mapping, matching, cache/refresh,
  emit) and the axe suite on push/PR, rolled into one required check via
  are-we-good. The axe job uses fixture data in tests/fixtures/data (real
  web/data is gitignored). If you change the data schema, regenerate
  fixtures from a fresh build: keep a couple of cameras per status,
  matching view cones, a few places, WA/VA/OR states, and the current
  rules.json. If you change build.py behavior, extend tests/python too.
- Python deps are pinned in build/requirements.txt (runtime) and
  tests/python/requirements.txt (tests); dependabot
  (.github/dependabot.yml) bumps actions, npm, and pip weekly in grouped
  PRs.
- Keep GitHub Actions `uses:` entries pinned to full commit SHAs with a
  `# vX.Y.Z` comment.
- Workflow conventions (enforced by the zizmor workflow at persona
  `pedantic`; run `zizmor --persona pedantic .github/workflows/` locally
  before pushing workflow changes, it must report zero findings):
  - checkout always sets `persist-credentials: false`; the data-refresh
    push authenticates via `gh auth setup-git` instead.
  - Never interpolate `${{ }}` inside `run:` blocks; route values through
    `env:`.
  - Permissions are minimal, declared at the tightest level, and each
    grant carries a trailing `# why` comment.
  - Every workflow has a `concurrency` group; every job has a `name`.
  - Runners: `ubuntu-slim` everywhere except the zizmor job
    (`ubuntu-latest`). Playwright's webServer is the dependency-free
    tests/serve.cjs (node), so the suite doesn't assume python exists on
    slim runners.
  - `lowlydba/sustainable-npm` runs after setup-node, before any npm
    install.
  - `lowlydba/are-we-good` is the final rollup job in the a11y workflow;
    require that single check in branch protection, not per-job checks.
- Production domain is www.foulflock.com (root CNAME file, owned upstream,
  plus canonical/OG tags in index.html). The root index.html holding page
  will be replaced by the web/ app when Pages deployment is wired up.
- SEO assets live in web/assets/: favicon.svg, og-image.png (1200x630,
  referenced by og:image/twitter:image meta and the README hero), and
  og-template.html (the source; re-render with a 1200x630 Playwright
  screenshot if the branding changes).
- Protected places render as orange badge icons using Maki glyphs (CC0,
  https://labs.mapbox.com/maki-icons/) stored in web/assets/icons/ and
  composed at runtime in app.js (loadPlaceIcon: white glyph on orange
  circle). Worship icons pick christian/muslim/jewish variants from the
  `religion` property, derived in build.py from the Overture category code
  (RELIGION_BY_CATEGORY). To add a category, download the Maki SVG and add
  a row to PLACE_ICONS.
- Two heuristic radii are user-adjustable at runtime (legend sliders,
  ranges in rules.json `sliders`): "close by" (default 100 m) for rules
  with no distance language, and "on premises" (default 50 m) for rules
  with heuristic_buffer_meters (VA). The build scans candidates out to
  --scan-max (400 m) for all non-statutory rules and app.js recomputes
  flagged status client-side (applyBuffer). Statutory numeric distances
  are NOT affected by the sliders.
- Plain-English onboarding lives in web/faq.html ("How & why", linked from
  the topbar and About modal). Keep it jargon-free and keep the
  not-legal-advice framing.
