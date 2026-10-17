"""Overture taxonomy anchors, category lookups, and religion icons."""

import pytest


@pytest.fixture(scope="module")
def lookup(build_mod):
    rules_doc = build_mod.yaml.safe_load(
        build_mod.RULES_FILE.read_text(encoding="utf-8"))
    taxonomy = build_mod.load_taxonomy()
    return build_mod.build_category_lookup(
        rules_doc["category_overture_mapping"], taxonomy)


def test_taxonomy_descendants_match(lookup):
    assert lookup["school"] == "school"
    assert lookup["elementary_school"] == "school"
    assert lookup["montessori_school"] == "school"
    assert lookup["kindergarten"] == "school"


def test_taxonomy_healthcare_scope(lookup):
    # kept: facilities, ERs and urgent care, and the pediatric subtree
    assert lookup["hospital"] == "healthcare_facility"
    assert lookup["childrens_hospital"] == "healthcare_facility"
    assert lookup["emergency_department"] == "healthcare_facility"
    assert lookup["urgent_care_clinic"] == "healthcare_facility"
    assert lookup["walk_in_clinic"] == "healthcare_facility"
    assert lookup["pediatric_clinic"] == "healthcare_facility"
    assert lookup["pediatric_cardiology"] == "healthcare_facility"
    # exact anchor: the generic node matches, its practices do not
    assert lookup["outpatient_care_facility"] == "healthcare_facility"
    assert "doctors_office" not in lookup
    assert "dental_clinic" not in lookup
    assert "dermatology" not in lookup
    assert "medical_billing" not in lookup
    assert "ambulance_or_ems_service" not in lookup


def test_taxonomy_worship_and_civic(lookup):
    assert lookup["muslim_place_of_worship"] == "place_of_worship"
    assert lookup["sunni_muslim_place_of_worship"] == "place_of_worship"
    assert lookup["jewish_place_of_worship"] == "place_of_worship"
    assert lookup["religious_organization"] == "place_of_worship"
    assert lookup["monastery"] == "place_of_worship"
    assert lookup["shinto_shrine"] == "place_of_worship"
    assert lookup["courthouse"] == "courthouse"
    assert lookup["food_bank"] == "food_bank"
    # higher ed is deliberately out of scope
    assert "college_university" not in lookup


def test_guideline_categories(lookup):
    assert lookup["abortion_clinic"] == "reproductive_health_clinic"
    assert lookup["reproductive_perinatal_and_womens_care"] == "reproductive_health_clinic"
    assert "obstetrics_and_gynecology" not in lookup
    assert "immigration_assistance_service" not in lookup
    assert "immigration_law" not in lookup


def test_name_pattern_anchor(build_mod):
    taxonomy = {"svc": ["svc"], "kid": ["svc", "kid"], "other": ["other"]}
    mapping = {"imm": [{"code": "svc", "name_pattern": "immigra|refugee"}]}
    assert build_mod.build_category_lookup(mapping, taxonomy) == {}
    names = build_mod.build_name_lookup(mapping, taxonomy)
    assert set(names) == {"svc", "kid"}
    cat, rx = names["kid"][0]
    assert cat == "imm"
    assert rx.search("Center for Immigrant Development")
    assert not rx.search("Town Community Center")


def test_exact_anchor_does_not_cover_descendants(build_mod):
    taxonomy = {"a": ["a"], "b": ["a", "b"], "c": ["x", "c"]}
    mapping = {"cat1": [{"code": "a", "descendants": False}], "cat2": ["c"]}
    assert build_mod.build_category_lookup(mapping, taxonomy) == {
        "a": "cat1", "c": "cat2"}


def test_taxonomy_loads_paths(build_mod):
    tax = build_mod.load_taxonomy()
    assert tax["elementary_school"] == [
        "education", "place_of_learning", "school", "elementary_school"]
    assert tax["school"][-1] == "school"
    assert len(tax) > 2000


def test_religion_from_hierarchy(build_mod):
    rh = build_mod.religion_from_hierarchy
    assert rh(["cultural_and_historic", "place_of_worship",
               "christian_place_of_worship", "roman_catholic_place_of_worship"]) == "christian"
    assert rh(["cultural_and_historic", "place_of_worship",
               "muslim_place_of_worship"]) == "muslim"
    assert rh(["cultural_and_historic", "place_of_worship",
               "jewish_place_of_worship", "reform_jewish_place_of_worship"]) == "jewish"
    assert rh(["cultural_and_historic", "religious_organization"]) is None
    assert rh(None) is None
