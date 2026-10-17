"""Overture Maps: the vendored places taxonomy, category lookups, state
division polygons, and per-state protected-place streams."""

from __future__ import annotations

import csv
import io
import json
import re
import sys
import time
import urllib.request
from collections import defaultdict

from . import config


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


# ------------------------------------------------- Overture fetch ----

def load_taxonomy() -> dict[str, list[str]]:
    """Parse the vendored Overture places taxonomy: primary code -> path
    from the root category down to the code itself."""
    taxonomy = {}
    with config.TAXONOMY_CSV.open(encoding="utf-8", newline="") as fh:
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


STAC_INDEX_URL = "https://stac.overturemaps.org/{release}/collections.parquet"
# Seconds between attempts at the STAC index; Retry-After can lengthen one.
STAC_RETRY_WAITS = (10, 30, 90)

_STAC_INDEX: dict[str, object] = {}


def _http_get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.read()


def _stac_index(release: str):
    """The release's STAC index (one row per data file, with its bbox),
    downloaded once per build. overturemaps re-downloads it on every
    stac=True query (OvertureMaps/overturemaps-py#150), and a build's ~100
    queries in a row get HTTP 429 from the STAC host. None when the index
    stays unreachable after retries."""
    if release in _STAC_INDEX:
        return _STAC_INDEX[release]
    import pyarrow.parquet as pq

    url = STAC_INDEX_URL.format(release=release)
    table = None
    for attempt in range(len(STAC_RETRY_WAITS) + 1):
        try:
            table = pq.read_table(io.BytesIO(_http_get(url)))
            break
        except Exception as e:  # noqa: BLE001
            code = getattr(e, "code", None)
            retryable = isinstance(e, OSError) and (
                code is None or code == 429 or code >= 500)
            if not retryable or attempt == len(STAC_RETRY_WAITS):
                print(f"    STAC index unavailable ({e}); "
                      "scanning full datasets instead")
                break
            wait = STAC_RETRY_WAITS[attempt]
            from datetime import datetime, timezone
            from email.utils import parsedate_to_datetime

            retry_after = (getattr(e, "headers", None) or {}).get("Retry-After")
            if retry_after:
                try:
                    retry_wait = int(retry_after)
                except (TypeError, ValueError):
                    try:
                        retry_at = parsedate_to_datetime(retry_after)
                        if retry_at.tzinfo is None:
                            retry_at = retry_at.replace(tzinfo=timezone.utc)
                        retry_wait = (
                            retry_at - datetime.now(timezone.utc)).total_seconds()
                    except (TypeError, ValueError, OverflowError):
                        retry_wait = 0
                wait = max(wait, retry_wait)
            print(f"    STAC index fetch failed ({e}); retrying in {wait}s")
            time.sleep(wait)
    _STAC_INDEX[release] = table
    return table


def _stac_files(index, overture_type: str, bbox: tuple) -> list[str]:
    """S3 bucket/key paths of the type's files whose bbox meets bbox."""
    import pyarrow.compute as pc

    xmin, ymin, xmax, ymax = bbox
    hits = index.filter(
        (pc.field("collection") == overture_type)
        & (pc.field("type") == "Feature")
        & (pc.field("bbox", "xmin") < xmax) & (pc.field("bbox", "xmax") > xmin)
        & (pc.field("bbox", "ymin") < ymax) & (pc.field("bbox", "ymax") > ymin))
    return [a["aws"]["alternate"]["s3"]["href"].removeprefix("s3://")
            for a in hits.column("assets").to_pylist()]


def _files_reader(files: list[str], bbox: tuple):
    """Non-empty batches from the given Overture files, rows inside bbox."""
    import pyarrow.compute as pc
    import pyarrow.dataset as ds
    import pyarrow.fs as fs

    if not files:
        return iter(())
    xmin, ymin, xmax, ymax = bbox
    dataset = ds.dataset(files, filesystem=fs.S3FileSystem(
        anonymous=True, region="us-west-2"))
    batches = dataset.to_batches(
        filter=((pc.field("bbox", "xmin") < xmax)
                & (pc.field("bbox", "xmax") > xmin)
                & (pc.field("bbox", "ymin") < ymax)
                & (pc.field("bbox", "ymax") > ymin)),
        use_threads=True, batch_readahead=16, fragment_readahead=4)
    return (b for b in batches if b.num_rows)


def _overture_reader(overture_type: str, bbox: tuple):
    """Batches of one Overture type inside bbox. The STAC index narrows the
    read to the files that intersect, so cost tracks the bbox instead of the
    dataset's file count; an unreachable index falls back to a full scan."""
    try:
        from overturemaps import core
    except ImportError:
        sys.exit("overturemaps required for non-cached fetches: "
                 "pip install overturemaps")
    release = core.get_latest_release()
    index = _stac_index(release)
    if index is not None:
        return _files_reader(_stac_files(index, overture_type, bbox), bbox)
    reader = core.record_batch_reader(overture_type, bbox=bbox, release=release)
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
    path = config.OUT_DIR / "states.geojson"
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
    cache_file = config.CACHE_DIR / f"overture_division_{state_code}.json"
    if cache_file.exists() and "divisions" not in config.REFRESH_SCOPES:
        print(f"    cache hit: {cache_file.name}")
        return json.loads(cache_file.read_text(encoding="utf-8"))
    if "divisions" not in config.REFRESH_SCOPES:
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
                        "name": config.STATE_NAMES[state_code],
                        "division_id": cols["division_id"][i],
                        "geometry": json.loads(shapely.to_geojson(geom)),
                    }
                    config.CACHE_DIR.mkdir(exist_ok=True)
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
    cache_file = config.CACHE_DIR / f"overture_places_{state_code}.json"
    if cache_file.exists() and "places" not in config.REFRESH_SCOPES:
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
    config.CACHE_DIR.mkdir(exist_ok=True)
    cache_file.write_text(
        json.dumps({"mapping_key": mapping_key, "places": places}),
        encoding="utf-8")
    return places
