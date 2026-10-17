"""Road-class "spirit-of" guidelines: road matching, grandfather phases,
and reuse of the previous build's lookups."""

import pytest

from factories import guideline_road_rule, make_cam


def road_way(ref="SR 50"):
    # east-west road at lat 28.0
    return {"type": "way", "tags": {"ref": ref, "highway": "primary"},
            "geometry": [{"lat": 28.0, "lon": -81.001},
                         {"lat": 28.0, "lon": -80.999}]}


def test_nearest_road_refs(build_mod):
    on = make_cam(camera_id="on", lat=28.0001, lon=-81.0, state="FL")
    off = make_cam(camera_id="off", lat=28.001, lon=-81.0, state="FL")
    got = build_mod.nearest_road_refs([on, off], [road_way()], 30)
    assert set(got) == {"on"}
    assert got["on"][0] == "SR 50"
    assert got["on"][1] == pytest.approx(11.1, rel=0.05)


def test_road_guideline_phases(build_mod, monkeypatch):
    old = make_cam(camera_id="old", osm_id=1, lat=28.0001, lon=-81.0, state="TX")
    new = make_cam(camera_id="new", osm_id=2, lat=28.0001, lon=-81.0, state="TX")
    off = make_cam(camera_id="off", osm_id=3, lat=28.01, lon=-81.0, state="TX")
    monkeypatch.setattr(build_mod, "fetch_state_road_ways",
                        lambda *a, **k: [road_way("TX 71")])
    monkeypatch.setattr(build_mod, "fetch_camera_ids_asof",
                        lambda *a, **k: {1, 3})
    n = build_mod.apply_road_guidelines(
        [old, new, off], [guideline_road_rule()], offline=True)
    assert n == 1
    assert old["road_phase"] == "existing" and old["road_flag"] is False
    assert new["road_phase"] == "new" and new["road_flag"] is True
    assert "road_flag" not in off

    # no cutoff: everything on a state road is flagged
    fl = make_cam(camera_id="fl", osm_id=4, lat=28.0001, lon=-81.0, state="FL")
    rule = guideline_road_rule(state_code="FL", grandfather_cutoff=None)
    assert build_mod.apply_road_guidelines([fl], [rule], offline=True) == 1
    assert fl["road_phase"] == "all"

    # as-of snapshot unavailable: can't grandfather, flag as unknown
    monkeypatch.setattr(build_mod, "fetch_camera_ids_asof",
                        lambda *a, **k: None)
    unk = make_cam(camera_id="unk", osm_id=5, lat=28.0001, lon=-81.0, state="TX")
    build_mod.apply_road_guidelines([unk], [guideline_road_rule()], True)
    assert unk["road_phase"] == "unknown" and unk["road_flag"] is True


def test_road_lookup_reuses_previous_build(build_mod, monkeypatch):
    rule = guideline_road_rule(grandfather_cutoff=None)
    key = build_mod.road_eval_key(rule)

    def cam(cid, oid):
        return make_cam(camera_id=cid, osm_id=oid, lat=28.0001, lon=-81.0,
                        state="TX")

    kept, moved, stale, fresh, clear = (
        cam("kept", 1), cam("moved", 2), cam("stale", 3), cam("fresh", 4),
        cam("clear", 5))
    props = {"road_eval": key, "road_rule_id": rule["rule_id"],
             "road_ref": "TX 9"}
    previous = {
        "kept": {"lat": 28.0001, "lon": -81.0, "props": props},
        # same id but the node was moved
        "moved": {"lat": 28.5, "lon": -81.0, "props": props},
        # evaluated under a different rule configuration
        "stale": {"lat": 28.0001, "lon": -81.0,
                  "props": {**props, "road_eval": "other:00000000"}},
        # evaluated before and was not on a state road
        "clear": {"lat": 28.0001, "lon": -81.0,
                  "props": {"road_eval": key, "road_rule_id": None}},
    }
    queried = []
    monkeypatch.setattr(
        build_mod, "fetch_state_road_ways",
        lambda st, rx, buf, ids, off: queried.append(sorted(ids))
        or [road_way("TX 71")])
    n = build_mod.apply_road_guidelines(
        [kept, moved, stale, fresh, clear], [rule], True, previous=previous)

    assert queried == [[2, 3, 4]]            # only unknown cameras hit Overpass
    assert kept["road_ref"] == "TX 9" and kept["road_flag"] is True
    assert "road_rule_id" not in clear and clear["road_eval"] == key
    assert moved["road_ref"] == fresh["road_ref"] == "TX 71"
    assert n == 4


def test_road_guideline_skipped_without_road_data(build_mod, monkeypatch):
    cam = make_cam(camera_id="c", osm_id=1, state="TX")
    monkeypatch.setattr(build_mod, "fetch_state_road_ways",
                        lambda *a, **k: None)
    assert build_mod.apply_road_guidelines(
        [cam], [guideline_road_rule()], offline=True) == 0
    assert "road_flag" not in cam


def test_optional_overpass_miss_offline(build_mod, sandbox):
    assert build_mod.overpass_query(
        "nope", "q", offline=True, required=False) is None
    with pytest.raises(SystemExit):
        build_mod.overpass_query("nope", "q", offline=True)
