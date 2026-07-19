#!/usr/bin/env python3
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
                      cameras weekly and everything else monthly.
    --states          Restrict build to a subset of rule states.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

ROOT = Path(__file__).resolve().parent.parent

# CI pipes stdout, which makes Python block-buffer it; line-buffer so
# progress lines stream live in the Actions log instead of in one burst.
sys.stdout.reconfigure(line_buffering=True)
RULES_FILE = ROOT / "rules" / "alpr_state_rules.yaml"
CACHE_DIR = ROOT / "cache"
OUT_DIR = ROOT / "web" / "data"
TAXONOMY_CSV = Path(__file__).resolve().parent / "vendor" / "overture_categories.csv"

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

# Set by --refresh: cache scopes ("cameras", "places", "divisions") whose
# entries are ignored and refetched.
REFRESH_SCOPES: set[str] = set()

STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida",
    "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky",
    "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}

EARTH_R = 6371008.8  # meters

# Interior probe point per state (lon, lat), used to pull each state's
# division_area polygon from Overture with a tiny bbox query. Points sit
# well inside the land polygon, away from borders and coastlines.
INTERIOR_POINTS = {
    "AL": (-86.8, 32.8), "AK": (-152.0, 64.0), "AZ": (-111.7, 34.3),
    "AR": (-92.4, 34.9), "CA": (-119.7, 36.8), "CO": (-105.5, 39.0),
    "CT": (-72.7, 41.6), "DE": (-75.55, 39.1), "DC": (-77.02, 38.90),
    "FL": (-81.6, 28.1), "GA": (-83.4, 32.6), "HI": (-155.5, 19.6),
    "ID": (-114.6, 44.4), "IL": (-89.2, 40.0), "IN": (-86.3, 39.9),
    "IA": (-93.5, 42.0), "KS": (-98.4, 38.5), "KY": (-85.3, 37.5),
    "LA": (-92.0, 31.0), "ME": (-69.2, 45.4), "MD": (-76.8, 39.4),
    "MA": (-71.8, 42.3), "MI": (-84.6, 43.5), "MN": (-94.3, 46.3),
    "MS": (-89.7, 32.7), "MO": (-92.5, 38.5), "MT": (-109.6, 47.0),
    "NE": (-99.8, 41.5), "NV": (-116.6, 39.3), "NH": (-71.6, 43.7),
    "NJ": (-74.5, 40.1), "NM": (-106.1, 34.4), "NY": (-75.5, 42.9),
    "NC": (-79.4, 35.5), "ND": (-100.5, 47.4), "OH": (-82.8, 40.3),
    "OK": (-97.5, 35.6), "OR": (-120.6, 43.9), "PA": (-77.8, 40.9),
    "RI": (-71.55, 41.7), "SC": (-80.9, 33.9), "SD": (-100.3, 44.4),
    "TN": (-86.4, 35.8), "TX": (-99.3, 31.4), "UT": (-111.7, 39.3),
    "VT": (-72.7, 44.0), "VA": (-78.2, 37.5), "WA": (-120.4, 47.4),
    "WV": (-80.6, 38.6), "WI": (-89.8, 44.6), "WY": (-107.5, 43.0),
}

# Religion for worship-place icon variants, derived from the Overture
# category code (OSM used the religion tag; Overture encodes it in the
# taxonomy instead).
RELIGION_BY_CATEGORY = {
    "church_cathedral": "christian",
    "mosque": "muslim",
    "synagogue": "jewish",
    "buddhist_temple": "buddhist",
    "hindu_temple": "hindu",
    "sikh_temple": "sikh",
    "shinto_shrines": "shinto",
}


# --------------------------------------------------------------- fetch ----

def http_post_overpass(data: str, retries: int = 10) -> bytes:
    """POST to Overpass, rotating across mirrors with backoff on 429/504."""
    for attempt in range(retries):
        url = OVERPASS_URLS[attempt % len(OVERPASS_URLS)]
        try:
            req = urllib.request.Request(
                url,
                data=("data=" + urllib.parse.quote(data)).encode(),
                headers={"User-Agent": "alpr-siting-compliance-map/0.1 (advocacy research tool)"},
            )
            with urllib.request.urlopen(req, timeout=90) as resp:
                return resp.read()
        except Exception as e:  # noqa: BLE001
            if attempt == retries - 1:
                raise
            wait = min(15 * (attempt + 1), 45)
            print(f"    retry in {wait}s on next mirror ({e})")
            time.sleep(wait)
    raise RuntimeError("unreachable")


def overpass_query(cache_key: str, query: str, offline: bool) -> dict:
    """Run an Overpass query with on-disk caching."""
    cache_file = CACHE_DIR / f"{cache_key}.json"
    if cache_file.exists() and "cameras" not in REFRESH_SCOPES:
        print(f"    cache hit: {cache_file.name}")
        return json.loads(cache_file.read_text(encoding="utf-8"))
    if offline:
        sys.exit(f"--offline set but no cache for {cache_key}")
    print(f"    querying Overpass: {cache_key} ...")
    raw = http_post_overpass(query)
    data = json.loads(raw)
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file.write_text(json.dumps(data), encoding="utf-8")
    time.sleep(10)  # be polite to the public endpoints
    return data


def fetch_cameras(state_code: str, offline: bool) -> list[dict]:
    """ALPR camera nodes in a state (DeFlock tagging convention)."""
    q = f"""
[out:json][timeout:180];
area["ISO3166-2"="US-{state_code}"][admin_level=4]->.st;
node["man_made"="surveillance"]["surveillance:type"="ALPR"](area.st);
out body;
"""
    data = overpass_query(f"cameras_{state_code}", q, offline)
    cams = []
    for el in data.get("elements", []):
        if el.get("type") != "node":
            continue
        tags = el.get("tags", {})
        cams.append({
            "camera_id": f"osm-node-{el['id']}",
            "osm_id": el["id"],
            "lat": el["lat"],
            "lon": el["lon"],
            "operator": tags.get("operator"),
            "brand": tags.get("brand") or tags.get("manufacturer"),
            "direction": tags.get("camera:direction") or tags.get("direction"),
            "state_code": state_code,
        })
    return cams


# ------------------------------------------------- Overture fetch ----

def load_taxonomy() -> dict[str, list[str]]:
    """Parse the vendored Overture categories CSV: code -> ancestry list
    (root first, the code itself last)."""
    taxonomy = {}
    with TAXONOMY_CSV.open(encoding="utf-8") as fh:
        reader = csv.reader(fh, delimiter=";")
        next(reader)  # header
        for row in reader:
            if len(row) < 2:
                continue
            code = row[0].strip()
            chain = row[1].strip().strip("[]").split(",")
            taxonomy[code] = [c.strip() for c in chain]
    return taxonomy


def build_category_lookup(cat_mapping: dict[str, list[str]],
                          taxonomy: dict[str, list[str]]) -> dict[str, str]:
    """Expand the curated mapping into a flat Overture-code -> protected
    category dict. A code matches when it or any taxonomy ancestor is
    listed in the mapping (so "elementary_school" matches via "school")."""
    wanted = {}  # anchor code -> protected category
    for protected, anchors in cat_mapping.items():
        for a in anchors:
            wanted[a] = protected
    lookup = {}
    for code, chain in taxonomy.items():
        for anchor in chain:
            if anchor in wanted:
                lookup[code] = wanted[anchor]
                break
    return lookup


def _overture_reader(overture_type: str, bbox: tuple):
    try:
        from overturemaps import core
    except ImportError:
        sys.exit("overturemaps required for non-cached fetches: "
                 "pip install overturemaps")
    return core.record_batch_reader(overture_type, bbox=bbox)


def fetch_division(state_code: str, offline: bool) -> dict:
    """One state's Overture division_area land polygon + GERS division id.

    Uses a tiny bbox probe around an interior point; the reader returns
    every division whose bbox intersects it, and we keep the land-class
    region polygon for the requested state.
    """
    cache_file = CACHE_DIR / f"overture_division_{state_code}.json"
    if cache_file.exists() and "divisions" not in REFRESH_SCOPES:
        print(f"    cache hit: {cache_file.name}")
        return json.loads(cache_file.read_text(encoding="utf-8"))
    if offline:
        sys.exit(f"--offline set but no cache for overture_division_{state_code}")

    import shapely

    lon, lat = INTERIOR_POINTS[state_code]
    want = f"US-{state_code}"
    for pad in (0.02, 0.3):
        reader = _overture_reader(
            "division_area", (lon - pad, lat - pad, lon + pad, lat + pad))
        for batch in reader:
            cols = batch.to_pydict()
            for i in range(batch.num_rows):
                if (cols["subtype"][i] == "region"
                        and cols["region"][i] == want
                        and cols["class"][i] == "land"):
                    geom = shapely.from_wkb(cols["geometry"][i])
                    # Light simplification (~110 m tolerance, ~1 m grid
                    # snap): keeps coastline detail at street zoom while
                    # holding states.geojson to a web-friendly size.
                    geom = shapely.simplify(geom, 0.001, preserve_topology=True)
                    geom = shapely.set_precision(geom, 0.00001)
                    entry = {
                        "state_code": state_code,
                        "name": STATE_NAMES[state_code],
                        "division_id": cols["division_id"][i],
                        "geometry": json.loads(shapely.to_geojson(geom)),
                    }
                    CACHE_DIR.mkdir(exist_ok=True)
                    cache_file.write_text(json.dumps(entry), encoding="utf-8")
                    return entry
        print(f"    {state_code}: no region hit at pad {pad}, widening probe")
    sys.exit(f"Overture division_area returned no land region for {want}")


def fetch_overture_places(state_code: str, needed_cats: set[str],
                          code_lookup: dict[str, str], division: dict,
                          offline: bool) -> list[dict]:
    """Protected places of all needed categories in one state, streamed
    from the Overture places theme and filtered locally.

    The bbox comes from the state's division polygon; a point-in-polygon
    pass drops neighbors whose bboxes overlap. Cached post-filter, keyed
    with the category codes used so mapping changes self-invalidate.
    """
    mapping_key = sorted(c for c, p in code_lookup.items() if p in needed_cats)
    cache_file = CACHE_DIR / f"overture_places_{state_code}.json"
    if cache_file.exists() and "places" not in REFRESH_SCOPES:
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        if cached.get("mapping_key") == mapping_key:
            print(f"    cache hit: {cache_file.name}")
            return cached["places"]
        if offline:
            sys.exit(f"--offline set but cached overture_places_{state_code} "
                     "was built with a different category mapping")
        print(f"    {cache_file.name}: category mapping changed, refetching")
    elif offline:
        sys.exit(f"--offline set but no cache for overture_places_{state_code}")

    import shapely

    shape = shapely.from_geojson(json.dumps(division["geometry"]))
    shapely.prepare(shape)
    minx, miny, maxx, maxy = shapely.bounds(shape).tolist()

    print(f"    streaming Overture places for {state_code} "
          f"(bbox {minx:.2f},{miny:.2f},{maxx:.2f},{maxy:.2f}) ...")
    places = []
    total = 0
    reader = _overture_reader("place", (minx, miny, maxx, maxy))
    for batch in reader:
        cols = batch.to_pydict()
        total += batch.num_rows
        for i in range(batch.num_rows):
            cat_struct = cols["categories"][i]
            code = cat_struct.get("primary") if cat_struct else None
            protected = code_lookup.get(code)
            if protected is None or protected not in needed_cats:
                continue
            geom = shapely.from_wkb(cols["geometry"][i])
            pt = shapely.centroid(geom) if geom.geom_type != "Point" else geom
            if not shapely.contains_xy(shape, pt.x, pt.y):
                continue
            names = cols["names"][i] or {}
            places.append({
                "place_id": f"overture-{cols['id'][i]}",
                "lat": round(pt.y, 7),
                "lon": round(pt.x, 7),
                "name": names.get("primary") or f"(unnamed {protected})",
                "category": protected,
                "state_code": state_code,
                "religion": RELIGION_BY_CATEGORY.get(code),
                "overture_category": code,
            })
    print(f"    {state_code}: kept {len(places)} of {total} streamed places")
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file.write_text(
        json.dumps({"mapping_key": mapping_key, "places": places}),
        encoding="utf-8")
    return places


# ------------------------------------------------------------ matching ----

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


class GridIndex:
    """Simple lat/lon grid index for radius queries (cell ≈ max radius)."""

    def __init__(self, cell_deg: float):
        self.cell = cell_deg
        self.cells: dict[tuple[int, int], list[dict]] = defaultdict(list)

    def _key(self, lat: float, lon: float) -> tuple[int, int]:
        return (int(lat // self.cell), int(lon // self.cell))

    def insert(self, item: dict) -> None:
        self.cells[self._key(item["lat"], item["lon"])].append(item)

    def query(self, lat: float, lon: float, radius_m: float) -> list[tuple[dict, float]]:
        out = []
        ky, kx = self._key(lat, lon)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for item in self.cells.get((ky + dy, kx + dx), []):
                    d = haversine_m(lat, lon, item["lat"], item["lon"])
                    if d <= radius_m:
                        out.append((item, d))
        return out


def mark_possible_exclusions(cameras: list[dict], rules: list[dict],
                             rules_doc: dict) -> int:
    """Mark cameras from traffic-safety-camera vendors in states whose rules
    declare statutory exclusions. These are screened out of matching to
    reduce false positives, but kept visible on the map with an explanation."""
    watchlist = [w.lower() for w in
                 rules_doc.get("exclusion_manufacturer_watchlist", [])]
    excl_states = {r["state_code"] for r in rules if r.get("statutory_exclusions")}
    n = 0
    for cam in cameras:
        vendor = (cam.get("brand") or "").lower()
        cam["possible_exclusion"] = bool(
            cam["state_code"] in excl_states
            and vendor
            and any(w in vendor for w in watchlist)
        )
        n += cam["possible_exclusion"]
    return n


def run_matching(cameras: list[dict], places_by_cat: dict[str, list[dict]],
                 rules: list[dict], default_buffer: float,
                 scan_max: float) -> list[dict]:
    """For each camera, evaluate every rule of its state. Returns match rows.

    Rules that fall back to the global heuristic default are scanned out to
    scan_max so the web UI can let users adjust the "close by" radius at
    runtime without a rebuild. Statutory and per-rule-override distances are
    scanned exactly; they are not user-adjustable.
    """
    max_radius = max(
        [default_buffer, scan_max]
        + [r["buffer_meters"] for r in rules if r.get("buffer_meters")]
        + [r["heuristic_buffer_meters"] for r in rules
           if r.get("heuristic_buffer_meters")]
    )
    cell_deg = max(max_radius / 111_000 * 1.2, 0.005)

    indexes: dict[str, GridIndex] = {}
    for cat, places in places_by_cat.items():
        idx = GridIndex(cell_deg)
        for p in places:
            idx.insert(p)
        indexes[cat] = idx

    rules_by_state: dict[str, list[dict]] = defaultdict(list)
    for r in rules:
        rules_by_state[r["state_code"]].append(r)

    matches = []
    for cam in cameras:
        if cam.get("possible_exclusion"):
            continue
        for rule in rules_by_state.get(cam["state_code"], []):
            cat = rule["restricted_category"]
            idx = indexes.get(cat)
            if idx is None:
                continue
            # statutory distance > per-rule heuristic override > global default
            buffer_m = (rule.get("buffer_meters")
                        or rule.get("heuristic_buffer_meters")
                        or default_buffer)
            # Any non-statutory rule scans wide so both UI sliders ("close
            # by" and "on premises") can be adjusted without a rebuild.
            statutory = bool(rule.get("buffer_meters"))
            scan_m = buffer_m if statutory else max(buffer_m, scan_max)
            found = []
            for place, dist in idx.query(cam["lat"], cam["lon"], scan_m):
                if place["state_code"] != cam["state_code"]:
                    continue
                found.append((dist, place))
            # Dense categories (e.g. doctor offices) can put dozens of
            # candidates in the scan ring; keep the nearest few. Status
            # only needs the closest match, and the UI shows them sorted.
            found.sort(key=lambda t: t[0])
            for dist, place in found[:20]:
                matches.append({
                    "camera_id": cam["camera_id"],
                    "rule_id": rule["rule_id"],
                    "place_id": place["place_id"],
                    "place_name": place["name"],
                    "place_category": cat,
                    "place_lat": place["lat"],
                    "place_lon": place["lon"],
                    "distance_m": round(dist, 1),
                    "buffer_m": buffer_m,
                    "buffer_specified": bool(rule.get("buffer_specified")),
                    "rule_heuristic_m": rule.get("heuristic_buffer_meters"),
                })
    return matches


# ---------------------------------------------------------------- emit ----

def json_default(o):
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    raise TypeError(str(type(o)))


# ------------------------------------------------------------ view cones ----

CARDINAL_DEG = {
    "N": 0, "NNE": 22.5, "NE": 45, "ENE": 67.5, "E": 90, "ESE": 112.5,
    "SE": 135, "SSE": 157.5, "S": 180, "SSW": 202.5, "SW": 225,
    "WSW": 247.5, "W": 270, "WNW": 292.5, "NW": 315, "NNW": 337.5,
}

# OSM has no FOV width/range tags for ALPRs; these are illustrative defaults.
CONE_HALF_ANGLE_DEG = 30.0
CONE_RANGE_M = 60.0


def parse_directions(raw) -> list[float]:
    """Parse camera:direction - numeric degrees, cardinal names, ';'-lists."""
    if raw is None:
        return []
    out = []
    for part in str(raw).replace(",", ";").split(";"):
        part = part.strip().upper()
        if not part:
            continue
        try:
            out.append(float(part) % 360)
        except ValueError:
            if part in CARDINAL_DEG:
                out.append(CARDINAL_DEG[part])
    return out


def destination(lat: float, lon: float, bearing_deg: float, dist_m: float):
    """Great-circle destination point."""
    br = math.radians(bearing_deg)
    d = dist_m / EARTH_R
    p1 = math.radians(lat)
    l1 = math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) +
                   math.cos(p1) * math.sin(d) * math.cos(br))
    l2 = l1 + math.atan2(math.sin(br) * math.sin(d) * math.cos(p1),
                         math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), math.degrees(l2)


def cone_polygon(lat: float, lon: float, bearing: float) -> list[list[float]]:
    """Cone from camera point, CONE_HALF_ANGLE_DEG each side, arc at range."""
    ring = [[lon, lat]]
    steps = 8
    start = bearing - CONE_HALF_ANGLE_DEG
    for i in range(steps + 1):
        b = start + (2 * CONE_HALF_ANGLE_DEG) * i / steps
        plat, plon = destination(lat, lon, b, CONE_RANGE_M)
        ring.append([round(plon, 6), round(plat, 6)])
    ring.append([lon, lat])
    return [ring]


def emit(cameras, places_by_cat, matches, rules, rules_doc, divisions,
         default_buffer, rule_states, scan_max):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    matches_by_cam = defaultdict(list)
    for m in matches:
        matches_by_cam[m["camera_id"]].append(m)

    # cameras.geojson - every camera in rule states, with status + matches
    # view_cones.geojson - illustrative FOV cones where camera:direction exists
    cam_features = []
    cone_features = []
    for cam in cameras:
        cam_matches = sorted(matches_by_cam.get(cam["camera_id"], []),
                             key=lambda m: m["distance_m"])
        # status reflects the default buffers; candidates beyond them are
        # kept in the file so the UI slider can widen the radius client-side
        within = [m for m in cam_matches if m["distance_m"] <= m["buffer_m"]]
        if cam.get("possible_exclusion"):
            status = "possible_exclusion"
        else:
            status = "flagged" if within else "checked_clear"
        bearings = parse_directions(cam["direction"])
        cam_features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [cam["lon"], cam["lat"]]},
            "properties": {
                "camera_id": cam["camera_id"],
                "osm_url": f"https://www.openstreetmap.org/node/{cam['osm_id']}",
                "state_code": cam["state_code"],
                "operator": cam["operator"],
                "brand": cam["brand"],
                "direction": cam["direction"],
                "has_cone": bool(bearings),
                "status": status,
                "match_count": len(within),
                "matches": json.dumps(cam_matches),
            },
        })
        for b in bearings:
            cone_features.append({
                "type": "Feature",
                "geometry": {"type": "Polygon",
                             "coordinates": cone_polygon(cam["lat"], cam["lon"], b)},
                "properties": {"camera_id": cam["camera_id"], "status": status,
                               "bearing": b},
            })
    write_geojson("cameras.geojson", cam_features)
    write_geojson("view_cones.geojson", cone_features)

    # places.geojson - only places involved in >=1 match (keeps file small)
    matched_place_ids = {m["place_id"] for m in matches}
    place_features = []
    for places in places_by_cat.values():
        for p in places:
            if p["place_id"] in matched_place_ids:
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
    write_geojson("places.geojson", place_features)

    # states.geojson - Overture division polygons annotated with law status.
    # States not yet reviewed get no feature at all (no overlay = no claim
    # either way).
    reviewed_no_law = {
        s["state_code"]: s for s in rules_doc.get("states_reviewed_no_siting_law", [])
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
        elif code in reviewed_no_law:
            law_status = "reviewed_no_law"
            last_verified = str(reviewed_no_law[code].get("last_verified") or "")
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
                "verdict": reviewed_no_law.get(code, {}).get("verdict"),
                "note": reviewed_no_law.get(code, {}).get("note"),
            },
        })
    write_geojson("states.geojson", state_features)

    # rules.json - full curated rules for popups / report drafting
    onprem_default = min(
        [r["heuristic_buffer_meters"] for r in rules
         if r.get("heuristic_buffer_meters")] or [50])
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
            "half_angle_deg": CONE_HALF_ANGLE_DEG,
            "range_m": CONE_RANGE_M,
        },
        "rule_states": sorted(rule_states),
        "camera_count": len(cameras),
        "flagged_count": sum(1 for f in cam_features
                             if f["properties"]["status"] == "flagged"),
        "match_count": sum(1 for m in matches
                           if m["distance_m"] <= m["buffer_m"]),
        "rules": rules,
        "states_reviewed_no_siting_law":
            rules_doc.get("states_reviewed_no_siting_law", []),
    }
    (OUT_DIR / "rules.json").write_text(
        json.dumps(meta, indent=1, default=json_default), encoding="utf-8")
    print(f"  wrote rules.json ({len(rules)} rules)")

    # Data license notice rides along with the artifacts (the refresh
    # workflow commits web/data, so the repo redistributes this data).
    (OUT_DIR / "DATA_LICENSE.txt").write_text(
        "Data licenses for the files in this directory\n"
        "\n"
        "cameras.geojson, view_cones.geojson:\n"
        "  Derived from OpenStreetMap. (c) OpenStreetMap contributors,\n"
        "  Open Database License (ODbL) 1.0. https://www.openstreetmap.org/copyright\n"
        "\n"
        "states.geojson:\n"
        "  Derived from the Overture Maps divisions theme, which contains\n"
        "  OpenStreetMap data. (c) Overture Maps Foundation, ODbL 1.0.\n"
        "\n"
        "places.geojson:\n"
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
    path = OUT_DIR / name
    path.write_text(json.dumps(
        {"type": "FeatureCollection", "features": features},
        default=json_default), encoding="utf-8")
    print(f"  wrote {name} ({len(features)} features, {path.stat().st_size/1e6:.1f} MB)")


# ---------------------------------------------------------------- main ----

def main() -> None:
    global REFRESH_SCOPES
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
                         "cameras,places,divisions, or 'all' (bare flag)")
    ap.add_argument("--states", default=None, help="comma-separated subset, e.g. WA,VA")
    args = ap.parse_args()
    if args.offline and args.refresh:
        sys.exit("--offline and --refresh are mutually exclusive")
    if args.refresh:
        valid = {"cameras", "places", "divisions"}
        scopes = ({s.strip() for s in args.refresh.split(",")}
                  if args.refresh != "all" else set(valid))
        bad = scopes - valid
        if bad:
            sys.exit(f"unknown --refresh scope(s): {', '.join(sorted(bad))} "
                     f"(valid: all, {', '.join(sorted(valid))})")
        REFRESH_SCOPES = scopes
        print(f"Refreshing: {', '.join(sorted(scopes))}")

    rules_doc = yaml.safe_load(RULES_FILE.read_text(encoding="utf-8"))
    rules = rules_doc["rules"]
    cat_mapping = rules_doc["category_overture_mapping"]
    code_lookup = build_category_lookup(cat_mapping, load_taxonomy())
    default_buffer = (args.default_buffer
                      if args.default_buffer is not None
                      else rules_doc.get("default_heuristic_buffer_m", 200))

    rule_states = sorted({r["state_code"] for r in rules})
    if args.states:
        keep = {s.strip().upper() for s in args.states.split(",")}
        rule_states = [s for s in rule_states if s in keep]
        rules = [r for r in rules if r["state_code"] in keep]

    print(f"States with siting rules: {', '.join(rule_states)}")
    print(f"Default heuristic buffer: {default_buffer} m")

    print("\n[1/5] Fetching state boundaries (Overture divisions) ...")
    reviewed = [s["state_code"]
                for s in rules_doc.get("states_reviewed_no_siting_law", [])]
    divisions: dict[str, dict] = {}
    div_states = sorted(set(rule_states) | set(reviewed))
    for i, st in enumerate(div_states, 1):
        if st not in INTERIOR_POINTS:
            print(f"    [{i}/{len(div_states)}] {st}: no interior probe point, skipping")
            continue
        print(f"    [{i}/{len(div_states)}] {st}")
        divisions[st] = fetch_division(st, args.offline)
    print(f"    {len(divisions)} state polygons")

    print("\n[2/5] Fetching ALPR cameras (OSM/DeFlock tagging) ...")
    cameras: list[dict] = []
    for i, st in enumerate(rule_states, 1):
        cams = fetch_cameras(st, args.offline)
        print(f"    [{i}/{len(rule_states)}] {st}: {len(cams)} cameras")
        cameras.extend(cams)

    print("\n[3/5] Fetching protected places (Overture places) ...")
    needed_cats = defaultdict(set)  # state -> categories its rules need
    for r in rules:
        needed_cats[r["state_code"]].add(r["restricted_category"])
    places_by_cat: dict[str, list[dict]] = defaultdict(list)
    for i, st in enumerate(rule_states, 1):
        pls = fetch_overture_places(st, needed_cats[st], code_lookup,
                                    divisions[st], args.offline)
        by_cat = defaultdict(int)
        for p in pls:
            places_by_cat[p["category"]].append(p)
            by_cat[p["category"]] += 1
        print(f"    [{i}/{len(rule_states)}] {st}: "
              + ", ".join(f"{c}={n}" for c, n in sorted(by_cat.items())))

    print("\n[4/5] Matching cameras against rules ...")
    n_excl = mark_possible_exclusions(cameras, rules, rules_doc)
    print(f"    {n_excl} cameras marked possible statutory exclusion "
          f"(traffic-safety vendors)")
    matches = run_matching(cameras, places_by_cat, rules, default_buffer,
                           args.scan_max)
    flagged = len({m["camera_id"] for m in matches
                   if m["distance_m"] <= m["buffer_m"]})
    print(f"    {len(matches)} candidate matches; {flagged} cameras flagged "
          f"at default buffers")

    print("\n[5/5] Emitting artifacts ...")
    meta = emit(cameras, places_by_cat, matches, rules, rules_doc,
                divisions, default_buffer, rule_states, args.scan_max)

    print(f"\nDone. {meta['flagged_count']} of {meta['camera_count']} cameras flagged "
          f"for review. Serve ./web to view the map.")


if __name__ == "__main__":
    main()
