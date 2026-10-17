# Development

## Layout

```
rules/alpr_state_rules.yaml   Hand-curated legal core. Never scraped/automated.
build/build.py                Entry point: fetch (cached) -> match -> emit web/data/ (GeoJSON + PMTiles)
build/foulflock/              The build pipeline, one module per concern (see AGENTS.md)
build/vectortiles.py          Tiler + PMTiles writer for the map's static layers
build/vendor/                 Pinned Overture places taxonomy (CC-BY-4.0)
web/                          Static MapLibre app: js/ (ES modules), css/ (no backend, no runtime APIs)
docs/research/                Archived state-law survey outputs
cache/                        Overpass + Overture response cache (safe to delete)
AGENTS.md                     Maintenance runbook (state re-reviews, overrides)
```

## Build and run

```powershell
pip install -r build\requirements.txt
python build\build.py                        # fetches (cached), then matches
node tests\serve.cjs 8080 web                # then open http://localhost:8080
```

Useful flags:

- `--offline`: cache only.
- `--refresh [scope]`: ignore the cache and refetch. Scope is `all` or a comma-separated subset of `cameras,places,divisions`.
- `--states WA`: build a subset of states.

The PMTiles archives are read by HTTP range request, so serve `web/` with `tests/serve.cjs` or another server that supports ranges. `python -m http.server` doesn't.

## Data refresh

A scheduled workflow (`.github/workflows/data-refresh.yml`) refreshes cameras weekly and Overture places and divisions monthly, matching Overture's monthly release cadence. Each run opens a PR with the regenerated `web/data/*`, so the published data stays current without manual runs. Merging it redeploys the site through `.github/workflows/deploy.yml`.

## Tests

The Python build pipeline is unit-tested with pytest. WCAG 2.1 A/AA is asserted with axe-core via pa11y-ci in both light and dark color schemes (see AGENTS.md for the theming rules). Popup sizing, dismissal, viewport-aware zoom limits, and the tablet and phone layouts have browser regression tests through Node's test runner and Puppeteer.

```powershell
pip install -r tests\python\requirements.txt
python -m pytest tests\python

npm ci
node tests\serve.cjs 8081 web      # in a second terminal
$env:A11Y_SCHEME = "dark"          # or "light" (default)
npm run test:a11y
npm run test:map
```

## CI

`.github/workflows/ci.yml` runs these suites on every push and PR and rolls them into a single required check. The pa11y job runs [lowlysre/pa11y-ci-action](https://github.com/lowlysre/pa11y-ci-action) against fixture data in `tests/fixtures/data`, since the real `web/data` is gitignored.

Workflow security is linted by zizmor at its pedantic persona (`.github/workflows/zizmor.yml`), and dependabot bumps actions, npm, and pip weekly.
