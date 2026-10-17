"""Camera-to-place matching: haversine distance, a grid index, statutory
exclusion screening, and candidate matches within the scan radius."""

from __future__ import annotations

import math
from collections import defaultdict

from . import config


# ------------------------------------------------------------ matching ----

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * config.EARTH_R * math.asin(math.sqrt(a))


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
