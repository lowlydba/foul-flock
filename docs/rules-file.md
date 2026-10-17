# Maintaining the rules file

`rules/alpr_state_rules.yaml` is the legal core of the map. Everything the map claims about a state traces back to an entry there.

- Human-authored only. Do not scrape legal text.
- The schema is documented in [AGENTS.md](../AGENTS.md) and enforced on every build and in `tests/python/test_rules.py`.
- Every entry needs `sources`, `confidence` (`verified`, `inferred`, or `needs_review`), and `last_verified`.
- Re-review quarterly, since siting bills come up every legislative session. The procedure is in AGENTS.md under "quarterly state-law re-review".
- If a statute gives no numeric distance, set `buffer_meters: null` and a non-statutory `distance_basis`. Never invent a number and present it as law.

Full survey outputs are archived in [docs/research](research/) as `state-law-survey-YYYY-MM-DD.txt`.

After any edit, run:

```powershell
python -m pytest tests\python
```
