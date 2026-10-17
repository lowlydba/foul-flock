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
                      cameras weekly and everything else monthly. "roads"
                      also redoes every state-road lookup instead of reusing
                      the previous build's; it is not part of "all".
    --states          Restrict build to a subset of rule states.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vectortiles  # noqa: E402  (sibling module; build/ is not a package)

ROOT = Path(__file__).resolve().parent.parent

# CI pipes stdout, which makes Python block-buffer it; line-buffer so
# progress lines stream live in the Actions log instead of in one burst.
sys.stdout.reconfigure(line_buffering=True)
RULES_FILE = ROOT / "rules" / "alpr_state_rules.yaml"
CACHE_DIR = ROOT / "cache"
OUT_DIR = ROOT / "web" / "data"
# Cameras with no candidate place within the scan radius can never be
# flagged at any slider setting, so they ship as static vector tiles
# (PMTiles) behind the map's off-by-default "Nothing found" layer instead of
# in the core cameras.geojson, which the slider logic keeps in memory.
CLEAR_CAMERAS_ARCHIVE = "clear_cameras.pmtiles"
CLEAR_CONES_ARCHIVE = "clear_cones.pmtiles"
PLACES_ARCHIVE = "places.pmtiles"
STATES_ARCHIVE = "states.pmtiles"
# Zoom ranges per archive. Clear cameras show from zoom 6 (state level) and
# are exact to ~2 m by zoom 12, which MapLibre overzooms beyond. Cones draw
# at zoom 13+ and one tile level overzooms cleanly. Places draw from zoom
# 10. State outlines are ~1 km simplified, so zoom 7 tiles overzoom fine.
CLEAR_CAMERAS_ZOOMS = (6, 12)
CONE_TILE_ZOOM = 13
PLACES_ZOOMS = (10, 12)
STATES_ZOOMS = (0, 7)
TAXONOMY_CSV = Path(__file__).resolve().parent / "vendor" / "overture_taxonomy.csv"

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

# --------------------------------------------------- rules file schema ----

RULES_SCHEMA_VERSION = 2
AUTHORITIES = {"statute", "executive_action", "statute_pending_distance"}
MECHANISMS = {"proximity", "road_class"}
DISTANCE_BASES = {"statutory", "on_premises", "close_by", "tbd"}
CONFIDENCES = {"verified", "inferred", "needs_review"}
STATUTE_STATUSES = {"no_siting_law", "no_alpr_statute"}
SOURCE_TYPES = {"primary", "secondary"}
PRIVATE_FIELDS = ("maintainer_notes",)


def _strings(obj, path=""):
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _strings(v, f"{path}[{i}]")


def _check_sources(srcs, e):
    if not isinstance(srcs, list) or not srcs:
        e("sources must be a non-empty list of {url, type}")
        return
    for s in srcs:
        if not isinstance(s, dict) or not str(s.get("url", "")).startswith("https://"):
            e(f"source needs an https url: {s!r}")
        elif s.get("type") not in SOURCE_TYPES:
            e(f"source type must be one of {sorted(SOURCE_TYPES)}: {s!r}")


def validate_rules_doc(doc: dict, taxonomy: dict | None = None) -> list[str]:
    """Return a list of problems with the rules file (empty = valid).
    With a taxonomy, also checks every category anchor exists in it."""
    errs: list[str] = []
    if doc.get("schema_version") != RULES_SCHEMA_VERSION:
        errs.append(f"schema_version must be {RULES_SCHEMA_VERSION}")
    for key in ("default_heuristic_buffer_m", "default_on_premises_m"):
        v = doc.get(key)
        if not isinstance(v, (int, float)) or v <= 0:
            errs.append(f"{key} must be a positive number")
    mapping = doc.get("category_overture_mapping") or {}
    cats = set(mapping)
    for cat, anchors in mapping.items():
        if not anchors:
            errs.append(f"category_overture_mapping {cat}: needs at least one anchor")
        for a in anchors or []:
            if isinstance(a, dict) and (
                    set(a) - {"code", "descendants", "name_pattern"}
                    or not isinstance(a.get("descendants", True), bool)):
                errs.append(f"category_overture_mapping {cat}: bad anchor {a!r}")
                continue
            if not isinstance(a, (str, dict)) or (isinstance(a, dict)
                                                  and "code" not in a):
                errs.append(f"category_overture_mapping {cat}: bad anchor {a!r}")
                continue
            if isinstance(a, dict) and "name_pattern" in a:
                try:
                    re.compile(a["name_pattern"])
                except (re.error, TypeError):
                    errs.append(f"category_overture_mapping {cat}: "
                                f"bad name_pattern in {a!r}")
                    continue
            code = anchor_spec(a)[0]
            if taxonomy is not None and code not in taxonomy:
                errs.append(f"category_overture_mapping {cat}: anchor "
                            f"{code!r} is not in the vendored taxonomy")

    seen: set[str] = set()
    statutory_states: set[str] = set()
    guideline_states: set[str] = set()
    for i, r in enumerate(doc.get("rules") or []):
        rid = r.get("rule_id") or f"rules[{i}]"

        def e(msg, rid=rid):
            errs.append(f"{rid}: {msg}")

        if not isinstance(r.get("rule_id"), str) or not r["rule_id"].strip():
            e("rule_id must be a nonempty string")

        if rid in seen:
            e("duplicate rule_id")
        seen.add(rid)
        code, auth, mech = r.get("state_code"), r.get("authority"), r.get("mechanism")
        if code not in STATE_NAMES:
            e(f"unknown state_code {code!r}")
        if auth not in AUTHORITIES:
            e(f"authority must be one of {sorted(AUTHORITIES)}")
        if mech not in MECHANISMS:
            e(f"mechanism must be one of {sorted(MECHANISMS)}")
        if not r.get("title"):
            e("title is required")
        for f in ("effective_date", "last_verified"):
            if not isinstance(r.get(f), date):
                e(f"{f} must be a YAML date")
        if r.get("confidence") not in CONFIDENCES:
            e(f"confidence must be one of {sorted(CONFIDENCES)}")
        _check_sources(r.get("sources"), e)

        if auth == "statute":
            statutory_states.add(code)
            if mech != "proximity":
                e("statutes must use mechanism proximity")
            for f in ("citation", "statutory_language"):
                if not r.get(f):
                    e(f"{f} is required for statutes")
            if not isinstance(r.get("language_verbatim"), bool):
                e("language_verbatim (true/false) is required for statutes")
            if r.get("distance_basis") == "tbd":
                e("a statute in force cannot have distance_basis tbd")
            if (r.get("distance_basis") == "statutory"
                    and r.get("language_verbatim") is not True):
                e("a statutory distance needs language_verbatim: true, "
                  "quoting the text it comes from")
        else:
            guideline_states.add(code)
            if not r.get("summary"):
                e("summary (public text) is required for non-statutes")
            if r.get("distance_basis") == "statutory":
                e("only a statute can have distance_basis statutory")
            if (auth == "statute_pending_distance" and mech == "proximity"
                    and r.get("distance_basis") != "tbd"):
                e("statute_pending_distance needs distance_basis tbd")

        if mech == "proximity":
            if r.get("restricted_category") not in cats:
                e(f"restricted_category {r.get('restricted_category')!r} "
                  "is not in category_overture_mapping")
            basis = r.get("distance_basis")
            if basis not in DISTANCE_BASES:
                e(f"distance_basis must be one of {sorted(DISTANCE_BASES)}")
            bm = r.get("buffer_meters")
            if basis == "statutory":
                if not isinstance(bm, (int, float)) or bm <= 0:
                    e("distance_basis statutory needs a numeric buffer_meters "
                      "taken from the statute")
            elif bm is not None:
                e("buffer_meters must be null unless distance_basis is statutory")
        elif mech == "road_class":
            if auth == "statute":
                e("road_class is for guidelines only")
            try:
                re.compile(r.get("road_ref_regex") or "")
            except re.error as ex:
                e(f"road_ref_regex does not compile: {ex}")
            if not r.get("road_ref_regex"):
                e("road_ref_regex is required")
            rb = r.get("road_buffer_m")
            if not isinstance(rb, (int, float)) or rb <= 0:
                e("road_buffer_m must be a positive number")
            gc = r.get("grandfather_cutoff")
            if gc is not None and not isinstance(gc, date):
                e("grandfather_cutoff must be null or a YAML date")
            if "distance_basis" in r:
                e("road_class rules use road_buffer_m, not distance_basis")

    reviewed: set[str] = set()
    for i, s in enumerate(doc.get("states_reviewed") or []):
        code = s.get("state_code")

        def e(msg, code=code, i=i):
            errs.append(f"states_reviewed {code or i}: {msg}")

        if code not in STATE_NAMES:
            e(f"unknown state_code {code!r}")
        if code in reviewed:
            e("duplicate entry")
        reviewed.add(code)
        if s.get("statute_status") not in STATUTE_STATUSES:
            e(f"statute_status must be one of {sorted(STATUTE_STATUSES)}")
        if s.get("confidence") not in CONFIDENCES:
            e(f"confidence must be one of {sorted(CONFIDENCES)}")
        summ = s.get("summary")
        if not isinstance(summ, str) or not summ.strip():
            e("summary is required")
        if not isinstance(s.get("last_verified"), date):
            e("last_verified must be a YAML date")
        _check_sources(s.get("sources"), e)
        if code in statutory_states:
            e("has a statutory siting rule, so it must not be listed here")
    for code in sorted(guideline_states - statutory_states - reviewed):
        errs.append(f"states_reviewed: {code} has guideline rules, so it "
                    "needs an entry describing its statutory position")

    # Public text must read as finished sentences and avoid em dashes.
    public = [("states_reviewed", doc.get("states_reviewed")),
              ("rules", doc.get("rules"))]
    for name, block in public:
        for path, s in _strings(block, name):
            if "\u2014" in s:
                errs.append(f"{path}: contains an em dash")
            if (path.endswith((".summary", ".maintainer_notes"))
                    and not s.rstrip().endswith((".", ")", '"'))):
                errs.append(f"{path}: should end with a full sentence")
    return errs


def public_copy(entry: dict) -> dict:
    """An exported copy of a rule/state entry without maintainer-only
    fields."""
    return {k: v for k, v in entry.items() if k not in PRIVATE_FIELDS}


def load_rules_doc(path: Path, taxonomy: dict | None = None) -> dict:
    """Parse, validate, and normalize the rules file. Exits with every
    problem listed if validation fails. Adds statutory_rules,
    guideline_rules, and the derived fields the matcher and UI use."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    errs = validate_rules_doc(doc, taxonomy or load_taxonomy())
    if errs:
        sys.exit("rules file is invalid:\n  - " + "\n  - ".join(errs))

    def primary_url(entry):
        srcs = entry["sources"]
        return next((s["url"] for s in srcs if s["type"] == "primary"),
                    srcs[0]["url"])

    on_prem = doc["default_on_premises_m"]
    for r in doc["rules"]:
        r["source_url"] = primary_url(r)
        basis = r.get("distance_basis")
        r["buffer_specified"] = basis == "statutory"
        r["heuristic_buffer_meters"] = (
            on_prem if basis in ("on_premises", "tbd") else None)
        r["distance_tbd"] = basis == "tbd"
    for s in doc["states_reviewed"]:
        s["source_url"] = primary_url(s)
    doc["statutory_rules"] = [r for r in doc["rules"]
                              if r["authority"] == "statute"]
    doc["guideline_rules"] = [r for r in doc["rules"]
                              if r["authority"] != "statute"]
    return doc

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

# Religion for worship-place icon variants, read from the place's Overture
# taxonomy hierarchy: any denominational place of worship carries its
# faith's node among its ancestors.
RELIGION_BY_ANCESTOR = {
    "christian_place_of_worship": "christian",
    "muslim_place_of_worship": "muslim",
    "jewish_place_of_worship": "jewish",
    "buddhist_place_of_worship": "buddhist",
    "hindu_place_of_worship": "hindu",
    "sikh_place_of_worship": "sikh",
    "shinto_shrine": "shinto",
}


def religion_from_hierarchy(hierarchy: list[str] | None) -> str | None:
    for code in hierarchy or []:
        if code in RELIGION_BY_ANCESTOR:
            return RELIGION_BY_ANCESTOR[code]
    return None


# --------------------------------------------------------------- fetch ----

def http_post_overpass(data: str, retries: int = 10,
                       timeout: int = 90) -> bytes:
    """POST to Overpass, rotating across mirrors with backoff on 429/504."""
    for attempt in range(retries):
        url = OVERPASS_URLS[attempt % len(OVERPASS_URLS)]
        try:
            req = urllib.request.Request(
                url,
                data=("data=" + urllib.parse.quote(data)).encode(),
                headers={"User-Agent": "alpr-siting-compliance-map/0.1 (advocacy research tool)"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
            # A timed-out or out-of-memory query still answers 200, with the
            # failure in a top-level "remark" beside a partial result.
            remark = json.loads(body).get("remark", "")
            if "runtime error" in remark:
                raise RuntimeError("Overpass runtime error (partial result)")
            return body
        except Exception as e:  # noqa: BLE001
            if attempt == retries - 1:
                raise
            wait = min(15 * (attempt + 1), 45)
            host = urllib.parse.urlparse(url).netloc
            print(f"    {host} failed ({e}); retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError("unreachable")


def overpass_query(cache_key: str, query: str, offline: bool,
                   refresh_scope: str | tuple[str, ...] | None = "cameras",
                   required: bool = True, timeout: int = 90) -> dict | None:
    """Run an Overpass query with on-disk caching.

    refresh_scope: the --refresh scope(s) that invalidate this cache entry
    (None = never; used for historical as-of snapshots, which can't change).
    required: when False, an offline cache miss returns None instead of
    exiting, for optional guideline data.
    timeout: client read timeout in seconds (heavy queries need more).
    """
    cache_file = CACHE_DIR / f"{cache_key}.json"
    scopes = ({refresh_scope} if isinstance(refresh_scope, str)
              else set(refresh_scope or ()))
    refresh = bool(scopes & set(REFRESH_SCOPES))
    if cache_file.exists() and not refresh:
        print(f"    cache hit: {cache_file.name}")
        return json.loads(cache_file.read_text(encoding="utf-8"))
    if offline:
        if not required:
            print(f"    no cache for {cache_key} (offline); skipping")
            return None
        sys.exit(f"--offline set but no cache for {cache_key}")
    print(f"    querying Overpass: {cache_key} ...")
    raw = http_post_overpass(query, timeout=timeout)
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
out meta;
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
            # UTC date of the node's last OSM edit; caches from before
            # `out meta` lack it until the next cameras refresh.
            "last_edited": (el.get("timestamp") or "")[:10] or None,
            "state_code": state_code,
        })
    return cams


# ----------------------------------------------- "spirit-of" guidelines ----

ROAD_QUERY_CHUNK = 1500  # camera ids per Overpass road query


def fetch_state_road_ways(state_code: str, regex: str, buffer_m: float,
                          camera_ids: list[int],
                          offline: bool) -> list[dict] | None:
    """State-system road ways (ref matches regex) within buffer_m of any of
    the given ALPR cameras, with geometry. Queried in id chunks because one
    statewide query for thousands of cameras times out on public mirrors.
    None if any chunk is unavailable offline."""
    key = hashlib.sha1(f"{regex}|{buffer_m}".encode()).hexdigest()[:8]
    ids = sorted(camera_ids)
    ways: dict[int, dict] = {}
    names = set()
    for n, i in enumerate(range(0, len(ids), ROAD_QUERY_CHUNK)):
        chunk = ids[i:i + ROAD_QUERY_CHUNK]
        ck = hashlib.sha1(",".join(map(str, chunk)).encode()).hexdigest()[:8]
        name = f"roads_{state_code}_{key}_{n}_{ck}"
        names.add(f"{name}.json")
        q = f"""
[out:json][timeout:300];
node(id:{",".join(map(str, chunk))})->.c;
way(around.c:{buffer_m:g})["highway"]["ref"~"{regex}"];
out geom;
"""
        data = overpass_query(name, q, offline, required=False, timeout=330,
                              refresh_scope=("cameras", "roads"))
        if data is None:
            return None
        for el in data.get("elements", []):
            if el.get("type") == "way":
                ways[el["id"]] = el
    # Camera sets change between runs, which changes the chunk hashes; drop
    # superseded cache files so the cache dir doesn't grow unbounded.
    if CACHE_DIR.exists():
        for f in CACHE_DIR.glob(f"roads_{state_code}_{key}_*.json"):
            if f.name not in names:
                f.unlink()
    return list(ways.values())


def fetch_camera_ids_asof(state_code: str, as_of: date,
                          offline: bool) -> set[int] | None:
    """OSM ids of ALPR nodes that already existed on as_of (Overpass attic
    snapshot). None if unavailable offline. Snapshots never change, so they
    ignore --refresh."""
    q = f"""
[out:json][timeout:300][date:"{as_of.isoformat()}T00:00:00Z"];
area["ISO3166-2"="US-{state_code}"][admin_level=4]->.st;
node["man_made"="surveillance"]["surveillance:type"="ALPR"](area.st);
out ids;
"""
    data = overpass_query(f"cameras_asof_{state_code}_{as_of.isoformat()}", q,
                          offline, refresh_scope=None, required=False,
                          timeout=330)
    if data is None:
        return None
    return {el["id"] for el in data.get("elements", [])}


def _point_segment_m(lat: float, lon: float, a: dict, b: dict) -> float:
    """Distance (m) from a point to segment a-b, local equirectangular."""
    kx = 111_320 * math.cos(math.radians(lat))
    ky = 110_574
    ax, ay = (a["lon"] - lon) * kx, (a["lat"] - lat) * ky
    bx, by = (b["lon"] - lon) * kx, (b["lat"] - lat) * ky
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    t = 0.0 if seg2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
    return math.hypot(ax + t * dx, ay + t * dy)


def nearest_road_refs(cameras: list[dict], ways: list[dict],
                      buffer_m: float) -> dict[str, tuple[str, float]]:
    """camera_id -> (ref, distance_m) of the nearest listed way segment
    within buffer_m."""
    cell = 0.001
    grid: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for cam in cameras:
        grid[(int(cam["lat"] // cell), int(cam["lon"] // cell))].append(cam)
    pad = buffer_m / 111_000 + 1e-6
    best: dict[str, tuple[str, float]] = {}
    for way in ways:
        ref = (way.get("tags") or {}).get("ref", "")
        geom = way.get("geometry") or []
        for a, b in zip(geom, geom[1:]):
            lo_y = int((min(a["lat"], b["lat"]) - pad) // cell)
            hi_y = int((max(a["lat"], b["lat"]) + pad) // cell)
            lo_x = int((min(a["lon"], b["lon"]) - pad * 1.5) // cell)
            hi_x = int((max(a["lon"], b["lon"]) + pad * 1.5) // cell)
            for gy in range(lo_y, hi_y + 1):
                for gx in range(lo_x, hi_x + 1):
                    for cam in grid.get((gy, gx), ()):
                        d = _point_segment_m(cam["lat"], cam["lon"], a, b)
                        cur = best.get(cam["camera_id"])
                        if d <= buffer_m and (cur is None or d < cur[1]):
                            best[cam["camera_id"]] = (ref, d)
    return best


def road_eval_key(rule: dict) -> str:
    """Identifies the road-lookup configuration. Per-camera road results
    carried over from the last build count only when this key matches."""
    sig = f"{rule['road_ref_regex']}|{float(rule.get('road_buffer_m', 30))}"
    return f"{rule['rule_id']}:{hashlib.sha1(sig.encode()).hexdigest()[:8]}"


def load_previous_cameras() -> dict[str, dict]:
    """camera_id -> {lat, lon, props} from the last emitted camera files
    (cameras.geojson plus the clear-camera tiles). Seeds road lookups so a
    routine refresh only queries new or moved cameras. Empty if there is no
    readable earlier build."""
    try:
        gj = json.loads((OUT_DIR / "cameras.geojson").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    features = list(gj.get("features", []))
    tiles = OUT_DIR / CLEAR_CAMERAS_ARCHIVE
    if tiles.exists():
        try:
            features += vectortiles.read_features(tiles, "clear_cameras")
        except Exception as e:  # unreadable archive: re-query, don't fail
            print(f"    previous clear-camera tiles unreadable ({e}); "
                  "road lookups will not be reused for them")
    prev = {}
    for f in features:
        props = f["properties"]
        lon, lat = f["geometry"]["coordinates"][:2]
        if "lat_e7" in props:  # tile geometry is rounded; use the exact position
            lat, lon = props["lat_e7"] / 1e7, props["lon_e7"] / 1e7
        prev[props["camera_id"]] = {"lat": lat, "lon": lon, "props": props}
    return prev


def apply_road_guidelines(cameras: list[dict], guideline_rules: list[dict],
                          offline: bool,
                          previous: dict[str, dict] | None = None) -> int:
    """Tag cameras sitting on state-maintained roads for road_class
    guidelines. phase: "all" (no grandfather date), "existing" (already in
    OSM at the cutoff, filtered out), "new" (first appears after the cutoff:
    possibly a problem), "unknown" (as-of snapshot unavailable).

    Cameras whose id and position match the previous build, evaluated under
    the same rule configuration, reuse that result; only the rest hit
    Overpass."""
    previous = previous or {}
    n_flag = 0
    for rule in guideline_rules:
        if rule.get("mechanism") != "road_class":
            continue
        st = rule["state_code"]
        key = road_eval_key(rule)
        st_cams = [c for c in cameras if c["state_code"] == st]
        buf = float(rule.get("road_buffer_m", 30))

        on_road: dict[str, str] = {}   # camera_id -> ref
        todo = []
        for cam in st_cams:
            prev = previous.get(cam["camera_id"])
            same = (prev is not None
                    and round(prev["lat"], 6) == round(cam["lat"], 6)
                    and round(prev["lon"], 6) == round(cam["lon"], 6)
                    and prev["props"].get("road_eval") == key)
            if not same:
                todo.append(cam)
                continue
            cam["road_eval"] = key
            if prev["props"].get("road_rule_id") == rule["rule_id"]:
                on_road[cam["camera_id"]] = prev["props"].get("road_ref") or ""
        reused = len(st_cams) - len(todo)

        if todo:
            ways = fetch_state_road_ways(
                st, rule["road_ref_regex"], buf,
                [c["osm_id"] for c in todo], offline)
            if ways is None:
                print(f"    {st}: road data unavailable for {len(todo)} "
                      "cameras, guideline skipped for them")
            else:
                hits = nearest_road_refs(todo, ways, buf)
                for cam in todo:
                    cam["road_eval"] = key
                    if cam["camera_id"] in hits:
                        on_road[cam["camera_id"]] = hits[cam["camera_id"]][0]

        cutoff = rule.get("grandfather_cutoff")
        asof = (fetch_camera_ids_asof(st, cutoff, offline)
                if cutoff and on_road else None)
        for cam in st_cams:
            if cam["camera_id"] not in on_road:
                continue
            cam["road_rule_id"] = rule["rule_id"]
            cam["road_ref"] = on_road[cam["camera_id"]]
            if not cutoff:
                phase = "all"
            elif asof is None:
                phase = "unknown"
            else:
                phase = "existing" if cam["osm_id"] in asof else "new"
            cam["road_phase"] = phase
            cam["road_flag"] = phase != "existing"
            n_flag += cam["road_flag"]
        print(f"    {st}: {len(on_road)} of {len(st_cams)} cameras on state "
              f"roads ({reused} reused from the previous build, "
              f"{len(todo)} looked up)")
    return n_flag


# ------------------------------------------------- Overture fetch ----

def load_taxonomy() -> dict[str, list[str]]:
    """Parse the vendored Overture places taxonomy: primary code -> path
    from the root category down to the code itself."""
    taxonomy = {}
    with TAXONOMY_CSV.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            taxonomy[row["primary"].strip()] = [
                c.strip() for c in row["taxonomy"].split(">")]
    return taxonomy


def anchor_spec(anchor) -> tuple[str, bool]:
    """(code, include_descendants) for a category_overture_mapping entry:
    a bare string covers the whole subtree, {code, descendants: false}
    only the code itself."""
    if isinstance(anchor, str):
        return anchor, True
    return anchor["code"], anchor.get("descendants", True)


def build_category_lookup(cat_mapping: dict[str, list],
                          taxonomy: dict[str, list[str]]) -> dict[str, str]:
    """Expand the curated mapping into a flat Overture-code -> protected
    category dict. A code matches when it or a taxonomy ancestor is a
    subtree anchor (so "elementary_school" matches via "school"), or when
    it is itself an exact anchor. Anchors with a name_pattern are left to
    build_name_lookup."""
    subtree, exact = {}, {}  # anchor code -> protected category
    for protected, anchors in cat_mapping.items():
        for a in anchors:
            if isinstance(a, dict) and "name_pattern" in a:
                continue
            code, descendants = anchor_spec(a)
            (subtree if descendants else exact)[code] = protected
    return _expand_anchors(subtree, exact, taxonomy)


def _expand_anchors(subtree: dict, exact: dict,
                    taxonomy: dict[str, list[str]]) -> dict:
    lookup = {}
    for code, chain in taxonomy.items():
        for anchor in chain:
            if anchor in subtree:
                lookup[code] = subtree[anchor]
                break
            if anchor == code and code in exact:
                lookup[code] = exact[code]
                break
    return lookup


def build_name_lookup(cat_mapping: dict[str, list],
                      taxonomy: dict[str, list[str]]) -> dict[str, list]:
    """Overture code -> [(protected category, compiled regex)] for anchors
    that also require the place name to match name_pattern. Used where the
    taxonomy has no code for the protected kind of place."""
    out: dict[str, list] = defaultdict(list)
    for protected, anchors in cat_mapping.items():
        for a in anchors:
            if not (isinstance(a, dict) and "name_pattern" in a):
                continue
            code, descendants = anchor_spec(a)
            rx = re.compile(a["name_pattern"], re.IGNORECASE)
            sub, ex = ({code: protected}, {}) if descendants else (
                {}, {code: protected})
            for c in _expand_anchors(sub, ex, taxonomy):
                out[c].append((protected, rx))
    return dict(out)


def _overture_reader(overture_type: str, bbox: tuple):
    """Batches of one Overture type inside bbox. The STAC index narrows the
    read to the files that intersect, so cost tracks the bbox instead of the
    dataset's file count; an unreachable index falls back to a full scan."""
    try:
        from overturemaps import core
    except ImportError:
        sys.exit("overturemaps required for non-cached fetches: "
                 "pip install overturemaps")
    try:
        reader = core.record_batch_reader(overture_type, bbox=bbox, stac=True)
    except Exception as e:  # noqa: BLE001
        print(f"    STAC lookup failed ({e}); scanning the full dataset")
        reader = None
    if reader is None:
        # overturemaps also returns None when it fails to build a reader,
        # so None can't be read as "no data" without a second attempt.
        reader = core.record_batch_reader(overture_type, bbox=bbox)
    if reader is None:
        sys.exit(f"Overture {overture_type} reader unavailable for {bbox}")
    return reader


_PREV_DIVISIONS: dict[str, dict[str, dict]] = {}


def previous_division(state_code: str) -> dict | None:
    """A state's division entry from the last emitted states.geojson.

    State boundaries are effectively static, and CI starts with an empty
    cache dir, so re-streaming ~50 polygons from Overture on every run is
    wasted work (about 15 minutes). Seeding from the previous build skips
    it; `--refresh divisions` still forces a real fetch."""
    path = OUT_DIR / "states.geojson"
    key = str(path)
    if key not in _PREV_DIVISIONS:
        entries = {}
        try:
            gj = json.loads(path.read_text(encoding="utf-8"))
            for f in gj.get("features", []):
                p = f["properties"]
                if p.get("division_id") and f.get("geometry"):
                    entries[p["state_code"]] = {
                        "state_code": p["state_code"],
                        "name": p["name"],
                        "division_id": p["division_id"],
                        "geometry": f["geometry"],
                    }
        except (OSError, ValueError, KeyError):
            pass
        _PREV_DIVISIONS[key] = entries
    return _PREV_DIVISIONS[key].get(state_code)


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
    if "divisions" not in REFRESH_SCOPES:
        seeded = previous_division(state_code)
        if seeded is not None:
            print(f"    seeded from previous build: {state_code}")
            return seeded
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
                          offline: bool,
                          known_codes: set[str] | None = None,
                          name_lookup: dict[str, list] | None = None
                          ) -> list[dict]:
    """Protected places of all needed categories in one state, streamed
    from the Overture places theme and filtered locally.

    The bbox comes from the state's division polygon; a point-in-polygon
    pass drops neighbors whose bboxes overlap. Cached post-filter, keyed
    with the category codes used so mapping changes self-invalidate.
    known_codes (the vendored taxonomy's codes) lets the stream report
    places whose category the vendored taxonomy doesn't know.
    """
    name_lookup = name_lookup or {}
    mapping_key = sorted(c for c, p in code_lookup.items() if p in needed_cats)
    mapping_key += sorted(f"{c}|{p}|{rx.pattern}"
                          for c, rules in name_lookup.items()
                          for p, rx in rules if p in needed_cats)
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
    unknown: dict[str, int] = defaultdict(int)
    reader = _overture_reader("place", (minx, miny, maxx, maxy))
    for batch in reader:
        if "taxonomy" not in batch.schema.names:
            sys.exit("Overture places have no `taxonomy` column; update "
                     "overturemaps and build/vendor/overture_taxonomy.csv")
        total += batch.num_rows
        # Decode only the taxonomy column up front; the other columns are
        # read for the few rows that match.
        taxonomy = batch.column("taxonomy").to_pylist()
        for i, tax in enumerate(taxonomy):
            code = (tax or {}).get("primary")
            if known_codes is not None and code and code not in known_codes:
                unknown[code] += 1
            protected = code_lookup.get(code)
            if protected is not None and protected not in needed_cats:
                protected = None
            pending = [] if protected else [
                p for p, _ in name_lookup.get(code, ()) if p in needed_cats]
            if protected is None and not pending:
                continue
            geom = shapely.from_wkb(batch.column("geometry")[i].as_py())
            pt = shapely.centroid(geom) if geom.geom_type != "Point" else geom
            if not shapely.contains_xy(shape, pt.x, pt.y):
                continue
            names = batch.column("names")[i].as_py() or {}
            if protected is None:
                label = names.get("primary") or ""
                protected = next(
                    (p for p, rx in name_lookup[code]
                     if p in needed_cats and rx.search(label)), None)
                if protected is None:
                    continue
            places.append({
                "place_id": f"overture-{batch.column('id')[i].as_py()}",
                "lat": round(pt.y, 7),
                "lon": round(pt.x, 7),
                "name": names.get("primary") or f"(unnamed {protected})",
                "category": protected,
                "state_code": state_code,
                "religion": (religion_from_hierarchy(tax.get("hierarchy"))
                             if protected == "place_of_worship" else None),
                "overture_category": code,
            })
    if unknown:
        top = ", ".join(f"{c} ({n})" for c, n in sorted(
            unknown.items(), key=lambda kv: -kv[1])[:5])
        print(f"    WARNING: {sum(unknown.values())} places use taxonomy "
              f"codes missing from the vendored taxonomy ({top}); refresh "
              "build/vendor/overture_taxonomy.csv")
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
                    "guideline": rule.get("authority", "statute") != "statute",
                    "distance_tbd": bool(rule.get("distance_tbd")),
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
    OUT_DIR.mkdir(parents=True, exist_ok=True)
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
        bearings = parse_directions(cam["direction"])
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
                         "coordinates": cone_polygon(cam["lat"], cam["lon"], b)},
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
    for old in (*OUT_DIR.glob("*_clear_*.geojson"), OUT_DIR / "places.geojson"):
        old.unlink(missing_ok=True)
    clear_tiles = {}
    for name, feats, layer, lo, hi in (
            (CLEAR_CAMERAS_ARCHIVE, clear_cams, "clear_cameras",
             CLEAR_CAMERAS_ZOOMS[0], CLEAR_CAMERAS_ZOOMS[1]),
            (CLEAR_CONES_ARCHIVE, clear_cones, "clear_cones",
             CONE_TILE_ZOOM, CONE_TILE_ZOOM)):
        path = OUT_DIR / name
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
        write_tiles(PLACES_ARCHIVE, place_features, "places",
                    PLACES_ZOOMS[0], PLACES_ZOOMS[1], point_buffer=False)
    else:
        (OUT_DIR / PLACES_ARCHIVE).unlink(missing_ok=True)

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
    write_tiles(STATES_ARCHIVE, state_features, "states",
                STATES_ZOOMS[0], STATES_ZOOMS[1])
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
            "half_angle_deg": CONE_HALF_ANGLE_DEG,
            "range_m": CONE_RANGE_M,
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
        "rules": [public_copy(r) for r in rules],
        "guideline_rules": [public_copy(r) for r in guideline_rules],
        "states_reviewed": [public_copy(s)
                            for s in rules_doc.get("states_reviewed", [])],
    }
    (OUT_DIR / "rules.json").write_text(
        json.dumps(meta, indent=1, default=json_default), encoding="utf-8")
    print(f"  wrote rules.json ({len(rules)} rules)")

    # Data license notice rides along with the artifacts (the refresh
    # workflow commits web/data, so the repo redistributes this data).
    (OUT_DIR / "DATA_LICENSE.txt").write_text(
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
    path = OUT_DIR / name
    path.write_text(json.dumps(
        {"type": "FeatureCollection", "features": features},
        default=json_default, separators=(",", ":")), encoding="utf-8")
    print(f"  wrote {name} ({len(features)} features, {path.stat().st_size/1e6:.1f} MB)")


def write_tiles(name: str, features: list[dict], layer: str, minzoom: int,
                maxzoom: int, point_buffer: bool = True) -> None:
    stats = vectortiles.write_pmtiles(
        OUT_DIR / name, features, layer, minzoom, maxzoom,
        point_buffer=point_buffer)
    print(f"  wrote {name} ({len(features)} features, {stats['tiles']} tiles, "
          f"{stats['bytes']/1e6:.1f} MB)")


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
        REFRESH_SCOPES = scopes
        print(f"Refreshing: {', '.join(sorted(scopes))}")

    taxonomy = load_taxonomy()
    rules_doc = load_rules_doc(RULES_FILE, taxonomy)
    rules = rules_doc["statutory_rules"]
    cat_mapping = rules_doc["category_overture_mapping"]
    code_lookup = build_category_lookup(cat_mapping, taxonomy)
    name_lookup = build_name_lookup(cat_mapping, taxonomy)
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
        if st not in INTERIOR_POINTS:
            print(f"    [{i}/{len(div_states)}] {st}: no interior probe point, skipping")
            continue
        print(f"    [{i}/{len(div_states)}] {st}")
        divisions[st] = fetch_division(st, args.offline)
    print(f"    {len(divisions)} state polygons")

    print("\n[2/5] Fetching ALPR cameras (OSM/DeFlock tagging) ...")
    cameras: list[dict] = []
    for i, st in enumerate(camera_states, 1):
        cams = fetch_cameras(st, args.offline)
        print(f"    [{i}/{len(camera_states)}] {st}: {len(cams)} cameras")
        cameras.extend(cams)

    print("\n[3/5] Fetching protected places (Overture places) ...")
    needed_cats = defaultdict(set)  # state -> categories its rules need
    for r in rules + poi_guidelines:
        needed_cats[r["state_code"]].add(r["restricted_category"])
    places_by_cat: dict[str, list[dict]] = defaultdict(list)
    place_states = [s for s in camera_states if needed_cats[s]]
    for i, st in enumerate(place_states, 1):
        pls = fetch_overture_places(st, needed_cats[st], code_lookup,
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
    n_excl = mark_possible_exclusions(cameras, rules, rules_doc)
    print(f"    {n_excl} cameras marked possible statutory exclusion "
          f"(traffic-safety vendors)")
    n_road = apply_road_guidelines(
        cameras, guideline_rules, args.offline,
        previous={} if "roads" in REFRESH_SCOPES else load_previous_cameras())
    print(f"    {n_road} cameras flagged by state-road guidelines")
    matches = run_matching(cameras, places_by_cat, rules + poi_guidelines,
                           default_buffer, args.scan_max)
    flagged = len({m["camera_id"] for m in matches
                   if m["distance_m"] <= m["buffer_m"]
                   and not m.get("guideline")})
    print(f"    {len(matches)} candidate matches; {flagged} cameras flagged "
          f"at default buffers")

    print("\n[5/5] Emitting artifacts ...")
    meta = emit(cameras, places_by_cat, matches, rules, rules_doc,
                divisions, default_buffer, rule_states, args.scan_max,
                guideline_rules=guideline_rules)

    print(f"\nDone. {meta['flagged_count']} of {meta['camera_count']} cameras flagged "
          f"for review. Serve ./web to view the map.")


if __name__ == "__main__":
    main()
