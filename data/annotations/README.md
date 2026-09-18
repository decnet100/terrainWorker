# Annotations

Hand-edited GIS layers (road edge, guardrails, …).

- Schema: [`docs/ANNOTATIONS.md`](../../docs/ANNOTATIONS.md)
- Smoke-test GPKG: `tirol-m28-test-500m.gpkg` (EPSG:31254)
- Create empty: `python tools/init_annotations_gpkg.py`
- Seed heuristic: `python tools/seed_annotations.py` (`--force` only to overwrite on purpose)
- Normal builds (`build_smoke` / `build_guardrails`) do **not** write this file
