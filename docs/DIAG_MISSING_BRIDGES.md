# Diagnose: fehlende GIP-Brücken (Längsprofil-Dips auf `STR_CODE`)

## Problem

In manchen Regionen (z. B. Imst) ist eine Brücke in den GIP-Verkehrswegen **geometrisch vorhanden** (als Linienzug / `OBJECTID`), aber **nicht als Kunstbau markiert** (`KUNSTBAUTEN` leer bzw. ohne „Brücke“). Dann wird in der Pipeline **keine** Bridge-MeshRoad + Widerlager-Behandlung erzeugt und die spätere Fahrbahn-/Decal-Erstellung folgt der DGM-Kerbe. Ergebnis: **kurze 1–2 m tiefe Gräben** im Längsprofil.

Diese Diagnose sucht solche offenkundigen Hindernisse **vor** der eigentlichen Straßenerstellung und gibt die betroffenen `OBJECTID`s plus automatisch geschätzte **Auflagebereiche** aus.

## Tool

`tools/diag_missing_bridges.py`

- scannt **benannte** GIP-Korridore (nur Segmente mit gesetztem `STR_CODE`)
- erkennt zusammenhängende „Dip“-Abschnitte über der Heightmap (DGM/komponiert, je nachdem was vorhanden ist)
- schätzt Abutments über die bestehende 5‑Linien‑Heuristik aus `road_span_profile.find_abutments`
- ordnet die Senke einer wahrscheinlichsten GIP-`OBJECTID` zu (nächste Mitglieds-Polyline im Korridor)
- schreibt:
  - `data/processed/<site>/diag_missing_bridges.json` (voller Report)
  - `data/processed/<site>/diag_missing_bridges_suggestions.yaml` (copy/paste‑Snippet für Site‑YAML)
  - `data/processed/<site>/diag_missing_bridges_filtered.json` (herausgefilterte Kandidaten mit Grund)
  - `data/processed/<site>/diag_missing_bridges_filtered_suggestions.yaml` (kommentierte Übernahme-Blöcke)

**Wichtig:** Das Tool schreibt **nichts** in den zentralen GIP‑Speicher (`data/roads/*`). Es erzeugt nur Diagnosedateien im `processed/`‑Ordner.

## Voraussetzungen (warum es nicht „frisch geklont“ läuft)

Das Repo versioniert üblicherweise **keine** Site-Artefakte. Das Tool benötigt:

- **GIP-Cache**: `data/raw/gip_<site>_*.geojson` (aus `tools/fetch_gip.py`)
- **Heightmap + Meta**: `data/processed/<site>/heightmap_meta.json` und mindestens `heightmap_<size>.png` (aus `tools/build_smoke.py` bzw. Core-Build)

Ohne diese Dateien bricht das Tool bewusst ab.

## Aufruf (PowerShell, 1 Zeile)

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/<site>.yaml"; python tools\diag_missing_bridges.py
```

Einschränkung auf eine Straße:

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/<site>.yaml"; python tools\diag_missing_bridges.py --only-str-code B179
```

## Wichtige Parameter (Heuristik)

- `--dip` (Default `1.0`): minimale Dip-Tiefe in Metern (im Längsprofil) um als Kandidat zu zählen  
- `--min-len` / `--max-len` (Default `6..160`): sinnvolle Spannweitenbegrenzung, damit keine langen Täler oder Mikrorauigkeit gemeldet werden  
- `--step` (Default `1.0`): Abtastabstand entlang des Korridors  
- `--solid-run` (Default `4.0`): Fensterlänge, aus der eine „solide“ Baseline (rolling max) gebildet wird

Wenn du sehr viele false positives bekommst, zuerst `--dip` erhöhen und/oder `--min-len` erhöhen.

## Ergebnis übernehmen (YAML)

Das Tool erzeugt ein YAML‑Snippet, das du in die Site‑YAML kopieren kannst:

- **Pflicht (Brücke überhaupt als Kunstbau führen)**: `beamng.bridges.gip_extra`  
  Damit wird die `OBJECTID` beim GIP-Fetch als Brücke gelabelt (Name `Brücke <oid>`), auch wenn der Dienst keinen Kunstbau setzt.

- **Optional (Abutments festnageln)**: `beamng.bridges.items[].abutment_s`  
  Nur wenn die automatische Spannweitenschätzung in einer DGM‑Kerbe „zu weit rein“ rutscht oder du reproduzierbar auf feste Widerlager-Stationen gehen willst. `abutment_s` sind Meter entlang des gewählten Korridors.

## Hinweise / Grenzen

- Die `OBJECTID`-Zuordnung ist eine räumliche Heuristik („nächste Linie“). Wenn im Report `oid_candidates` merkwürdig aussehen, nimm die passendere OID manuell.
- Das Tool prüft nur `STR_CODE`-Korridore (benannte Straßen). Unbenannte Wege sind absichtlich ausgenommen.
- Konservativ: Kandidaten, die bereits als **Tunnel/Galerie/sonstiger Kunstbau** erkannt werden, landen standardmäßig in den „filtered“-Dateien und werden nicht automatisch als Brücke vorgeschlagen.
- Für exakte Fahrbarkeit ist weiterhin entscheidend, dass Brücken/Galerien das **Widerlager-System** nutzen (Auflagen/Pad/`approach_conform`/`force_deck_z`). Die Diagnose ist nur der „Finder“.

