"""Unit tests for build/vectortiles.py: MVT encoding, tiling, and PMTiles
output. Fully offline."""

import math

import pytest

import vectortiles as vt


def point(lon, lat, **props):
    return {"geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": props}


def meters(a, b):
    """Approximate distance in meters between two lon/lat pairs."""
    dy = (a[1] - b[1]) * 111_195
    dx = (a[0] - b[0]) * 111_195 * math.cos(math.radians(a[1]))
    return math.hypot(dx, dy)


def test_points_round_trip_with_property_types(tmp_path):
    feats = [
        point(-77.4243997, 38.8940631, camera_id="a", has_cone=True, n=3,
              neg=-7, ratio=0.5, empty=None),
        point(-76.6, 36.9, camera_id="b"),
    ]
    path = tmp_path / "p.pmtiles"
    stats = vt.write_pmtiles(path, feats, "clear_cameras", 6, 12)
    assert stats["tiles"] > 0
    got = {f["properties"]["camera_id"]: f for f in
           vt.read_features(path, "clear_cameras")}
    assert set(got) == {"a", "b"}          # one copy each, no buffer duplicates
    assert got["a"]["properties"] == {
        "camera_id": "a", "has_cone": True, "n": 3, "neg": -7, "ratio": 0.5}
    # position survives to within the ~1.2 m tile grid at zoom 12
    assert meters(got["a"]["geometry"]["coordinates"],
                  (-77.4243997, 38.8940631)) < 1.5


def test_zoom_range_and_overzoom_levels(tmp_path):
    from pmtiles.reader import MmapSource, Reader
    path = tmp_path / "p.pmtiles"
    vt.write_pmtiles(path, [point(-77.0, 38.9, camera_id="a")], "l", 6, 12)
    with open(path, "rb") as f:
        header = Reader(MmapSource(f)).header()
    assert (header["min_zoom"], header["max_zoom"]) == (6, 12)


def test_points_near_a_tile_edge_repeat_in_the_neighbor(tmp_path):
    # lon -90 is the edge between tiles x=15 and x=16 at zoom 6.
    from pmtiles.reader import MmapSource, Reader, all_tiles
    import gzip
    path = tmp_path / "p.pmtiles"
    vt.write_pmtiles(path, [point(-90.00001, 40.0, camera_id="edge")], "l", 6, 6)
    with open(path, "rb") as f:
        reader = Reader(MmapSource(f))
        tiles = {k: vt.decode_tile(gzip.decompress(d))
                 for k, d in all_tiles(reader.get_bytes)}
    assert len({(z, x) for z, x, _y in tiles}) == 2     # both sides of the seam
    outside = [p for t in tiles.values() for feat in t["l"]["features"]
               for p in feat["geometry"] if not 0 <= p[0] < vt.EXTENT]
    assert len(outside) == 1                            # the buffer copy
    assert len(vt.read_features(path, "l")) == 1        # but read once


def test_no_point_buffer_for_symbol_layers(tmp_path):
    path = tmp_path / "p.pmtiles"
    stats = vt.write_pmtiles(path, [point(-90.00001, 40.0, place_id="p")],
                             "places", 10, 10, point_buffer=False)
    assert stats["tiles"] == 1


def cone(lon, lat, size=0.0005):
    ring = [[lon, lat], [lon + size, lat + size], [lon + size, lat - size],
            [lon, lat]]
    return {"geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": {"camera_id": "c", "bearing": 90}}


def ring_area(ring):
    return abs(vt._ring_area(ring)) / 2


def test_small_polygons_keep_shape_and_winding(tmp_path):
    path = tmp_path / "c.pmtiles"
    vt.write_pmtiles(path, [cone(-77.4, 38.9)], "clear_cones", 13, 13)
    got = vt.read_features(path, "clear_cones")
    assert len(got) == 1
    ring = got[0]["geometry"]["coordinates"][0][0]
    assert len(ring) == 3                      # closing duplicate dropped
    assert got[0]["properties"] == {"camera_id": "c", "bearing": 90}


def test_large_polygon_is_clipped_per_tile_and_keeps_holes(tmp_path):
    outer = [[-100, 30], [-80, 30], [-80, 45], [-100, 45], [-100, 30]]
    hole = [[-88, 35], [-84, 35], [-84, 40], [-88, 40], [-88, 35]]
    feat = {"geometry": {"type": "Polygon", "coordinates": [outer, hole]},
            "properties": {"state_code": "XX"}}
    from pmtiles.reader import MmapSource, Reader, all_tiles
    import gzip
    path = tmp_path / "s.pmtiles"
    vt.write_pmtiles(path, [feat], "states", 2, 5)
    with open(path, "rb") as f:
        reader = Reader(MmapSource(f))
        tiles = {k: vt.decode_tile(gzip.decompress(d))
                 for k, d in all_tiles(reader.get_bytes)}
    z5 = {k: v for k, v in tiles.items() if k[0] == 5}
    assert len(z5) > 4                          # spread over several tiles
    for (_z, _x, _y), t in z5.items():
        for f_ in t["states"]["features"]:
            for polygon in f_["geometry"]:
                for ring in polygon:
                    assert all(-vt.DEFAULT_BUFFER - 1 <= x <= vt.EXTENT
                               + vt.DEFAULT_BUFFER + 1 for x, _y in ring)
    # exterior rings wind positive, holes negative
    rings = [r for t in z5.values() for f_ in t["states"]["features"]
             for polygon in f_["geometry"] for r in polygon]
    assert any(vt._ring_area(r) < 0 for r in rings), "hole preserved"
    assert any(vt._ring_area(r) > 0 for r in rings)


def test_output_is_deterministic(tmp_path):
    feats = [point(-77.0 + i / 100, 38.9, camera_id=f"c{i}") for i in range(50)]
    a, b = tmp_path / "a.pmtiles", tmp_path / "b.pmtiles"
    vt.write_pmtiles(a, feats, "l", 6, 9)
    vt.write_pmtiles(b, feats, "l", 6, 9)
    assert a.read_bytes() == b.read_bytes()


def test_empty_input_is_an_error(tmp_path):
    with pytest.raises(ValueError):
        vt.write_pmtiles(tmp_path / "e.pmtiles", [], "l", 0, 1)


def test_unsupported_geometry_is_rejected(tmp_path):
    line = {"geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            "properties": {}}
    with pytest.raises(ValueError, match="LineString"):
        vt.write_pmtiles(tmp_path / "l.pmtiles", [line], "l", 0, 1)


def test_mercator_round_trip():
    for lon, lat in [(-122.3, 47.6), (0, 0), (179.7, 71.4), (-77.0, 38.9)]:
        x, y = vt.world_xy(lon, lat)
        back = vt.world_to_lonlat(x, y)
        assert back == pytest.approx((lon, lat), abs=1e-9)
