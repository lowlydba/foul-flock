"""ALPR Siting Compliance Map - build pipeline.

Data sources:
  - ALPR cameras: OpenStreetMap via Overpass (DeFlock tagging convention).
    Overture has no ALPR data, so cameras stay on OSM.
  - Protected places: Overture Maps places theme (CDLA-Permissive-2.0),
    streamed per state via the official overturemaps package and filtered
    locally against the vendored taxonomy (build/vendor/).
  - State boundaries: Overture Maps divisions theme (contains OSM data,
    ODbL), which also provides GERS division IDs.

Cross-references cameras against the hand-curated state siting-rules file
and emits static GeoJSON artifacts for the web map. Fully offline-serving
output: no runtime API calls in the web app.

Usage:
    python build/build.py [--default-buffer 100] [--offline] [--refresh]
                          [--states WA,VA]

    --default-buffer  Heuristic distance (m) used when a statute gives no
                      numeric buffer. Overrides default_heuristic_buffer_m
                      in the rules YAML.
    --offline         Use cached responses only (fail if missing).
    --refresh SCOPE   Ignore cache and refetch. SCOPE is a comma-separated
                      subset of cameras,places,divisions or "all" (the
                      default when the flag is given bare). Overture only
                      releases monthly, so the scheduled workflow refreshes
                      cameras weekly and everything else monthly. "roads"
                      also redoes every state-road lookup instead of reusing
                      the previous build's; it is not part of "all".
    --states          Restrict build to a subset of rule states.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict

from . import artifacts, config, guidelines, matching, overpass, overture, rulesfile


# ---------------------------------------------------------------- main ----

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--default-buffer", type=float, default=None,
                    help="heuristic buffer (m) when statute has no numeric distance")
    ap.add_argument("--scan-max", type=float, default=400,
                    help="max candidate scan radius (m) for the UI's adjustable "
                         "heuristic slider")
    ap.add_argument("--offline", action="store_true", help="cached data only")
    ap.add_argument("--refresh", nargs="?", const="all", default=None,
                    metavar="SCOPE",
                    help="ignore cache and refetch: comma-separated subset of "
                         "cameras,places,divisions, or 'all' (bare flag). "
                         "'roads' also redoes every state-road lookup, which "
                         "is otherwise reused from the previous build; it is "
                         "not part of 'all'")
    ap.add_argument("--states", default=None, help="comma-separated subset, e.g. WA,VA")
    args = ap.parse_args()
    if args.offline and args.refresh:
        sys.exit("--offline and --refresh are mutually exclusive")
    if args.refresh:
        valid = {"cameras", "places", "divisions"}
        scopes = ({s.strip() for s in args.refresh.split(",")}
                  if args.refresh != "all" else set(valid))
        bad = scopes - valid - {"roads"}
        if bad:
            sys.exit(f"unknown --refresh scope(s): {', '.join(sorted(bad))} "
                     f"(valid: all, roads, {', '.join(sorted(valid))})")
        config.REFRESH_SCOPES = scopes
        print(f"Refreshing: {', '.join(sorted(scopes))}")

    taxonomy = overture.load_taxonomy()
    rules_doc = rulesfile.load_rules_doc(config.RULES_FILE, taxonomy)
    rules = rules_doc["statutory_rules"]
    cat_mapping = rules_doc["category_overture_mapping"]
    code_lookup = overture.build_category_lookup(cat_mapping, taxonomy)
    name_lookup = overture.build_name_lookup(cat_mapping, taxonomy)
    default_buffer = (args.default_buffer
                      if args.default_buffer is not None
                      else rules_doc["default_heuristic_buffer_m"])

    rule_states = sorted({r["state_code"] for r in rules})
    guideline_rules = rules_doc["guideline_rules"]
    guide_states = sorted({g["state_code"] for g in guideline_rules})
    if args.states:
        keep = {s.strip().upper() for s in args.states.split(",")}
        rule_states = [s for s in rule_states if s in keep]
        rules = [r for r in rules if r["state_code"] in keep]
        guide_states = [s for s in guide_states if s in keep]
        guideline_rules = [g for g in guideline_rules
                           if g["state_code"] in keep]
    camera_states = sorted(set(rule_states) | set(guide_states))
    # Guideline proximity rules ride through the same matcher, tagged as
    # guideline by their authority
    poi_guidelines = [g for g in guideline_rules
                      if g["mechanism"] == "proximity"]

    print(f"States with siting rules: {', '.join(rule_states)}")
    print(f"States with spirit-of guidelines: {', '.join(guide_states)}")
    print(f"Default heuristic buffer: {default_buffer} m")

    print("\n[1/5] Fetching state boundaries (Overture divisions) ...")
    reviewed = [s["state_code"] for s in rules_doc["states_reviewed"]]
    divisions: dict[str, dict] = {}
    div_states = sorted(set(camera_states) | set(reviewed))
    for i, st in enumerate(div_states, 1):
        if st not in overture.INTERIOR_POINTS:
            print(f"    [{i}/{len(div_states)}] {st}: no interior probe point, skipping")
            continue
        print(f"    [{i}/{len(div_states)}] {st}")
        divisions[st] = overture.fetch_division(st, args.offline)
    print(f"    {len(divisions)} state polygons")

    print("\n[2/5] Fetching ALPR cameras (OSM/DeFlock tagging) ...")
    cameras: list[dict] = []
    for i, st in enumerate(camera_states, 1):
        cams = overpass.fetch_cameras(st, args.offline)
        print(f"    [{i}/{len(camera_states)}] {st}: {len(cams)} cameras")
        cameras.extend(cams)

    print("\n[3/5] Fetching protected places (Overture places) ...")
    needed_cats = defaultdict(set)  # state -> categories its rules need
    for r in rules + poi_guidelines:
        needed_cats[r["state_code"]].add(r["restricted_category"])
    places_by_cat: dict[str, list[dict]] = defaultdict(list)
    place_states = [s for s in camera_states if needed_cats[s]]
    for i, st in enumerate(place_states, 1):
        pls = overture.fetch_overture_places(st, needed_cats[st], code_lookup,
                                    divisions[st], args.offline,
                                    known_codes=set(taxonomy),
                                    name_lookup=name_lookup)
        by_cat = defaultdict(int)
        for p in pls:
            places_by_cat[p["category"]].append(p)
            by_cat[p["category"]] += 1
        print(f"    [{i}/{len(place_states)}] {st}: "
              + ", ".join(f"{c}={n}" for c, n in sorted(by_cat.items())))

    print("\n[4/5] Matching cameras against rules ...")
    n_excl = matching.mark_possible_exclusions(cameras, rules, rules_doc)
    print(f"    {n_excl} cameras marked possible statutory exclusion "
          f"(traffic-safety vendors)")
    n_road = guidelines.apply_road_guidelines(
        cameras, guideline_rules, args.offline,
        previous={} if "roads" in config.REFRESH_SCOPES else guidelines.load_previous_cameras())
    print(f"    {n_road} cameras flagged by state-road guidelines")
    matches = matching.run_matching(cameras, places_by_cat, rules + poi_guidelines,
                           default_buffer, args.scan_max)
    flagged = len({m["camera_id"] for m in matches
                   if m["distance_m"] <= m["buffer_m"]
                   and not m.get("guideline")})
    print(f"    {len(matches)} candidate matches; {flagged} cameras flagged "
          f"at default buffers")

    print("\n[5/5] Emitting artifacts ...")
    meta = artifacts.emit(cameras, places_by_cat, matches, rules, rules_doc,
                divisions, default_buffer, rule_states, args.scan_max,
                guideline_rules=guideline_rules)

    print(f"\nDone. {meta['flagged_count']} of {meta['camera_count']} cameras flagged "
          f"for review. Serve ./web to view the map.")
