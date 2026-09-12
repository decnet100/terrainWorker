# Konzept: Geodaten → BeamNG Passstraßen (Tirol)

## Ziel

Semiautomatisierte, robuste Pipeline von öffentlich zugänglichen Tiroler Geodaten zu einem **spielbaren BeamNG.drive-Level** für alpine Passstraßen.

**Phase 1 (Erstversuch):** Straße + Terrain + Oberflächen + Bergkamm-Silhouette (Fels vs. Wald).  
**Nicht in Phase 1:** Gebäude/Dörfer, kompletter langer Pass.

Andere Sims (Ur-AC, AC Rally UE5, AMS2, rF2) sind bewusst außerhalb des Scopes.

---

## Warum naive DGM→Mesh scheitert

0,5 m-DGM erzeugt oft **Schrägen** statt harter Straßenränder (Leitplanken, Gabionen, Stützmauern). DOM enthält Seilbahn-/Vegetations-Artefakte. Tunnel und Galerien fehlen im Heightfield.

Deshalb:

- Fahrbahn als **eigene Road** (Spline + Breite/Profil)
- Terrain daneben mit begrenztem Terraforming
- Vertikale Features und Bauwerke als Annotation + Props/Meshes (später)
- Tunnel: XY-Pfad vom Menschen + **Cubic-Hermite-Z** aus Portalhöhen und Approach-Steigungen

---

## Datenquellen

| Datensatz | Rolle | Bezug |
|-----------|--------|--------|
| DGM 0,5 m | Heightmap, Straßenhöhen | WCS / Blattschnitt GeoTIFF |
| DOM 0,5 m | Artefakt-Erkennung (optional) | WCS |
| Orthofoto ~20 cm | Textur-/Maskenhilfe | WCS / Download |
| GIP / OSM | Centerline, Straßentyp → Breite/Surface | OGD / Overpass |
| Landnutzung Tirol | Wald vs. Fels, grobe Materials | WFS / Download |

Google Earth nur als **visuelle Referenz** beim Annotieren, nicht als Texturquelle (Lizenz).

CRS typisch: MGI GK West/Central (EPSG:31254 / 31255), Höhen GHA.

---

## Pipeline (Überblick)

```text
OGD (WCS/WFS/OSM)
    → Ingest + einheitliches Meter-CRS
    → Centerline (korrigierbar in QGIS)
    → Road-JSON (x,y,z,width) + Heightmap 16-bit PNG
    → Surface-Segmente → Terrain-Paint + Groundmodels
    → Wald/Fels-Maske → Kamm-Impostors (Wipfel-Alpha)
    → BeamNG Level (World Editor Import)
```

### Straße und Ränder

- Spline-Nodes mit Höhe aus DGM (Fahrbahn leicht glätten)
- Terraforming mit kleinem Margin — Böschungen nicht „hochschmieren“
- Leitplanken/Mauern **nicht** als Terrain-Schräge (Props später)

### Oberflächen (statt AC-Surface-INI)

1. **Physics:** Groundmodels unter bemaltem Terrain (`ASPHALT`, Kies-Varianten, `roughnessCoefficient`)
2. **Look:** wenige Decal-/Terrain-Materials
3. **Makro:** DGM-Wellen; keine flächendeckende High-Poly-Körnung

DecalRoads sind oft Optik/Navigation; Grip kommt typisch vom Terrain-Material darunter.

### Kamm-Silhouette (visuell wichtig in Phase 1)

- Maske: Landnutzung Wald vs. Fels (+ Hangneigung)
- Felskamm: hartes Terrain-Material, **keine** Wipfel-Karten
- Bewaldeter Hang: **Alpha-Impostors / Transparenz-Muster** (Forest, ohne Collision) für unruhigen Horizont — keine teuren Einzelbäume nötig
- Nah an der Straße optional später echte Forest-Meshes

### Tunnel / Galerien (Phase 1.5+)

Eingabe: horizontale Centerline zwischen Portalen.  
Höhe: Cubic Hermite entlang Bogenlänge aus Portal-Z + Approach-Steigung \(dz/ds\).  
Mesh-Loft + Terrain-Holes an Portalen. Galerien: DGM mischen wo offen.

### Gebäude (Phase 2+)

Footprint → Kategorie (Wohnen, Kirche, Gewerbe, …) → Varianten-Pool → TSStatic nah / Proxy fern. **Nicht Erstversuch.**

---

## Minimalbeispiele (Robustheit)

1. Kurze Freilandstraße (~300–800 m)
2. Abschnitt mit hartem Rand (Mauer/Leitplanke) — später
3. Kamm sichtbar: Fels vs. unruhiger Waldhorizont

Akzeptanz: in BeamNG startbar, befahrbar, sinnvolle Silhouette, keine Seilbahn-Spikes.

---

## Tech-Stack (geplant)

- Python + GDAL/rasterio + geopandas/shapely + numpy
- QGIS für Centerline/Annotation
- BeamNG World Editor (Heightmap, Roads, Painter, Forest)
- Blender nur für Billboard-Texturen / später Meshes

Arbeitsverzeichnis: `C:\temp\beamng_autoroad`

---

## Nächste Schritte

Siehe [STATUS.md](STATUS.md) (Stand + priorisierte Roadmap).

Kurz:
1. QGIS: Guardrail-Öffnungen/Ränder feinjustieren (Build liest GPKG bereits)
2. `road_edge` in Terrain-Masken einbinden
3. Soft-Cover Richtung Moos/Alm; Geologie/LISA später
