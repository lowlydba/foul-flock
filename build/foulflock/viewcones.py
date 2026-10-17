"""Illustrative camera view cones from the mapped bearing."""

from __future__ import annotations

import math

from . import config


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
    d = dist_m / config.EARTH_R
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
