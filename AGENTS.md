# AGENTS.md - Maintenance guide for the ALPR Siting Compliance Map

Instructions for any agent (or human) performing maintenance on this repo.
Model-agnostic: assumes only web search, file editing, and a shell.

## What this project is

A static map flagging ALPR cameras near legally-protected locations
(schools, worship, healthcare, courts, food banks) under state siting
statutes. The legal core is `rules/alpr_state_rules.yaml` (hand-curated,
never scraped). `build/build.py` fetches cameras from OSM/Overpass and
places + state boundaries from Overture Maps, matches, and emits
`web/data/*.geojson` and PMTiles archives consumed by the static MapLibre app in `web/`.

Read `README.md` first. Honesty rules are non-negotiable:

- Never invent a numeric distance and present it as statutory. Statutes with
  vague language get `distance_basis: close_by` or `on_premises` (with
  `buffer_meters: null`) and a quoted `statutory_language` field. The UI
  labels heuristic distances.
- Every legal claim needs `sources` and `last_verified`.
- A flag is a screening candidate, never a violation determination.
- The rules file is validated on every build and in pytest
  (`build.validate_rules_doc`, `tests/python/test_rules.py`). Run
  `python -m pytest tests\python` after any edit to it.

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
     under `rules:` with `authority: statute` (copy the WA/VA anchors as a
     template) and REMOVE that state's entry from `states_reviewed` (the
     validator rejects a state that is in both).
   - Still no siting law: update the state's `summary` if its ALPR statute
     changed, and bump `last_verified`. Put maintainer-only caveats in
     `maintainer_notes`; `summary` is shown to users.
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
`rules/alpr_state_rules.yaml` under `states_reviewed`. Archive
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
kumi.systems, private.coffee) with backoff; do not tighten the sleep. The
camera query uses `out meta` so each node's last OSM edit date lands in
`last_edited` (shown in the detail panel and cited in the draft letter);
only the date is kept, never the editor's identity. Caches written before
that change lack the date until the next `--refresh cameras`. If a
mirror hangs, delete the partial cache file and rerun - completed queries
are cached and skipped.

Overture notes (places, divisions): streamed from the public S3 release
via the official `overturemaps` package; anonymous reads, no credentials.
Reads go through the package's STAC index, which opens only the files that
intersect the bbox so cost tracks the query area as the dataset grows; an
unreachable index falls back to a full dataset scan.
Places are cached per state AFTER category filtering, and the cache file
records the category codes used, so editing `category_overture_mapping`
self-invalidates the cache on the next online build. A place's category
is its `taxonomy.primary`; the vendored taxonomy (`build/vendor/`, with
its refresh procedure) expands the anchors, and the stream warns when
places carry codes the vendored file lacks.

State boundaries are effectively static, so divisions are seeded from the
previous build's `states.geojson`. Only a state missing from it is streamed,
with a tiny bbox probe at an interior point (INTERIOR_POINTS in build.py).
Each probe takes about 10 s because the full-resolution polygons are large.
`--refresh divisions` forces a real fetch. Geometry is simplified (~1 km)
for the national overlay.

Road lookups for `road_class` guidelines are seeded the same way: cameras
whose id and position match the previous `cameras.geojson` or
`clear_cameras.pmtiles` (positions there are tile-rounded, so "match" allows
about 5 m), evaluated under
the same rule configuration, reuse its result, so a routine refresh only
queries new or moved cameras. A cold run (no previous build, a changed
`road_ref_regex`/`road_buffer_m`, or `--refresh roads`) looks up every
camera in id chunks of 1,500 and is slow on busy Overpass mirrors;
`data-refresh.yml` allows two hours for it. Neither `--refresh cameras` nor
`--refresh all` redoes road lookups, so a camera judged clear keeps that
result until it moves.

## Rules-file field reference (beyond the obvious)

`validate_rules_doc` in `build/build.py` enforces the schema on every build
and in `tests/python/test_rules.py`; its error messages list every missing or
inconsistent field. The file has two entry lists:

- `states_reviewed` - one entry per reviewed state describing its STATUTORY
  position (`statute_status`: `no_siting_law` or `no_alpr_statute`), with
  `summary`, `sources`, `confidence`, and `last_verified`. A state with a
  statutory siting rule must not appear here. A state's map status is
  derived: statutory rules -> purple, else guideline rules -> amber, else
  its registry entry -> grey, else no overlay (unreviewed = no claim).
- `rules` - every rule the map visualizes, statutes and guidelines in one
  list. `mechanism: proximity` matches cameras near places of
  `restricted_category`; `mechanism: road_class` is covered below.
- `authority` splits statutes from "spirit-of" guidelines (`executive_action`,
  `statute_pending_distance`). Guidelines get the amber `guideline`
  camera/state status, never a report letter, and never count toward
  `rule_states`/flagged. When a guideline becomes a real statute (e.g. CT
  sets its distance), change its `authority` to `statute` with a quoted
  `statutory_language` and `language_verbatim`.
- `distance_basis` picks the radius: `statutory` (`buffer_meters`, ONLY a
  number that appears in the statute itself), `on_premises` (on-property
  wording such as VA's "on or within the boundaries"; "on premises" slider,
  `default_on_premises_m`), `close_by` (no distance language; "close by"
  slider), `tbd` (law leaves the distance to be set; on-premises slider,
  flagged loudly in the UI).
- `mechanism: road_class` (guidelines only) tags cameras within
  `road_buffer_m` of ways whose OSM `ref` matches `road_ref_regex`, queried
  from Overpass in id chunks. With `grandfather_cutoff`, cameras in the
  Overpass attic snapshot of that date are filtered out and later ones are
  "possibly" a problem. The snapshot is cached forever per state and date.
  `road_buffer_m` is a proximity test, so a match does not establish that a
  camera sits in the right-of-way.
- `summary` is public (tooltips, popups, detail panel); `maintainer_notes`
  never leaves the YAML. `sources` is a list of `{url, type}`; the first
  primary (else first) becomes the user-facing `source_url`.
- `language_verbatim` - whether `statutory_language` is an exact quote; the
  UI tags paraphrases. `date_approximate` marks an approximate
  `effective_date`.
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
  taxonomy anchor codes. A bare code matches itself and every descendant
  (school covers elementary_school); `{code: X, descendants: false}`
  matches only X. `{code: X, name_pattern: REGEX}` also requires the place
  name to match (case-insensitive); use it only where the taxonomy has no
  code for the protected kind of place. Taxonomy is vendored at
  build/vendor/overture_taxonomy.csv; see build/vendor/README.md before
  changing anchors.

## Rebuild and verify

```powershell
pip install pyyaml overturemaps pmtiles shapely     # or: pip install -r build\requirements.txt
python build\build.py --offline                     # cache-only rebuild
node tests\serve.cjs 8080 web                       # serve locally (PMTiles need range requests)
```

(`--offline` needs pyyaml, pmtiles and shapely; the overturemaps package is
required for any non-cached fetch.)

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
- WCAG 2.1 A/AA is asserted by axe-core via pa11y-ci (config in
  .pa11yci.cjs): `npm ci`, serve web/ with `node tests/serve.cjs 8081 web`,
  then `npm run test:a11y`. Set `A11Y_SCHEME=dark` for the dark scheme
  (light is the default); CI runs both. It scans the home page at load
  at desktop, tablet (820px), and phone (390px) viewports, plus the About
  dialog opened at each, so the detail panel and report letter aren't
  covered there.
- `npm run test:map` runs Node/Puppeteer regression tests against the same
  server for popup text scaling, dismissal, popup accessibility,
  viewport-aware zoom limits, and the tablet/phone layouts (legend toggle,
  detail drawer/sheet, 44px touch targets, focus handling). Popups render
  the same in both schemes, so CI runs it once (light job).
- Data payload: cameras with no candidate place inside the scan radius
  (about 80% of cameras) can never be flagged at any slider setting, so
  build.py emits them, and their view cones, as static vector tiles
  (`clear_cameras.pmtiles`, `clear_cones.pmtiles`) instead of in
  `cameras.geojson`, which the slider logic keeps in memory. States and
  protected places are `states.pmtiles` and `places.pmtiles` too. The
  Layers panel's "Cameras with nothing found" switch is off by default;
  MapLibre reads the archives by HTTP range request, so only tiles in view
  transfer (cameras draw from zoom 6, cones from 13). Candidate cones stay
  in `view_cones.geojson`, which loads lazily near zoom 13 and is restyled
  as slider changes flip camera status. `build/vectortiles.py` is the
  tiler and PMTiles writer (pins: `pmtiles` and `shapely` in
  build/requirements.txt); `web/assets/pmtiles.js` is the vendored reader
  (pmtiles 4.5.0, BSD-3-Clause; to update, copy `dist/pmtiles.js` from the
  npm package and rerun `npm run test:map`). `states.geojson` stays in
  web/data only as the full-resolution seed for the next build; the app
  never fetches it. rules.json carries `rule_bounds` (initial fit) and
  `clear_camera_count`. Static hosts must support range requests
  (GitHub Pages and `tests/serve.cjs` do; `python -m http.server` does
  not). Camera properties with no information (null, false, empty) are
  omitted; descriptive and provenance fields are never trimmed. Tile
  positions are rounded to ~1 m, so `last_edited`, `osm_url` and the node
  id remain the exact reference.
- Responsive modes (web/style.css "responsive modes", MODES in app.js; keep
  the breakpoints in sync): desktop > 1024px, tablet 641-1024px, phone
  <= 640px. Compact modes collapse the legend behind a topbar button and
  show the detail panel as a right drawer (tablet) or bottom sheet (phone);
  touch input uses a 22px tap tolerance on camera dots.
- CI: .github/workflows/ci.yml runs pytest (tests/python, offline unit
  tests for build.py: geometry, taxonomy mapping, matching, cache/refresh,
  emit) and the pa11y suite (lowlysre/pa11y-ci-action) on push/PR, rolled
  into one required check via are-we-good. The pa11y job uses fixture data
  in tests/fixtures/data (real
  web/data is gitignored). If you change the data schema, regenerate
  fixtures from a fresh build: keep a couple of cameras per status,
  matching view cones, a few places, WA/VA/OR states, a clear camera with
  its cone, the PMTiles archives (regenerate them from GeoJSON with
  `python build/vectortiles.py in.geojson out.pmtiles --layer L --minzoom
  A --maxzoom B`, using the zoom constants in build.py), and the current
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
  - Runners: `ubuntu-slim` everywhere except the zizmor, pa11y, and
    data-refresh jobs (`ubuntu-latest`; slim lacks Chromium's system
    libraries and caps jobs at 15 minutes). The pa11y
    job's static server is the dependency-free tests/serve.cjs (node), so
    the suite doesn't assume python exists.
  - `lowlydba/sustainable-npm` runs after setup-node, before any npm
    install.
  - `lowlydba/are-we-good` is the final rollup job in the a11y workflow;
    require that single check in branch protection, not per-job checks.
- Production domain is www.foulflock.com (canonical/OG tags in
  web/index.html). Deployment: .github/workflows/deploy.yml publishes web/
  as the Pages site root (workflow build type; the custom domain lives in
  the repo's Pages settings, not a CNAME file) on pushes to main touching
  web/**, so merging a data-refresh auto-PR redeploys the site.
- SEO assets live in web/assets/: favicon.svg, og-image.png (1200x630,
  referenced by og:image/twitter:image meta and the README hero), and
  og-template.html (the source; re-render with a 1200x630 Playwright
  screenshot if the branding changes).
- Protected places render as orange badge icons using Maki glyphs (CC0,
  https://labs.mapbox.com/maki-icons/) stored in web/assets/icons/ and
  composed at runtime in app.js (loadPlaceIcon: white glyph on an orange
  rounded square, so places don't read as camera dots). Worship icons pick christian/muslim/jewish variants from the
  `religion` property, derived in build.py from the place's Overture
  taxonomy hierarchy (RELIGION_BY_ANCESTOR). To add a category, download the Maki SVG and add
  a row to PLACE_ICONS.
- Two heuristic radii are user-adjustable at runtime (legend sliders,
  ranges in rules.json `sliders`): "close by" (default 100 m) for rules
  with no distance language, and "on premises" (default 50 m) for rules
  with `distance_basis` `on_premises` (VA) or `tbd` (CT guideline). The
  build scans candidates out to
  --scan-max (400 m) for all non-statutory rules and app.js recomputes
  flagged status client-side (applyBuffer). Statutory numeric distances
  are NOT affected by the sliders.
- Plain-English onboarding lives in the About dialog in web/index.html
  (opened by the topbar About button or the #about deep link; web/faq.html
  is a redirect stub for old How & why links). Keep it jargon-free and keep the
  not-legal-advice framing.
