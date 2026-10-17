"""The hand-curated rules file: schema validation, loading, and the public
copy of each entry that ships in rules.json."""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

from . import config, overture


# --------------------------------------------------- rules file schema ----

RULES_SCHEMA_VERSION = 2
AUTHORITIES = {"statute", "executive_action", "statute_pending_distance"}
MECHANISMS = {"proximity", "road_class"}
DISTANCE_BASES = {"statutory", "on_premises", "close_by", "tbd"}
CONFIDENCES = {"verified", "inferred", "needs_review"}
STATUTE_STATUSES = {"no_siting_law", "no_alpr_statute"}
SOURCE_TYPES = {"primary", "secondary"}
PRIVATE_FIELDS = ("maintainer_notes",)


def _strings(obj, path=""):
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _strings(v, f"{path}[{i}]")


def _check_sources(srcs, e):
    if not isinstance(srcs, list) or not srcs:
        e("sources must be a non-empty list of {url, type}")
        return
    for s in srcs:
        if not isinstance(s, dict) or not str(s.get("url", "")).startswith("https://"):
            e(f"source needs an https url: {s!r}")
        elif s.get("type") not in SOURCE_TYPES:
            e(f"source type must be one of {sorted(SOURCE_TYPES)}: {s!r}")


def validate_rules_doc(doc: dict, taxonomy: dict | None = None) -> list[str]:
    """Return a list of problems with the rules file (empty = valid).
    With a taxonomy, also checks every category anchor exists in it."""
    errs: list[str] = []
    if doc.get("schema_version") != RULES_SCHEMA_VERSION:
        errs.append(f"schema_version must be {RULES_SCHEMA_VERSION}")
    for key in ("default_heuristic_buffer_m", "default_on_premises_m"):
        v = doc.get(key)
        if not isinstance(v, (int, float)) or v <= 0:
            errs.append(f"{key} must be a positive number")
    mapping = doc.get("category_overture_mapping") or {}
    cats = set(mapping)
    for cat, anchors in mapping.items():
        if not anchors:
            errs.append(f"category_overture_mapping {cat}: needs at least one anchor")
        for a in anchors or []:
            if isinstance(a, dict) and (
                    set(a) - {"code", "descendants", "name_pattern"}
                    or not isinstance(a.get("descendants", True), bool)):
                errs.append(f"category_overture_mapping {cat}: bad anchor {a!r}")
                continue
            if not isinstance(a, (str, dict)) or (isinstance(a, dict)
                                                  and "code" not in a):
                errs.append(f"category_overture_mapping {cat}: bad anchor {a!r}")
                continue
            if isinstance(a, dict) and "name_pattern" in a:
                try:
                    re.compile(a["name_pattern"])
                except (re.error, TypeError):
                    errs.append(f"category_overture_mapping {cat}: "
                                f"bad name_pattern in {a!r}")
                    continue
            code = overture.anchor_spec(a)[0]
            if taxonomy is not None and code not in taxonomy:
                errs.append(f"category_overture_mapping {cat}: anchor "
                            f"{code!r} is not in the vendored taxonomy")

    seen: set[str] = set()
    statutory_states: set[str] = set()
    guideline_states: set[str] = set()
    for i, r in enumerate(doc.get("rules") or []):
        rid = r.get("rule_id") or f"rules[{i}]"

        def e(msg, rid=rid):
            errs.append(f"{rid}: {msg}")

        if not isinstance(r.get("rule_id"), str) or not r["rule_id"].strip():
            e("rule_id must be a nonempty string")

        if rid in seen:
            e("duplicate rule_id")
        seen.add(rid)
        code, auth, mech = r.get("state_code"), r.get("authority"), r.get("mechanism")
        if code not in config.STATE_NAMES:
            e(f"unknown state_code {code!r}")
        if auth not in AUTHORITIES:
            e(f"authority must be one of {sorted(AUTHORITIES)}")
        if mech not in MECHANISMS:
            e(f"mechanism must be one of {sorted(MECHANISMS)}")
        if not r.get("title"):
            e("title is required")
        for f in ("effective_date", "last_verified"):
            if not isinstance(r.get(f), date):
                e(f"{f} must be a YAML date")
        if r.get("confidence") not in CONFIDENCES:
            e(f"confidence must be one of {sorted(CONFIDENCES)}")
        _check_sources(r.get("sources"), e)

        if auth == "statute":
            statutory_states.add(code)
            if mech != "proximity":
                e("statutes must use mechanism proximity")
            for f in ("citation", "statutory_language"):
                if not r.get(f):
                    e(f"{f} is required for statutes")
            if not isinstance(r.get("language_verbatim"), bool):
                e("language_verbatim (true/false) is required for statutes")
            if r.get("distance_basis") == "tbd":
                e("a statute in force cannot have distance_basis tbd")
            if (r.get("distance_basis") == "statutory"
                    and r.get("language_verbatim") is not True):
                e("a statutory distance needs language_verbatim: true, "
                  "quoting the text it comes from")
        else:
            guideline_states.add(code)
            if not r.get("summary"):
                e("summary (public text) is required for non-statutes")
            if r.get("distance_basis") == "statutory":
                e("only a statute can have distance_basis statutory")
            if (auth == "statute_pending_distance" and mech == "proximity"
                    and r.get("distance_basis") != "tbd"):
                e("statute_pending_distance needs distance_basis tbd")

        if mech == "proximity":
            if r.get("restricted_category") not in cats:
                e(f"restricted_category {r.get('restricted_category')!r} "
                  "is not in category_overture_mapping")
            basis = r.get("distance_basis")
            if basis not in DISTANCE_BASES:
                e(f"distance_basis must be one of {sorted(DISTANCE_BASES)}")
            bm = r.get("buffer_meters")
            if basis == "statutory":
                if not isinstance(bm, (int, float)) or bm <= 0:
                    e("distance_basis statutory needs a numeric buffer_meters "
                      "taken from the statute")
            elif bm is not None:
                e("buffer_meters must be null unless distance_basis is statutory")
        elif mech == "road_class":
            if auth == "statute":
                e("road_class is for guidelines only")
            try:
                re.compile(r.get("road_ref_regex") or "")
            except re.error as ex:
                e(f"road_ref_regex does not compile: {ex}")
            if not r.get("road_ref_regex"):
                e("road_ref_regex is required")
            rb = r.get("road_buffer_m")
            if not isinstance(rb, (int, float)) or rb <= 0:
                e("road_buffer_m must be a positive number")
            gc = r.get("grandfather_cutoff")
            if gc is not None and not isinstance(gc, date):
                e("grandfather_cutoff must be null or a YAML date")
            if "distance_basis" in r:
                e("road_class rules use road_buffer_m, not distance_basis")

    reviewed: set[str] = set()
    for i, s in enumerate(doc.get("states_reviewed") or []):
        code = s.get("state_code")

        def e(msg, code=code, i=i):
            errs.append(f"states_reviewed {code or i}: {msg}")

        if code not in config.STATE_NAMES:
            e(f"unknown state_code {code!r}")
        if code in reviewed:
            e("duplicate entry")
        reviewed.add(code)
        if s.get("statute_status") not in STATUTE_STATUSES:
            e(f"statute_status must be one of {sorted(STATUTE_STATUSES)}")
        if s.get("confidence") not in CONFIDENCES:
            e(f"confidence must be one of {sorted(CONFIDENCES)}")
        summ = s.get("summary")
        if not isinstance(summ, str) or not summ.strip():
            e("summary is required")
        if not isinstance(s.get("last_verified"), date):
            e("last_verified must be a YAML date")
        _check_sources(s.get("sources"), e)
        if code in statutory_states:
            e("has a statutory siting rule, so it must not be listed here")
    for code in sorted(guideline_states - statutory_states - reviewed):
        errs.append(f"states_reviewed: {code} has guideline rules, so it "
                    "needs an entry describing its statutory position")

    # Public text must read as finished sentences and avoid em dashes.
    public = [("states_reviewed", doc.get("states_reviewed")),
              ("rules", doc.get("rules"))]
    for name, block in public:
        for path, s in _strings(block, name):
            if "\u2014" in s:
                errs.append(f"{path}: contains an em dash")
            if (path.endswith((".summary", ".maintainer_notes"))
                    and not s.rstrip().endswith((".", ")", '"'))):
                errs.append(f"{path}: should end with a full sentence")
    return errs


def public_copy(entry: dict) -> dict:
    """An exported copy of a rule/state entry without maintainer-only
    fields."""
    return {k: v for k, v in entry.items() if k not in PRIVATE_FIELDS}


def load_rules_doc(path: Path, taxonomy: dict | None = None) -> dict:
    """Parse, validate, and normalize the rules file. Exits with every
    problem listed if validation fails. Adds statutory_rules,
    guideline_rules, and the derived fields the matcher and UI use."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    errs = validate_rules_doc(doc, taxonomy or overture.load_taxonomy())
    if errs:
        sys.exit("rules file is invalid:\n  - " + "\n  - ".join(errs))

    def primary_url(entry):
        srcs = entry["sources"]
        return next((s["url"] for s in srcs if s["type"] == "primary"),
                    srcs[0]["url"])

    on_prem = doc["default_on_premises_m"]
    for r in doc["rules"]:
        r["source_url"] = primary_url(r)
        basis = r.get("distance_basis")
        r["buffer_specified"] = basis == "statutory"
        r["heuristic_buffer_meters"] = (
            on_prem if basis in ("on_premises", "tbd") else None)
        r["distance_tbd"] = basis == "tbd"
    for s in doc["states_reviewed"]:
        s["source_url"] = primary_url(s)
    doc["statutory_rules"] = [r for r in doc["rules"]
                              if r["authority"] == "statute"]
    doc["guideline_rules"] = [r for r in doc["rules"]
                              if r["authority"] != "statute"]
    return doc

# Interior probe point per state (lon, lat), used to pull each state's
# division_area polygon from Overture with a tiny bbox query. Points sit
