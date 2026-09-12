# beamng_autoroad

Semiautomatisierte Pipeline: Tiroler OGD → spielbare Passstraßen in **BeamNG.drive**.

## Stand

Zwei Testgebiete (siehe [config/sites/](config/sites/README.md)):

| Site | Zweck |
|------|--------|
| **Hahntennjoch** (`site.yaml` Default) | Leitplanken + Terrain-Optimierung, 512² |
| **L13 Kühtai** (`AUTOROAD_SITE=…/l13_kuehtai.yaml`) | Galerien/Brücken (GIP), 1024² |

- Heightmap + Masken + Guardrails (GPKG-Workflow)
- GIP-WFS Cache für Kunstbauten — [docs/GIP.md](docs/GIP.md)
- Roadmap: [docs/STATUS.md](docs/STATUS.md)

## Neu bauen

```powershell
cd C:\temp\beamng_autoroad
python -m pip install -r requirements.txt
python tools\fetch_osm.py
python tools\build_smoke.py
# oder nur Masken / Leitplanken:
# python tools\build_terrain_masks.py
# python tools\build_guardrails.py
# Annotations-GPKG (leer) anlegen / Heuristik seeden:
# python tools\init_annotations_gpkg.py
# python tools\seed_annotations.py
# python tools\seed_annotations.py --force   # nur bewusst Überschreiben
# python tools\fetch_gip.py                  # Galerien/Brücken (cached)
```

## Dokumente

- [docs/KONZEPT.md](docs/KONZEPT.md)
- [docs/STATUS.md](docs/STATUS.md) — Stand, Roadmap, Schnellbefehle
- [docs/DATENUEBERGABE.md](docs/DATENUEBERGABE.md)
- [docs/ANNOTATIONS.md](docs/ANNOTATIONS.md) — GIS: Straßenrand / Leitplanken / Öffnungen
- [docs/GIP.md](docs/GIP.md) — Tirol Verkehrswege WFS / Kunstbauten (Cache)
- [docs/BEAMNG_IMPORT.md](docs/BEAMNG_IMPORT.md)
- [config/site.example.yaml](config/site.example.yaml) — Vorlage (`site.yaml` lokal, gitignore)
