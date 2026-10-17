"""Geometry: haversine distance, the grid index, bearings, and view cones."""

import pytest


def test_haversine_one_degree_latitude(build_mod):
    # 1 degree of latitude is ~111.19 km on the sphere the pipeline uses.
    d = build_mod.haversine_m(47.0, -122.0, 48.0, -122.0)
    assert d == pytest.approx(111_195, rel=0.001)


def test_haversine_zero(build_mod):
    assert build_mod.haversine_m(47.5, -122.3, 47.5, -122.3) == 0


def test_grid_index_radius_filtering(build_mod):
    idx = build_mod.GridIndex(cell_deg=0.01)
    near = {"lat": 47.0005, "lon": -122.0, "name": "near"}
    far = {"lat": 47.02, "lon": -122.0, "name": "far"}
    idx.insert(near)
    idx.insert(far)
    hits = idx.query(47.0, -122.0, radius_m=200)
    assert [h[0]["name"] for h in hits] == ["near"]
    assert hits[0][1] == pytest.approx(55.6, rel=0.01)


def test_parse_directions_variants(build_mod):
    pd = build_mod.parse_directions
    assert pd(None) == []
    assert pd("90") == [90.0]
    assert pd(270) == [270.0]
    assert pd("NE") == [45.0]
    assert pd("90;180") == [90.0, 180.0]
    assert pd("N, S") == [0.0, 180.0]
    assert pd("garbage") == []
    assert pd("450") == [90.0]  # normalized mod 360


def test_cone_polygon_shape(build_mod):
    ring = build_mod.cone_polygon(47.0, -122.0, bearing=0.0)[0]
    # camera + 9 arc points + closing camera point
    assert len(ring) == 11
    assert ring[0] == ring[-1] == [-122.0, 47.0]
    # arc points sit ~CONE_RANGE_M away from the camera
    mid = ring[5]
    d = build_mod.haversine_m(47.0, -122.0, mid[1], mid[0])
    assert d == pytest.approx(build_mod.CONE_RANGE_M, rel=0.01)
