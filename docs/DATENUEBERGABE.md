# Datenübergabe

## Kurzantwort

**Ja — WCS-Links reichen**, wenn du zusätzlich angibst:

1. **BBOX** des Testausschnitts (Koordinaten + EPSG)
2. **Layer-/Coverage-Namen** (falls nicht aus GetCapabilities eindeutig)
3. Optional: gewünschte **Auflösung** (z.B. DGM 0,5 m, Ortho 0,2 m oder grober für Tests)

Du musst **keine** multi-GB-GeoTIFFs in den Chat legen. Ich (bzw. das Fetch-Skript) lade in `data/raw/` herunter.

Alternativ: bereits heruntergeladene Kacheln nach `data/raw/` legen und in `config/site.yaml` nur lokale Pfade eintragen.

---

## Empfohlenes Format: `config/site.yaml`

Vorlage: [`../config/site.example.yaml`](../config/site.example.yaml)

```yaml
name: mein-testpass
crs: EPSG:31254          # oder 31255 — an Daten anpassen
bbox:                    # xmin, ymin, xmax, ymax im CRS oben
  - <<<<<<<
  - <<<<<<<
  - <<<<<<<
  - <<<<<<

sources:
  dgm:
    type: wcs
    url: "https://.../WCS"
    coverage: "..."      # Coverage/Layer-ID
    resolution_m: 0.5
  ortho:
    type: wcs
    url: "https://..."
    coverage: "..."
    resolution_m: 0.2    # Erstversuch: 1.0 oder 2.0 reicht oft
  landuse:
    type: wfs             # oder local
    url: "https://..."
    type_name: "..."
  roads:
    type: osm             # oder gip / local gpkg
    # bbox aus site.bbox
```

Chat-Nachricht kann auch minimal sein:

> Testausschnitt: EPSG:31254, BBOX=[…].  
> DGM-WCS: `https://…` Coverage `…`  
> Ortho-WCS: `https://…` Coverage `…`

---

## Was wo liegt

```text
C:\temp\beamng_autoroad\
  config\site.yaml          # dein Manifest (nicht committen falls privat)
  config\site.example.yaml
  data\raw\                 # Original-Downloads (groß, gitignore)
  data\processed\           # Heightmap, road JSON, Masken
  data\annotations\         # von dir: GPX/GeoJSON Tunnel, Ränder, …
  docs\
```

---

## Annotationen (wenn nötig)

Später als Dateien unter `data/annotations/` (GeoJSON oder GeoPackage):

| Inhalt | Felder (Beispiel) |
|--------|-------------------|
| Centerline-Korrektur | LineString, optional `width`, `surface` |
| Tunnel/Galerie | Portale + XY-Innenpfad, `profile=hermite` |
| exclude | Polygone (Seilbahn-Artefakte) |
| wall/guardrail | Linien entlang Rand |

Google-Earth-Screenshots helfen zur Orientierung; Geometrie bitte als Vektor, nicht nur Bild.

---

## WCS-Praxis (Tirol)

Offizielle Einstiege:

- [tiris Geodatendienste](https://www.tirol.gv.at/statistik-budget/tiris/tiris-geodatendienste/)
- [DGM Tirol data.gv.at](https://www.data.gv.at/datasets/0454f5f3-1d8c-464e-847d-541901eb021a)
- [Laserscandaten / Gelände WCS](https://www.tirol.gv.at/sicherheit/geoinformation/geodaten-tiris/laserscandaten/)
- [Orthofotos WCS](https://www.tirol.gv.at/sicherheit/geoinformation/geodaten-tiris/orthofotos/)

Bitte die **konkreten GetCapabilities-/WCS-URLs** aus dem Katalog oder QGIS „Layer-Eigenschaften → Quelle“ hier eintragen — die Endpunkte ändern sich gelegentlich.

Prüfen:

```text
…/WCS?SERVICE=WCS&REQUEST=GetCapabilities
```

In QGIS: WCS hinzufügen → Ausschnitt exportieren funktioniert auch; dann Pfade unter `sources.*.type: local` setzen.

---

## Größe / Erstversuch

| Datensatz | Empfehlung für ersten Test |
|-----------|----------------------------|
| Ausschnitt | ~0,5–2 km Kantenlänge um die Straße |
| DGM | 0,5 m wenn machbar, sonst 1 m |
| Ortho | erst 1–2 m; 0,2 m nur wenn nötig |
| DOM | optional, nur Artefakt-Check |

Lieber ein kleiner, guter Ausschnitt mit sichtbarem **Fels-/Wald-Kamm** als ein ganzer Pass.
