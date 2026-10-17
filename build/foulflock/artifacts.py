"""Writes web/data: core GeoJSON, PMTiles archives, rules.json and the data
license notice."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timezone

import vectortiles

from . import config, rulesfile, viewcones


# ---------------------------------------------------------------- emit ----

def json_default(o):
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    raise TypeError(str(type(o)))


def compact_matches(cam_matches: list[dict]) -> str | None:
    """Matches as a compact JSON string for the web app, or None if empty.
    Each match drops camera_id: it repeats the parent feature's id."""
    if not cam_matches:
        return None
    return json.dumps(
        [{k: v for k, v in m.items() if k != "camera_id"} for m in cam_matches],
        separators=(",", ":"))


def emit(cameras, places_by_cat, matches, rules, rules_doc, divisions,
         default_buffer, rule_states, scan_max, guideline_rules=None):
    config.OUT_DIR.mkdir(parents=True, exist_ok=True)
    guideline_rules = guideline_rules or []
    guide_states = {g["state_code"] for g in guideline_rules}
    guide_cats = {g["restricted_category"] for g in guideline_rules
                  if g.get("restricted_category")}
    matches_by_cam = defaultdict(list)
    for m in matches:
        matches_by_cam[m["camera_id"]].append(m)

    # cameras.geojson - every camera that is, or could become with a wider
    # radius, a candidate; cameras with no candidate place in the scan
    # radius go to per-state clear shards loaded on demand
    # view_cones.geojson - illustrative FOV cones where camera:direction exists
    cam_features = []
    cone_features = []
    clear_cams = []
    clear_cones = []
    for cam in cameras:
        cam_matches = sorted(matches_by_cam.get(cam["camera_id"], []),
                             key=lambda m: m["distance_m"])
        # status reflects the default buffers; candidates beyond them are
        # kept in the file so the UI slider can widen the radius client-side
        within = [m for m in cam_matches if m["distance_m"] <= m["buffer_m"]]
        statutory_hits = [m for m in within if not m.get("guideline")]
        road_flag = bool(cam.get("road_flag"))
        if cam.get("possible_exclusion"):
            status = "possible_exclusion"
        elif statutory_hits:
            status = "flagged"
        elif len(within) > len(statutory_hits) or road_flag:
            status = "guideline"
        else:
            status = "checked_clear"
        bearings = viewcones.parse_directions(cam["direction"])
        # Properties that carry no information (null, false, empty) are
        # omitted so the ~34k-camera file stays small; the web app treats
        # a missing key as empty. Descriptive and provenance fields are
        # never dropped when they have a value.
        props = {
            "camera_id": cam["camera_id"],
            "osm_url": f"https://www.openstreetmap.org/node/{cam['osm_id']}",
            "state_code": cam["state_code"],
            "status": status,
        }
        optional = {
            "operator": cam["operator"],
            "brand": cam["brand"],
            "direction": cam["direction"],
            "last_edited": cam.get("last_edited"),
            "has_cone": True if bearings else None,
            "match_count": len(within) or None,
            "matches": compact_matches(cam_matches),
            "road_rule_id": cam.get("road_rule_id"),
            "road_ref": cam.get("road_ref"),
            "road_phase": cam.get("road_phase"),
            "road_flag": True if road_flag else None,
            "road_eval": cam.get("road_eval"),
        }
        props.update({k: v for k, v in optional.items() if v is not None})
        feature = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [cam["lon"], cam["lat"]]},
            "properties": props,
        }
        cam_cones = [{
            "type": "Feature",
            "geometry": {"type": "Polygon",
                         "coordinates": viewcones.cone_polygon(cam["lat"], cam["lon"], b)},
            "properties": {"camera_id": cam["camera_id"], "status": status,
                           "bearing": b},
        } for b in bearings]
        if status == "checked_clear" and not cam_matches:
            # Tile geometry is rounded per zoom (tens of meters at zoom 6),
            # so the exact position rides along for the detail panel.
            props["lat_e7"] = round(cam["lat"] * 1e7)
            props["lon_e7"] = round(cam["lon"] * 1e7)
            clear_cams.append(feature)
            clear_cones.extend(cam_cones)
        else:
            cam_features.append(feature)
            cone_features.extend(cam_cones)
    write_geojson("cameras.geojson", cam_features)
    write_geojson("view_cones.geojson", cone_features)

    # Never-flaggable cameras and their cones are static vector tiles the
    # map reads by range request. Retired GeoJSON shards must not linger.
    for old in (*config.OUT_DIR.glob("*_clear_*.geojson"), config.OUT_DIR / "places.geojson"):
        old.unlink(missing_ok=True)
    clear_tiles = {}
    for name, feats, layer, lo, hi in (
            (config.CLEAR_CAMERAS_ARCHIVE, clear_cams, "clear_cameras",
             config.CLEAR_CAMERAS_ZOOMS[0], config.CLEAR_CAMERAS_ZOOMS[1]),
            (config.CLEAR_CONES_ARCHIVE, clear_cones, "clear_cones",
             config.CONE_TILE_ZOOM, config.CONE_TILE_ZOOM)):
        path = config.OUT_DIR / name
        if feats:
            write_tiles(name, feats, layer, lo, hi)
            clear_tiles[layer] = len(feats)
        else:
            path.unlink(missing_ok=True)

    # places.pmtiles - only places involved in >=1 match (keeps it small)
    matched_place_ids = {m["place_id"] for m in matches}
    place_features = []
    for places in places_by_cat.values():
        for p in places:
            # Guideline categories are rare and the point is seeing them,
            # so those places are emitted whether or not a camera is near.
            if p["place_id"] in matched_place_ids or p["category"] in guide_cats:
                place_features.append({
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"]]},
                    "properties": {
                        "place_id": p["place_id"],
                        "name": p["name"],
                        "category": p["category"],
                        "state_code": p["state_code"],
                        "religion": p.get("religion"),
                        "overture_category": p.get("overture_category"),
                    },
                })
    if place_features:
        # Symbols are laid out across tiles, so no edge buffer copies.
        write_tiles(config.PLACES_ARCHIVE, place_features, "places",
                    config.PLACES_ZOOMS[0], config.PLACES_ZOOMS[1], point_buffer=False)
    else:
        (config.OUT_DIR / config.PLACES_ARCHIVE).unlink(missing_ok=True)

    # states.geojson - Overture division polygons annotated with law status.
    # States not yet reviewed get no feature at all (no overlay = no claim
    # either way).
    reviewed = {
        s["state_code"]: s for s in rules_doc.get("states_reviewed", [])
    }
    rule_verified = {}
    for r in rules:
        lv = str(r.get("last_verified") or "")
        rule_verified[r["state_code"]] = max(
            rule_verified.get(r["state_code"], ""), lv)
    state_features = []
    for code, div in sorted(divisions.items()):
        if code in rule_states:
            law_status = "siting_law"
            last_verified = rule_verified.get(code)
        elif code in guide_states:
            law_status = "guideline"
            last_verified = str(max(
                str(g.get("last_verified") or "")
                for g in guideline_rules if g["state_code"] == code))
        elif code in reviewed:
            law_status = "reviewed_no_law"
            last_verified = str(reviewed[code].get("last_verified") or "")
        else:
            continue
        state_features.append({
            "type": "Feature",
            "geometry": div["geometry"],
            "properties": {
                "name": div["name"],
                "state_code": code,
                "division_id": div["division_id"],
                "law_status": law_status,
                "last_verified": last_verified,
                "statute_status": reviewed.get(code, {}).get("statute_status"),
                "summary": reviewed.get(code, {}).get("summary"),
                "source_url": reviewed.get(code, {}).get("source_url"),
                "guideline_title": "; ".join(sorted({
                    g["title"] for g in guideline_rules
                    if g["state_code"] == code})) or None,
            },
        })
    # states.geojson stays as the full-resolution seed for the next build's
    # division lookup (the app never fetches it); states.pmtiles is what
    # the map reads.
    write_geojson("states.geojson", state_features)
    write_tiles(config.STATES_ARCHIVE, state_features, "states",
                config.STATES_ZOOMS[0], config.STATES_ZOOMS[1])
    siting = [f for f in state_features
              if f["properties"]["law_status"] == "siting_law"]
    rule_bounds = (
        [round(v, 4) for v in vectortiles.bounds(siting)] if siting else None)

    # rules.json - public view of the curated rules for popups / report
    # drafting (maintainer_notes never leave the YAML)
    onprem_default = rules_doc.get("default_on_premises_m", 50)
    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "default_heuristic_buffer_m": default_buffer,
        "heuristic_scan_max_m": scan_max,
        "sliders": {
            "close_by": {"min": 50, "max": scan_max, "step": 25,
                         "default": default_buffer},
            "on_premises": {"min": 10, "max": 150, "step": 10,
                            "default": onprem_default},
        },
        "view_cone_heuristic": {
            "half_angle_deg": viewcones.CONE_HALF_ANGLE_DEG,
            "range_m": viewcones.CONE_RANGE_M,
        },
        "rule_states": sorted(rule_states),
        "camera_count": len(cameras),
        "clear_camera_count": len(clear_cams),
        # Optional archives: the web app only registers sources for these.
        "clear_cone_count": len(clear_cones),
        "place_count": len(place_features),
        "rule_bounds": rule_bounds,
        "flagged_count": sum(1 for f in cam_features
                             if f["properties"]["status"] == "flagged"),
        "guideline_count": sum(1 for f in cam_features
                               if f["properties"]["status"] == "guideline"),
        "guideline_states": sorted(guide_states),
        "match_count": sum(1 for m in matches
                           if m["distance_m"] <= m["buffer_m"]
                           and not m.get("guideline")),
        "rules": [rulesfile.public_copy(r) for r in rules],
        "guideline_rules": [rulesfile.public_copy(r) for r in guideline_rules],
        "states_reviewed": [rulesfile.public_copy(s)
                            for s in rules_doc.get("states_reviewed", [])],
    }
    (config.OUT_DIR / "rules.json").write_text(
        json.dumps(meta, indent=1, default=json_default), encoding="utf-8")
    print(f"  wrote rules.json ({len(rules)} rules)")

    # Data license notice rides along with the artifacts (the refresh
    # workflow commits web/data, so the repo redistributes this data).
    (config.OUT_DIR / "DATA_LICENSE.txt").write_text(
        "Data licenses for the files in this directory\n"
        "\n"
        "cameras.geojson, view_cones.geojson, clear_cameras.pmtiles,\n"
        "clear_cones.pmtiles:\n"
        "  Derived from OpenStreetMap. (c) OpenStreetMap contributors,\n"
        "  Open Database License (ODbL) 1.0. https://www.openstreetmap.org/copyright\n"
        "\n"
        "states.geojson, states.pmtiles:\n"
        "  Derived from the Overture Maps divisions theme, which contains\n"
        "  OpenStreetMap data. (c) Overture Maps Foundation, ODbL 1.0.\n"
        "\n"
        "places.pmtiles:\n"
        "  Derived from the Overture Maps places theme. (c) Overture Maps\n"
        "  Foundation, CDLA-Permissive-2.0.\n"
        "  https://cdla.dev/permissive-2-0/\n"
        "\n"
        "rules.json:\n"
        "  Hand-curated by the project (see repository LICENSE). Statutory\n"
        "  quotes are public records.\n",
        encoding="utf-8")
    return meta


def write_geojson(name: str, features: list[dict]) -> None:
    path = config.OUT_DIR / name
    path.write_text(json.dumps(
        {"type": "FeatureCollection", "features": features},
        default=json_default, separators=(",", ":")), encoding="utf-8")
    print(f"  wrote {name} ({len(features)} features, {path.stat().st_size/1e6:.1f} MB)")


def write_tiles(name: str, features: list[dict], layer: str, minzoom: int,
                maxzoom: int, point_buffer: bool = True) -> None:
    stats = vectortiles.write_pmtiles(
        config.OUT_DIR / name, features, layer, minzoom, maxzoom,
        point_buffer=point_buffer)
    print(f"  wrote {name} ({len(features)} features, {stats['tiles']} tiles, "
          f"{stats['bytes']/1e6:.1f} MB)")
