# Vendored third-party reference data

## overture_taxonomy.csv

The Overture places taxonomy: one row per category code, with its path from the root category (`taxonomy` column), display name, level, and basic category.

- Source: OvertureMaps/docs, `static/taxonomy/2026-09-23.0/taxonomy.csv`
- Pinned at commit `a9004e530a60fcc18af80ac279790ea290852763`
- License: the OvertureMaps/docs repository is MIT; Overture publishes its schema under CC-BY-4.0. The attribution lives in the repo README and the data license notice.

`build/foulflock/overture.py` parses the CSV into a code to path map, and uses it to expand the anchors in `category_overture_mapping` (`rules/alpr_state_rules.yaml`) into the protected categories. A place matches through its `taxonomy.primary` code. The build also checks every anchor against this file and warns when streamed places carry codes it doesn't list.

### Refreshing

Each Overture release publishes its taxonomy under `static/taxonomy/<version>/` in OvertureMaps/docs, alongside `taxonomy_to_legacy_categories.csv`, which maps the pre-2026-09 `categories` codes to the new ones.

1. Download the newest `taxonomy.csv` from that directory over this file and update the commit pin above.
2. Run `python -m pytest tests\python`. An anchor missing from the new file fails the rules validation.
3. Review the anchors under `education`, `health_care`, `cultural_and_historic`, and `community_and_government`. Anchors select a subtree, so a new child is picked up automatically and a moved node needs a new anchor.
4. Rebuild with `--refresh places`.
