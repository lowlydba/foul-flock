"""ALPR Siting Compliance Map build pipeline, split by concern:

- config: paths, archive names, zoom ranges, refresh scopes
- rulesfile: rules YAML schema validation and loading
- overpass: Overpass mirrors, cache, and the camera query
- guidelines: road-class "spirit-of" guideline lookups
- overture: places taxonomy, state divisions, protected places
- matching: camera-to-place matching and exclusion screening
- viewcones: illustrative camera view cones
- artifacts: writes web/data (GeoJSON, PMTiles, rules.json)
- cli: argument parsing and the five build steps

Modules call each other through the module (`config.OUT_DIR`,
`overpass.overpass_query(...)`), so a test that patches a name on its
defining module changes what the whole pipeline sees.
"""
