# Vendored third-party reference data

## overture_categories.csv

- Source: OvertureMaps/schema repo,
  `docs/schema/concepts/by-theme/places/overture_categories.csv`
- Pinned at commit `ac891b7f22486a6c96c1f6232461e7193263b184` (2025-05-07)
- License: CC-BY-4.0 (the OvertureMaps/schema repository license)
- Refresh: re-download from
  https://raw.githubusercontent.com/OvertureMaps/schema/main/docs/schema/concepts/by-theme/places/overture_categories.csv
  and update the pin above. Then re-check CATEGORY_OVERTURE_MAPPING in
  rules/alpr_state_rules.yaml: the mapping selects taxonomy codes by prefix
  walk (a code matches if it or any of its taxonomy ancestors is listed),
  so renamed/added codes under education, health_and_medical,
  religious_organization, public_service_and_government need review.

The build (`build/build.py`) parses this CSV into a code -> ancestry map and
uses it to bucket Overture place `categories.primary` codes into the five
protected categories. See `category_overture_mapping` in
rules/alpr_state_rules.yaml for the curated selection and per-category
notes on what is deliberately included/excluded.
