# GIS-Annotationen (Schema)

Hand-editierte Lagen für Straßenrand, Leitplanken und Lücken (Einmündungen).  
OSM/DGM bleiben **Rohlage**; diese Dateien überschreiben Heuristik, sobald die Pipeline sie einliest.

## Datei und CRS

| | |
|--|--|
| Pfad | `data/annotations/<site>.gpkg` (bevorzugt) oder einzelne GeoJSON unter `data/annotations/` |
| CRS | wie `config/site.yaml` → `crs` (Smoke-Test: **EPSG:31254**) |
| Editor | QGIS; Orthofoto + DGM als Hintergrund |
| Anlegen | `python tools/init_annotations_gpkg.py` → leere Layer `road_edge`, `guardrail`, `centerline` |

Eine GeoPackage-Datei pro Site, Layer-Namen exakt wie unten.

`config/site.yaml`:

```yaml
annotations:
  gpkg: data/annotations/tirol-m28-test-500m.gpkg
  guardrail_source: auto   # auto | gpkg | heuristic
  # seed_sample_step_m: 2.0
```

## Zweistufiger Workflow

```text
1) python tools\seed_annotations.py [--force]
      → Heuristik schreibt Entwurf in GPKG (centerline, road_edge, guardrail)

2) QGIS: Linien verschieben / teilen / Lücken an Einmündungen
      → present=false oder fehlende Linie = keine Planke

3) python tools\build_guardrails.py
      → liest Layer guardrail (auto), baut 3D; schreibt GPKG nie
```

| `guardrail_source` | Verhalten |
|--------------------|-----------|
| `auto` (Default) | GPKG wenn `guardrail` Features hat, sonst Heuristik |
| `gpkg` | nur GPKG (Fehler wenn leer) |
| `heuristic` | nur OSM-Offset (GPKG ignorieren) |

### Seed (Heuristik → GPKG)

```powershell
python tools\seed_annotations.py          # nur wenn Layer leer
python tools\seed_annotations.py --force  # ersetzt vorhandene Features bewusst
```

Schreibt `centerline`, `road_edge` (`width/2`), `guardrail` (`width/2 + lateral_extra`) in Site-CRS.  
**`build_smoke.py` / `build_guardrails.py` rufen das nie auf** und überschreiben die GPKG nicht.

---

## Layer

### 1. `road_edge` — genauer Fahrbahnrand

**Geometrie:** `LineString` (2D; Z optional, sonst aus DGM)

Digitalisiere den **sichtbaren Asphalt-/Fahrbahnrand** (nicht die Leitplanke).  
Links und rechts getrennt; Orientierung beliebig, `side` ist maßgeblich.

| Attribut | Typ | Pflicht | Werte / Bedeutung |
|----------|-----|---------|-------------------|
| `id` | text/int | ja | stabiler Schlüssel |
| `side` | text | ja | `left` \| `right` (Fahrtrichtung der zugehörigen Centerline) |
| `road_ref` | text | nein | OSM-way-id oder lokaler Straßenname |
| `source` | text | nein | `ortho` \| `survey` \| `derived` |
| `notes` | text | nein | frei |

**Nutzung später:** Asphalt-Maske / Bankett / laterale Nullinie statt `width/2`.

---

### 2. `guardrail` — Leitplankenverlauf und -vorhandensein

**Geometrie:** `LineString` entlang der gewünschten **Planken-Achse** (Pfostenlinie).  
Nur Abschnitte zeichnen, wo eine Planke stehen soll. **Lücken = Öffnungen** (Einmündung, Zufahrt, Busbucht) — kein Extra-Polygon nötig.

| Attribut | Typ | Pflicht | Werte / Bedeutung |
|----------|-----|---------|-------------------|
| `id` | text/int | ja | stabiler Schlüssel |
| `side` | text | ja | `left` \| `right` |
| `present` | boolean | ja | `true` = bauen; `false` = bewusst keine Planke (selten nötig, wenn Linie fehlt) |
| `kind` | text | nein | `wbeam` (Standard) \| `concrete` \| `gabion` \| `none` |
| `gap_reason` | text | nein | bei Lücke / `present=false`: `junction` \| `driveway` \| `bus_stop` \| `bridge_joint` \| `other` |
| `road_ref` | text | nein | Zuordnung zur Straße |
| `section_m` | real | nein | Wunsch-Sektionslänge (Default aus `site.yaml`) |
| `notes` | text | nein | frei |

**Regeln:**

1. Linie = wo Planken stehen. Keine Linie / Lücke in der Linie = Öffnung.
2. `present=false` nur für explizite „hier keine Heuristik“-Sperren auf einem kurzen Stub.
3. `side` konsistent zu `road_edge` derselben Straßenseite.
4. Nicht die Centerline offsetten und committen, wenn der Rand schon in `road_edge` liegt — Pipeline kann Offset aus Rand + `lateral` ableiten; **bevorzugt** die editierte `guardrail`-Linie als Wahrheit.

**Nutzung später:** `build_guardrails.py` liest diese Linien statt OSM-Offset; Heuristik nur wo Layer fehlt.

---

### 3. `centerline` — optional, Korrektur der Achse

**Geometrie:** `LineString`

| Attribut | Typ | Pflicht | Werte |
|----------|-----|---------|--------|
| `id` | text/int | ja | |
| `width_m` | real | nein | überschreibt OSM-Breite |
| `highway` | text | nein | `secondary` … |
| `notes` | text | nein | |

Nur nötig, wenn OSM-Achse grob falsch ist. Sonst weglassen.

---

### 4. `gallery` / `bridge` / `tunnel` — aus GIP ableitbar

Rohdaten: Tirol Verkehrswege WFS — siehe [GIP.md](GIP.md).  
Felder `KUNSTBAUTEN` + `OBJEKT`/`OBJEKTBEZEICHNUNG` kennzeichnen Galerien und Brücken.

| Attribut | Typ | Pflicht | Werte |
|----------|-----|---------|--------|
| `id` | text | ja | stabil (z. B. GIP OBJECTID) |
| `kind` | text | ja | `gallery` \| `bridge` \| `tunnel` \| `culvert` |
| `name` | text | nein | aus `KUNSTBAUTEN` |
| `open_side` | text | nein | `left` \| `right` \| `both` \| `none` (Galerie) |
| `notes` | text | nein | |

Geometrie zunächst die GIP-Liniensegmente; Portale/Querschnitt später verfeinern.

### 5. Reserviert

| Layer | Geometrie | Zweck |
|-------|-----------|--------|
| `exclude` | Polygon | Seilbahn-/DOM-Artefakte aus Masken |
| `wall` | LineString | Stützmauer/Gabione (eigenes Mesh) |

---

## QGIS-Arbeitsablauf (kurz)

1. `python tools\seed_annotations.py` — Heuristik als Entwurf (nur wenn leer; sonst `--force`).
2. Ortho + DGM laden, Projekt-CRS = Site-CRS; GPKG-Layer öffnen.
3. `guardrail` verschieben, teilen, Lücken an Einmündungen (`present=false` optional).
4. Einmündung: Linie unterbrechen (**zwei Features**).
5. `python tools\build_guardrails.py` — 3D aus GPKG (`source=gpkg` in Meta).

**Schutz:** Seed ohne `--force` bricht ab, wenn schon Features existieren. Builds schreiben die GPKG nicht.

`road_edge` / `centerline` sind für spätere Masken/Achskorrektur; Guardrail-Build nutzt vorerst nur `guardrail`.

---

## Pipeline-Rollen

| Schritt | Verhalten |
|---------|-----------|
| `seed_annotations.py` | Heuristik → GPKG (nur explizit, `--force` bei Übernahme) |
| QGIS | Nutzer editiert |
| `build_guardrails.py` | liest `guardrail` (auto); **schreibt keine GPKG**; Fallback Heuristik wenn leer |
