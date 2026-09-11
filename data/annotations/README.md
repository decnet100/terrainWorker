# Annotations

Hand-editierte GIS-Lagen (Straßenrand, Leitplanken, …).

- Schema: [`docs/ANNOTATIONS.md`](../../docs/ANNOTATIONS.md)
- Smoke-Test-GPKG: `tirol-m28-test-500m.gpkg` (EPSG:31254)
- Leer anlegen: `python tools/init_annotations_gpkg.py`
- Heuristik seeden: `python tools/seed_annotations.py` (mit `--force` nur zum bewussten Überschreiben)
- Normale Builds (`build_smoke` / `build_guardrails`) schreiben diese Datei **nicht**
