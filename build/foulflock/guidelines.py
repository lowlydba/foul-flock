"""Road-class "spirit-of" guidelines: state-road lookups near cameras, the
grandfather snapshot, and reuse of the previous build's results."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import date

import vectortiles

from . import config, overpass


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
        data = overpass.overpass_query(name, q, offline, required=False, timeout=330,
                              refresh_scope=("cameras", "roads"))
        if data is None:
            return None
        for el in data.get("elements", []):
            if el.get("type") == "way":
                ways[el["id"]] = el
    # Camera sets change between runs, which changes the chunk hashes; drop
    # superseded cache files so the cache dir doesn't grow unbounded.
    if config.CACHE_DIR.exists():
        for f in config.CACHE_DIR.glob(f"roads_{state_code}_{key}_*.json"):
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
    data = overpass.overpass_query(f"cameras_asof_{state_code}_{as_of.isoformat()}", q,
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
        gj = json.loads((config.OUT_DIR / "cameras.geojson").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    features = list(gj.get("features", []))
    tiles = config.OUT_DIR / config.CLEAR_CAMERAS_ARCHIVE
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
