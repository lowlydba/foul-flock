"""Paths, output archive names and zoom ranges, Overpass mirrors, and the
refresh scopes set by --refresh. Tests redirect CACHE_DIR and OUT_DIR here."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RULES_FILE = ROOT / "rules" / "alpr_state_rules.yaml"
CACHE_DIR = ROOT / "cache"
OUT_DIR = ROOT / "web" / "data"
# Cameras with no candidate place within the scan radius can never be
# flagged at any slider setting, so they ship as static vector tiles
# (PMTiles) behind the map's off-by-default "Nothing found" layer instead of
# in the core cameras.geojson, which the slider logic keeps in memory.
CLEAR_CAMERAS_ARCHIVE = "clear_cameras.pmtiles"
CLEAR_CONES_ARCHIVE = "clear_cones.pmtiles"
PLACES_ARCHIVE = "places.pmtiles"
STATES_ARCHIVE = "states.pmtiles"
# Zoom ranges per archive. Clear cameras show from zoom 6 (state level) and
# are exact to ~2 m by zoom 12, which MapLibre overzooms beyond. Cones draw
# at zoom 13+ and one tile level overzooms cleanly. Places draw from zoom
# 10. State outlines are ~1 km simplified, so zoom 7 tiles overzoom fine.
CLEAR_CAMERAS_ZOOMS = (6, 12)
CONE_TILE_ZOOM = 13
PLACES_ZOOMS = (10, 12)
STATES_ZOOMS = (0, 7)
TAXONOMY_CSV = Path(__file__).resolve().parents[1] / "vendor" / "overture_taxonomy.csv"

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

# Set by --refresh: cache scopes ("cameras", "places", "divisions") whose
# entries are ignored and refetched.
REFRESH_SCOPES: set[str] = set()

STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida",
    "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky",
    "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}

EARTH_R = 6371008.8  # meters
