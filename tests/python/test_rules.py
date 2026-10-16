"""Schema and invariant tests for rules/alpr_state_rules.yaml and the
build-side loader. Offline and fast; these guard the hand-curated legal
core against on-the-fly drift."""

import copy

import pytest


@pytest.fixture(scope="module")
def raw(build_mod):
    return build_mod.yaml.safe_load(
        build_mod.RULES_FILE.read_text(encoding="utf-8"))


@pytest.fixture()
def doc(raw):
    return copy.deepcopy(raw)


def problems(build_mod, doc):
    return "\n".join(build_mod.validate_rules_doc(doc))


def rule(doc, rule_id):
    return next(r for r in doc["rules"] if r["rule_id"] == rule_id)


def test_committed_rules_file_is_valid(build_mod, raw):
    assert build_mod.validate_rules_doc(raw, build_mod.load_taxonomy()) == []


def test_rejects_unknown_or_malformed_anchors(build_mod, doc):
    doc["category_overture_mapping"]["school"].append("not_a_real_code")
    doc["category_overture_mapping"]["courthouse"].append({"code": "courthouse", "kids": 1})
    doc["category_overture_mapping"]["food_bank"] = []
    out = "\n".join(build_mod.validate_rules_doc(doc, build_mod.load_taxonomy()))
    assert "'not_a_real_code' is not in the vendored taxonomy" in out
    assert "bad anchor" in out
    assert "needs at least one anchor" in out


def test_loader_derives_matcher_fields(build_mod):
    doc = build_mod.load_rules_doc(build_mod.RULES_FILE)
    by_id = {r["rule_id"]: r for r in doc["rules"]}
    wa, va, ct = (by_id["WA-SB6002-schools"], by_id["VA-2025-schools"],
                  by_id["CT-PA26-14-reproductive"])
    assert wa["buffer_specified"] is False and wa["heuristic_buffer_meters"] is None
    assert va["heuristic_buffer_meters"] == doc["default_on_premises_m"]
    assert ct["distance_tbd"] is True and ct["heuristic_buffer_meters"] == 50
    assert wa["source_url"].startswith("https://")
    assert {r["authority"] for r in doc["statutory_rules"]} == {"statute"}
    assert all(r["authority"] != "statute" for r in doc["guideline_rules"])
    assert all(s["source_url"].startswith("https://")
               for s in doc["states_reviewed"])


def test_states_never_double_listed(raw):
    statutory = {r["state_code"] for r in raw["rules"]
                 if r["authority"] == "statute"}
    reviewed = {s["state_code"] for s in raw["states_reviewed"]}
    assert not statutory & reviewed


def test_rejects_invented_statutory_distance(build_mod, doc):
    rule(doc, "WA-SB6002-schools")["buffer_meters"] = 100
    assert "buffer_meters must be null" in problems(build_mod, doc)


def test_statutory_basis_needs_a_number(build_mod, doc):
    rule(doc, "WA-SB6002-schools")["distance_basis"] = "statutory"
    assert "needs a numeric buffer_meters" in problems(build_mod, doc)


def test_statute_cannot_be_road_class_or_tbd(build_mod, doc):
    r = rule(doc, "WA-SB6002-schools")
    r["mechanism"] = "road_class"
    assert "statutes must use mechanism proximity" in problems(build_mod, doc)
    r["mechanism"] = "proximity"
    r["distance_basis"] = "tbd"
    assert "cannot have distance_basis tbd" in problems(build_mod, doc)


def test_guideline_needs_public_summary(build_mod, doc):
    del rule(doc, "TX-TXDOT-PAUSE")["summary"]
    assert "summary (public text) is required" in problems(build_mod, doc)


def test_road_rule_needs_valid_regex(build_mod, doc):
    rule(doc, "TX-TXDOT-PAUSE")["road_ref_regex"] = "("
    assert "does not compile" in problems(build_mod, doc)


def test_guideline_state_needs_registry_entry(build_mod, doc):
    doc["states_reviewed"] = [s for s in doc["states_reviewed"]
                              if s["state_code"] != "TX"]
    assert "TX has guideline rules" in problems(build_mod, doc)


def test_statutory_state_cannot_be_in_registry(build_mod, doc):
    entry = copy.deepcopy(doc["states_reviewed"][0])
    entry["state_code"] = "WA"
    doc["states_reviewed"].append(entry)
    assert "must not be listed here" in problems(build_mod, doc)


def test_rejects_bad_category_status_and_source(build_mod, doc):
    rule(doc, "WA-SB6002-schools")["restricted_category"] = "nope"
    doc["states_reviewed"][0]["statute_status"] = "maybe"
    doc["states_reviewed"][0]["confidence"] = "sure"
    doc["states_reviewed"][1]["sources"] = [{"url": "http://x", "type": "primary"}]
    out = problems(build_mod, doc)
    assert "not in category_overture_mapping" in out
    assert "statute_status must be one of" in out
    assert "confidence must be one of" in out
    assert "source needs an https url" in out


def test_rejects_em_dash_and_unfinished_summary(build_mod, doc):
    doc["states_reviewed"][0]["summary"] = "Cut off mid sentence"
    rule(doc, "TX-TXDOT-PAUSE")["summary"] = "A \u2014 B."
    out = problems(build_mod, doc)
    assert "should end with a full sentence" in out
    assert "contains an em dash" in out


def test_rejects_duplicate_rule_ids(build_mod, doc):
    doc["rules"].append(copy.deepcopy(doc["rules"][0]))
    assert "duplicate rule_id" in problems(build_mod, doc)


@pytest.mark.parametrize("bad", [None, "", "  ", 7])
def test_requires_nonempty_rule_id(build_mod, doc, bad):
    doc["rules"][0]["rule_id"] = bad
    assert "rule_id must be a nonempty string" in problems(build_mod, doc)


def test_loader_exits_with_all_problems(build_mod, tmp_path, doc):
    rule(doc, "WA-SB6002-schools")["buffer_meters"] = 100
    doc["schema_version"] = 1
    path = tmp_path / "bad.yaml"
    path.write_text(build_mod.yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(SystemExit) as ex:
        build_mod.load_rules_doc(path)
    assert "schema_version" in str(ex.value)
    assert "buffer_meters must be null" in str(ex.value)


def test_rejects_statutory_distance_misuse(build_mod, doc):
    wa = rule(doc, "WA-SB6002-schools")
    wa["distance_basis"] = "statutory"
    wa["buffer_meters"] = 100
    assert "needs language_verbatim: true" in problems(build_mod, doc)

    ct = rule(doc, "CT-PA26-14-reproductive")
    ct["distance_basis"] = "statutory"
    ct["buffer_meters"] = 300
    out = problems(build_mod, doc)
    assert "only a statute can have distance_basis statutory" in out
    assert "statute_pending_distance needs distance_basis tbd" in out


def test_statutory_distance_with_verbatim_quote_is_valid(build_mod, doc):
    wa = rule(doc, "WA-SB6002-schools")
    wa.update(distance_basis="statutory", buffer_meters=100,
              language_verbatim=True)
    assert problems(build_mod, doc) == ""


def test_public_copy_strips_maintainer_notes(build_mod, raw):
    r = rule(raw, "WA-SB6002-schools")
    assert r["maintainer_notes"]
    assert "maintainer_notes" not in build_mod.public_copy(r)


# ------------------------------------------------------ road query chunks ----

def test_road_query_is_chunked_and_prunes_stale_cache(build_mod, sandbox,
                                                      monkeypatch):
    build_mod.CACHE_DIR.mkdir()
    stale = build_mod.CACHE_DIR / "roads_TX_deadbeef_0_old.json"
    key = build_mod.hashlib.sha1(b"re|30.0").hexdigest()[:8]
    stale = build_mod.CACHE_DIR / f"roads_TX_{key}_9_old.json"
    stale.write_text("{}", encoding="utf-8")

    calls = []

    def fake_query(name, q, offline, **kw):
        calls.append(name)
        (build_mod.CACHE_DIR / f"{name}.json").write_text("{}", encoding="utf-8")
        return {"elements": [{"type": "way", "id": len(calls) % 2,
                              "tags": {"ref": "TX 1"}, "geometry": []}]}

    monkeypatch.setattr(build_mod, "overpass_query", fake_query)
    ids = list(range(build_mod.ROAD_QUERY_CHUNK * 2 + 5))
    ways = build_mod.fetch_state_road_ways("TX", "re", 30.0, ids, offline=False)
    assert len(calls) == 3
    assert {w["id"] for w in ways} == {0, 1}   # deduped across chunks
    assert not stale.exists()                  # superseded cache pruned

    monkeypatch.setattr(build_mod, "overpass_query",
                        lambda *a, **k: None)
    assert build_mod.fetch_state_road_ways(
        "TX", "re", 30.0, ids, offline=True) is None
