# beamng_autoroad

Semiautomatisierte Pipeline: Tiroler OGD → spielbare Passstraßen in **BeamNG.drive**.

## Stand

Smoke-Test für **Hahntennjochstraße** (M28 / EPSG:31254, ~500×510 m) ist vorbereitet:

- DGM 0,5 m via WCS geladen
- `heightmap_512.png` + `roads_beamng.json` unter `data/processed/`
- Terrain-Layer-Masken: Grass / dirt / rock / **Asphalt** (volle OSM-Breite + Bankett)
- Import: [docs/BEAMNG_IMPORT.md](docs/BEAMNG_IMPORT.md)

| Phase 1 | Später |
|---------|--------|
| Straße + Heightmap | Tunnel/Galerien |
| Terrain-Materials (Gras/Dirt/Fels/Asphalt) | Feinere Surfaces |
| Leitplanken (Italy-Mesh, Pitch, Kurven-Sehnen) | AssemblySpline / R10–R15 |
| Kamm-Silhouette Fels/Wald (Masken) | Gebäude |

## Neu bauen

```powershell
cd C:\temp\beamng_autoroad
python -m pip install -r requirements.txt
python tools\fetch_osm.py
python tools\build_smoke.py
# oder nur Masken / Leitplanken:
# python tools\build_terrain_masks.py
# python tools\build_guardrails.py
```

## Dokumente

- [docs/KONZEPT.md](docs/KONZEPT.md)
- [docs/DATENUEBERGABE.md](docs/DATENUEBERGABE.md)
- [docs/BEAMNG_IMPORT.md](docs/BEAMNG_IMPORT.md)
- [config/site.example.yaml](config/site.example.yaml) — Vorlage (`site.yaml` lokal, gitignore)
