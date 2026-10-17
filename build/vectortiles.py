#!/usr/bin/env python3
"""Static vector tiles (PMTiles) for the web map.

The static layers (clear cameras, their view cones, protected places and
state boundaries) ship as PMTiles archives that MapLibre reads with HTTP
range requests, so a first load only transfers the tiles in view. This
module is a small self-contained tiler: Web Mercator projection, tile
bucketing, Mapbox Vector Tile (MVT) encoding and a PMTiles v3 writer (via
the pinned `pmtiles` package). Polygons wider than a fraction of a tile
are clipped with shapely; everything else needs no dependency beyond
`pmtiles`.

Output is deterministic (sorted tile ids, gzip mtime 0), so an unchanged
input rebuilds byte-identical archives and keeps the refresh diff small.

Usage (regenerate a fixture archive from GeoJSON):
    python build/vectortiles.py in.geojson out.pmtiles --layer places \\
        --minzoom 10 --maxzoom 12
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import struct
import sys
from collections import defaultdict
from pathlib import Path

EXTENT = 8192          # tile grid; 1.2 m per unit at zoom 12 (MapLibre's own)
DEFAULT_BUFFER = 128   # units drawn past a tile edge (~8 px) so shapes don't seam
SMALL_SHAPE_UNITS = EXTENT // 8   # polygons smaller than this skip clipping
MAX_LAT = 85.05112878


# ----------------------------------------------------------- projection ----

def world_xy(lon: float, lat: float) -> tuple[float, float]:
    """lon/lat -> Web Mercator in [0, 1], y growing downward."""
    lat = max(-MAX_LAT, min(MAX_LAT, lat))
    s = math.sin(math.radians(lat))
    x = (lon + 180.0) / 360.0
    y = 0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)
    return x, y


def world_to_lonlat(x: float, y: float) -> tuple[float, float]:
    lon = x * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y))))
    return lon, lat


# ------------------------------------------------------------- MVT encode --

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _zigzag(n: int) -> int:
    return (n << 1) ^ (n >> 63)


def _field(number: int, wire: int, payload: bytes) -> bytes:
    key = _varint((number << 3) | wire)
    if wire == 2:
        return key + _varint(len(payload)) + payload
    return key + payload


def _value_message(v) -> bytes:
    if isinstance(v, bool):
        return _field(7, 0, _varint(int(v)))
    if isinstance(v, int):
        if v >= 0:
            return _field(5, 0, _varint(v))
        return _field(6, 0, _varint(_zigzag(v)))
    if isinstance(v, float):
        return _field(3, 1, struct.pack("<d", v))
    return _field(1, 2, str(v).encode("utf-8"))


class _Layer:
    """One MVT layer: features plus deduplicated key and value tables."""

    def __init__(self, name: str):
        self.name = name
        self.keys: dict[str, int] = {}
        self.values: dict[tuple, int] = {}
        self.value_msgs: list[bytes] = []
        self.features: list[bytes] = []

    def _tags(self, props: dict) -> list[int]:
        tags = []
        for k, v in props.items():
            if v is None:
                continue
            ki = self.keys.setdefault(k, len(self.keys))
            vk = (type(v).__name__, v)
            vi = self.values.get(vk)
            if vi is None:
                vi = self.values[vk] = len(self.value_msgs)
                self.value_msgs.append(_value_message(v))
            tags += [ki, vi]
        return tags

    def add(self, geom_type: int, commands: list[int], props: dict) -> None:
        tags = b"".join(_varint(t) for t in self._tags(props))
        geom = b"".join(_varint(c) for c in commands)
        msg = b""
        if tags:
            msg += _field(2, 2, tags)
        msg += _field(3, 0, _varint(geom_type)) + _field(4, 2, geom)
        self.features.append(msg)

    def encode(self) -> bytes:
        out = _field(15, 0, _varint(2)) + _field(1, 2, self.name.encode())
        for f in self.features:
            out += _field(2, 2, f)
        for k in self.keys:
            out += _field(3, 2, k.encode())
        for v in self.value_msgs:
            out += _field(4, 2, v)
        out += _field(5, 0, _varint(EXTENT))
        return _field(3, 2, out)   # a tile is a list of layers (field 3)


def _point_commands(x: int, y: int) -> list[int]:
    return [(1 & 7) | (1 << 3), _zigzag(x), _zigzag(y)]


def _ring_area(ring: list[tuple[int, int]]) -> int:
    return sum(ring[i][0] * ring[(i + 1) % len(ring)][1]
               - ring[(i + 1) % len(ring)][0] * ring[i][1]
               for i in range(len(ring)))


def _polygon_commands(polygons: list[list[list[tuple[int, int]]]]) -> list[int]:
    """polygons: [[exterior, *holes], ...] of closed-without-repeat rings in
    tile units. Exteriors are wound positive, holes negative (MVT spec)."""
    cmds: list[int] = []
    cx = cy = 0
    for rings in polygons:
        for i, ring in enumerate(rings):
            area = _ring_area(ring)
            if (i == 0) != (area > 0):
                ring = ring[::-1]
            cmds.append((1 & 7) | (1 << 3))
            cmds += [_zigzag(ring[0][0] - cx), _zigzag(ring[0][1] - cy)]
            cx, cy = ring[0]
            cmds.append((2 & 7) | ((len(ring) - 1) << 3))
            for x, y in ring[1:]:
                cmds += [_zigzag(x - cx), _zigzag(y - cy)]
                cx, cy = x, y
            cmds.append((7 & 7) | (1 << 3))
    return cmds


def _clean_ring(coords) -> list[tuple[int, int]] | None:
    """Round to the tile grid, drop repeats and the closing duplicate."""
    ring: list[tuple[int, int]] = []
    for x, y in coords:
        p = (round(x), round(y))
        if not ring or ring[-1] != p:
            ring.append(p)
    if len(ring) > 1 and ring[0] == ring[-1]:
        ring.pop()
    if len(ring) < 3 or _ring_area(ring) == 0:
        return None
    return ring


# ----------------------------------------------------------------- tiling --

def _polygon_parts(geometry: dict) -> list[list[list[tuple[float, float]]]]:
    if geometry["type"] == "Polygon":
        parts = [geometry["coordinates"]]
    else:
        parts = geometry["coordinates"]
    return [[[world_xy(lon, lat) for lon, lat in ring] for ring in rings]
            for rings in parts]


def _clip_part(rings_px, x0, y0, x1, y1):
    """Clip one projected polygon to a rectangle; returns polygons as lists
    of integer rings in coordinates relative to (x0, y0)'s tile origin."""
    try:
        import shapely
        from shapely.geometry import Polygon
    except ImportError:
        sys.exit("shapely required to tile large polygons: pip install shapely")
    poly = Polygon(rings_px[0], rings_px[1:])
    if not poly.is_valid:
        poly = poly.buffer(0)
    clipped = shapely.clip_by_rect(poly, x0, y0, x1, y1)
    if clipped.is_empty:
        return []
    geoms = getattr(clipped, "geoms", [clipped])
    out = []
    for g in geoms:
        if g.geom_type != "Polygon" or g.is_empty:
            continue
        rings = [list(g.exterior.coords), *[list(r.coords) for r in g.interiors]]
        out.append(rings)
    return out


def build_layer_tiles(features, layer: str, minzoom: int, maxzoom: int,
                      buffer: int = DEFAULT_BUFFER, point_buffer: bool = True):
    """Bucket GeoJSON-like features (Point, Polygon, MultiPolygon) into MVT
    tiles for every zoom in [minzoom, maxzoom]. Returns {(z, x, y): bytes}.

    Points near a tile edge are repeated in the neighbor (so circles draw
    whole across seams) unless point_buffer is False, which suits symbol
    layers: those are laid out across tiles and would draw twice."""
    features = list(features)
    projected = []
    for f in features:
        g = f["geometry"]
        if g["type"] == "Point":
            projected.append(("Point", world_xy(*g["coordinates"][:2])))
        elif g["type"] in ("Polygon", "MultiPolygon"):
            projected.append(("Polygon", _polygon_parts(g)))
        else:
            raise ValueError(f"unsupported geometry {g['type']}")

    tiles: dict[tuple[int, int, int], _Layer] = {}

    def tile_layer(key):
        return tiles.setdefault(key, _Layer(layer))

    for z in range(minzoom, maxzoom + 1):
        n = 1 << z
        scale = n * EXTENT
        for f, (kind, shape) in zip(features, projected):
            props = f["properties"]
            if kind == "Point":
                px, py = shape[0] * scale, shape[1] * scale
                tx0, ty0 = int(px // EXTENT), int(py // EXTENT)
                span = (-1, 0, 1) if point_buffer else (0,)
                for dx in span:
                    for dy in span:
                        tx, ty = tx0 + dx, ty0 + dy
                        if not (0 <= tx < n and 0 <= ty < n):
                            continue
                        lx, ly = px - tx * EXTENT, py - ty * EXTENT
                        if -buffer <= lx <= EXTENT + buffer and \
                                -buffer <= ly <= EXTENT + buffer:
                            tile_layer((z, tx, ty)).add(
                                1, _point_commands(round(lx), round(ly)), props)
                continue
            by_tile: dict[tuple[int, int], list] = defaultdict(list)
            for rings in shape:
                pts = [(x * scale, y * scale) for ring in rings for x, y in ring]
                minx = min(p[0] for p in pts)
                maxx = max(p[0] for p in pts)
                miny = min(p[1] for p in pts)
                maxy = max(p[1] for p in pts)
                small = (maxx - minx < SMALL_SHAPE_UNITS
                         and maxy - miny < SMALL_SHAPE_UNITS)
                rings_px = [[(x * scale, y * scale) for x, y in ring]
                            for ring in rings]
                for tx in range(max(0, int((minx - buffer) // EXTENT)),
                                min(n - 1, int((maxx + buffer) // EXTENT)) + 1):
                    for ty in range(max(0, int((miny - buffer) // EXTENT)),
                                    min(n - 1, int((maxy + buffer) // EXTENT)) + 1):
                        ox, oy = tx * EXTENT, ty * EXTENT
                        if small:
                            polys = [rings_px]
                        else:
                            polys = _clip_part(rings_px, ox - buffer, oy - buffer,
                                               ox + EXTENT + buffer,
                                               oy + EXTENT + buffer)
                        for poly in polys:
                            cleaned = []
                            for i, ring in enumerate(poly):
                                c = _clean_ring([(x - ox, y - oy) for x, y in ring])
                                if c is not None:
                                    cleaned.append(c)
                                elif i == 0:
                                    cleaned = []
                                    break
                            if cleaned:
                                by_tile[(tx, ty)].append(cleaned)
            for (tx, ty), polygons in by_tile.items():
                tile_layer((z, tx, ty)).add(3, _polygon_commands(polygons), props)
    return {key: layer_obj.encode() for key, layer_obj in tiles.items()}


# ----------------------------------------------------------------- PMTiles --

def bounds(features) -> tuple[float, float, float, float]:
    xs, ys = [], []

    def walk(c):
        if isinstance(c[0], (int, float)):
            xs.append(c[0])
            ys.append(c[1])
        else:
            for sub in c:
                walk(sub)

    for f in features:
        walk(f["geometry"]["coordinates"])
    return min(xs), min(ys), max(xs), max(ys)


def write_pmtiles(path: Path, features, layer: str, minzoom: int, maxzoom: int,
                  buffer: int = DEFAULT_BUFFER, point_buffer: bool = True,
                  description: str = "") -> dict:
    """Tile `features` into a single-layer PMTiles archive at `path`.
    Returns {"tiles": n, "bytes": size}."""
    try:
        from pmtiles.tile import Compression, TileType, zxy_to_tileid
        from pmtiles.writer import Writer
    except ImportError:
        sys.exit("pmtiles required: pip install pmtiles")
    features = list(features)
    if not features:
        raise ValueError(f"no features to tile for layer {layer!r}")
    tiles = build_layer_tiles(features, layer, minzoom, maxzoom, buffer,
                              point_buffer)
    west, south, east, north = bounds(features)
    fields: dict[str, str] = {}
    for f in features:
        for k, v in f["properties"].items():
            if v is not None:
                fields.setdefault(k, "Boolean" if isinstance(v, bool)
                                  else "Number" if isinstance(v, (int, float))
                                  else "String")
    header = {
        "tile_type": TileType.MVT,
        "tile_compression": Compression.GZIP,
        "min_lon_e7": round(west * 1e7), "min_lat_e7": round(south * 1e7),
        "max_lon_e7": round(east * 1e7), "max_lat_e7": round(north * 1e7),
        "center_zoom": minzoom,
        "center_lon_e7": round((west + east) / 2 * 1e7),
        "center_lat_e7": round((south + north) / 2 * 1e7),
    }
    metadata = {
        "name": layer,
        "description": description,
        "vector_layers": [{"id": layer, "fields": fields,
                           "minzoom": minzoom, "maxzoom": maxzoom}],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        writer = Writer(f)
        for key in sorted(tiles, key=lambda k: zxy_to_tileid(*k)):
            writer.write_tile(zxy_to_tileid(*key),
                              gzip.compress(tiles[key], 9, mtime=0))
        writer.finalize(header, metadata)
    return {"tiles": len(tiles), "bytes": path.stat().st_size}


# ------------------------------------------------------------------ decode --

def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def _unzigzag(n: int) -> int:
    return (n >> 1) ^ -(n & 1)


def _parse(buf: bytes):
    """Yield (field, wire, value) for a protobuf message."""
    i = 0
    while i < len(buf):
        key, i = _read_varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            val, i = _read_varint(buf, i)
        elif wire == 1:
            val, i = buf[i:i + 8], i + 8
        elif wire == 2:
            ln, i = _read_varint(buf, i)
            val, i = buf[i:i + ln], i + ln
        elif wire == 5:
            val, i = buf[i:i + 4], i + 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        yield field, wire, val


def _decode_value(buf: bytes):
    for field, _wire, val in _parse(buf):
        if field == 1:
            return val.decode("utf-8")
        if field == 3:
            return struct.unpack("<d", val)[0]
        if field == 4 or field == 5:
            return val
        if field == 6:
            return _unzigzag(val)
        if field == 7:
            return bool(val)
    return None


def _decode_geometry(geom_type: int, ints: list[int]):
    if geom_type == 1:
        pts, i, x, y = [], 0, 0, 0
        while i < len(ints):
            count = ints[i] >> 3
            i += 1
            for _ in range(count):
                x += _unzigzag(ints[i])
                y += _unzigzag(ints[i + 1])
                i += 2
                pts.append((x, y))
        return pts
    rings, ring, i, x, y = [], [], 0, 0, 0
    while i < len(ints):
        cmd, count = ints[i] & 7, ints[i] >> 3
        i += 1
        if cmd == 7:
            rings.append(ring)
            ring = []
            continue
        for _ in range(count):
            x += _unzigzag(ints[i])
            y += _unzigzag(ints[i + 1])
            i += 2
            ring.append((x, y))
    polygons = []
    for r in rings:
        if _ring_area(r) > 0 or not polygons:
            polygons.append([r])
        else:
            polygons[-1].append(r)
    return polygons


def decode_tile(data: bytes) -> dict:
    """MVT bytes -> {layer: {"extent", "features": [{"type", "properties",
    "geometry"}]}} with geometry in tile units (points: [(x, y)], polygons:
    [[ring, *holes], ...])."""
    layers = {}
    for field, _wire, raw in _parse(data):
        if field != 3:
            continue
        name, extent, keys, values, feats = "", EXTENT, [], [], []
        for lf, _w, lv in _parse(raw):
            if lf == 1:
                name = lv.decode()
            elif lf == 2:
                feats.append(lv)
            elif lf == 3:
                keys.append(lv.decode())
            elif lf == 4:
                values.append(_decode_value(lv))
            elif lf == 5:
                extent = lv
        out = []
        for fb in feats:
            tags, ints, gtype = [], [], 0
            for ff, _w, fv in _parse(fb):
                if ff == 2 or ff == 4:
                    vals, j = [], 0
                    while j < len(fv):
                        v, j = _read_varint(fv, j)
                        vals.append(v)
                    if ff == 2:
                        tags = vals
                    else:
                        ints = vals
                elif ff == 3:
                    gtype = fv
            props = {keys[tags[k]]: values[tags[k + 1]]
                     for k in range(0, len(tags), 2)}
            out.append({"type": gtype, "properties": props,
                        "geometry": _decode_geometry(gtype, ints)})
        layers[name] = {"extent": extent, "features": out}
    return layers


def read_features(path: Path, layer: str, zoom: int | None = None) -> list[dict]:
    """Decode a layer back to GeoJSON-like features in lon/lat at `zoom`
    (default: the archive's max zoom). Buffer copies of points that lie
    outside their tile are skipped, so each point appears once. Positions
    carry the tile grid's rounding (about 1.2 m at zoom 12)."""
    from pmtiles.reader import MmapSource, Reader, all_tiles

    out = []
    with open(path, "rb") as f:
        reader = Reader(MmapSource(f))
        if zoom is None:
            zoom = reader.header()["max_zoom"]
        n = 1 << zoom
        for (z, tx, ty), data in all_tiles(reader.get_bytes):
            if z != zoom:
                continue
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            lyr = decode_tile(data).get(layer)
            if not lyr:
                continue
            ext = lyr["extent"]

            def lonlat(p):
                return world_to_lonlat((tx + p[0] / ext) / n, (ty + p[1] / ext) / n)

            for feat in lyr["features"]:
                if feat["type"] == 1:
                    for p in feat["geometry"]:
                        if 0 <= p[0] < ext and 0 <= p[1] < ext:
                            out.append({"geometry": {"type": "Point",
                                                     "coordinates": list(lonlat(p))},
                                        "properties": feat["properties"]})
                else:
                    polys = [[[list(lonlat(p)) for p in ring] for ring in poly]
                             for poly in feat["geometry"]]
                    out.append({"geometry": {"type": "MultiPolygon",
                                             "coordinates": polys},
                                "properties": feat["properties"]})
    return out


# --------------------------------------------------------------------- CLI --

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("geojson", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--layer", required=True)
    ap.add_argument("--minzoom", type=int, required=True)
    ap.add_argument("--maxzoom", type=int, required=True)
    ap.add_argument("--no-point-buffer", action="store_true",
                    help="do not repeat edge points in neighbor tiles "
                         "(symbol layers)")
    args = ap.parse_args()
    gj = json.loads(args.geojson.read_text(encoding="utf-8"))
    stats = write_pmtiles(args.out, gj["features"], args.layer, args.minzoom,
                          args.maxzoom, point_buffer=not args.no_point_buffer)
    print(f"wrote {args.out} ({stats['tiles']} tiles, "
          f"{stats['bytes'] / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()
