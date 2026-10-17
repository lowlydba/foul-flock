# How the map works

Foul Flock cross-references crowdsourced ALPR camera locations (OpenStreetMap, [DeFlock](https://deflock.me) tagging convention) against a hand-curated table of state camera-siting statutes, and flags cameras within a screened distance of a protected place: schools, places of worship, healthcare facilities, courts, food banks.

Every flag carries a statute citation and the law's own placement language, and the UI walks the user toward a verify-first inquiry or records-request letter. Rule provenance (`confidence`, `last_verified`) stays in the rules file for maintainers; the UI deliberately doesn't dress heuristic candidates up as verified findings.

## Statuses

The map deliberately distinguishes:

1. Flagged (red): the camera sits within the screened distance of a protected place. Comes with the statute citation and needs human verification.
2. Checked, nothing found (blue): a siting law exists in this state, and no mapped protected place turned up within the screened distance.
3. Reviewed, no siting law (grey state): shows a last-checked date on hover. States that haven't been reviewed get no overlay at all, because "we looked and there's nothing" and "we haven't looked" are different claims.
4. Spirit-of guideline (amber): not a siting statute, but an executive action or not-yet-operative provision the map visualizes best-effort (rules with a non-statute `authority` in the rules file).
   - Depending on the rule, cameras are flagged for sitting on state-maintained roads or near best-effort matches for a protected place type.
   - Where a rule has a grandfather date, cameras already mapped before it are filtered out and later ones are only *possibly* a problem, since mapping date isn't install date.
   - Rules whose distance isn't set yet are labeled as such.
   - Guidelines never produce a report letter.

## Heuristic distances

When a statute uses vague language ("immediately surrounding") instead of a number, the build applies a configurable "close by" default of 100 m. That number is ours, not the law's, and the UI labels it as such everywhere it appears (`distance_basis: close_by` in the rules file).

Two legend sliders adjust the heuristics live, with no rebuild, because the build pre-computes candidates out to `--scan-max` (400 m):

- "Close by" (50-400 m, default 100) for statutes with no distance language at all.
- "On premises" (10-150 m, default 50) for on-property language like Virginia's "on or within the boundaries of" (`distance_basis: on_premises` in the rules file).

Statutory numeric distances are fixed and unaffected by either slider. Change the defaults with `--default-buffer` or `default_heuristic_buffer_m`, and `default_on_premises_m`, in `rules/alpr_state_rules.yaml`.

Whenever a heuristic distance is used, the UI and the drafted report letter also quote the statute's actual placement language ("on or immediately surrounding", "on or within the boundaries of") from the `statutory_language` field. The on-property vs. nearby distinction is never hidden behind an invented number.

## View cones

Where OSM records a `camera:direction` bearing, the map draws an illustrative view cone, 60 degrees wide and 60 m deep, visible once you zoom in. OSM has no field-of-view width or range tags for ALPRs, so cones show the mapped bearing only. They aren't real detection zones.

## Data sources

| Source | Provides | License |
|---|---|---|
| OpenStreetMap via Overpass | ALPR camera nodes (`surveillance:type=ALPR`, DeFlock convention) | ODbL |
| Overture Maps places theme | Protected places (schools, worship, healthcare, courts, food banks) | CDLA-Permissive-2.0 |
| Overture Maps divisions theme | State boundary polygons and GERS division IDs | Contains OSM data: ODbL |
| Overture places taxonomy (vendored in `build/vendor/`) | Category codes mapped to the protected categories | CC-BY-4.0 |
| `rules/alpr_state_rules.yaml` | Hand-curated statutes, buffers, citations | Project |

Places and divisions are streamed per state from the Overture S3 release via the official [overturemaps](https://github.com/OvertureMaps/overturemaps-py) package (anonymous reads, no credentials) and filtered locally against the vendored taxonomy. The category-to-protected-place mapping lives in `category_overture_mapping` in the rules file. Cameras stay on OSM/Overpass because Overture has no ALPR layer. `state_division_id` on each rule records its Overture GERS division ID.

Camera data © OpenStreetMap contributors (ODbL). Places and boundaries © Overture Maps Foundation (places CDLA-Permissive-2.0; divisions contain OSM data, ODbL). See the [README](../README.md#license) for redistribution terms.
