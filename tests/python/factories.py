"""Builders for the rule, camera, and place dicts the pipeline tests feed
in. Plain functions, imported by the test modules."""


def make_rule(**over):
    rule = {
        "rule_id": "T-1",
        "state_code": "WA",
        "restricted_category": "school",
        "buffer_meters": None,
        "buffer_specified": False,
        "heuristic_buffer_meters": None,
    }
    rule.update(over)
    return rule


def make_cam(lat=47.0, lon=-122.0, state="WA", **over):
    cam = {"camera_id": "cam-1", "lat": lat, "lon": lon,
           "state_code": state, "possible_exclusion": False}
    cam.update(over)
    return cam


def place_at(build_mod, lat, lon, dist_m, bearing=90.0, state="WA",
             cat="school", pid="p-1"):
    plat, plon = build_mod.destination(lat, lon, bearing, dist_m)
    return {"place_id": pid, "lat": plat, "lon": plon, "name": pid,
            "category": cat, "state_code": state}


def guideline_road_rule(**over):
    rule = {"rule_id": "G-1", "state_code": "TX",
            "authority": "executive_action", "mechanism": "road_class",
            "road_ref_regex": "x", "road_buffer_m": 30,
            "grandfather_cutoff": build_date()}
    rule.update(over)
    return rule


def build_date():
    from datetime import date
    return date(2026, 9, 8)
