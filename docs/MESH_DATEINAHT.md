# Mesh-Dateinaht (Anweisung)

Gilt für die Zerlegung einer Fahrbahnfläche in mehrere COLLADA-Dateien
(Vertex-Limit). Nicht für die Höhenangleichung zwischen `04_layer0` und
`04_under` (anderer Schritt).

## Ziel

Zwei benachbarte Mesh-Dateien treffen sich an **denselben Punkten** mit
**derselben Höhe**. In der Draufsicht ist die Naht eine Zackenlinie auf den
Zellmitten des 0,5-m-Rasters, plus die echten Punkte am Fahrbahnrand.

Ohne bestandenen Cross-Mesh-Check ist die Teilung **nicht fertig**. Kein
„seams sealed“ aus einer Prüfung nur innerhalb einer Dateigruppe als
Erfolg verkaufen.

## Verfahren

1. **Vorlage legen.** Wo die Datei geteilt werden muss (Luft bis 65 535
   Vertices reicht), eine Linie. Die Linie darf durch Wiese und Wald
   laufen. Dort entsteht kein Mesh.

2. **Mit der Fahrbahn schneiden.** Die Vorlage mit der Fläche dieses
   Meshes schneiden. Nur die Stücke über Asphalt behalten. Jedes Stück
   ist eine eigene Naht (eine Straße, ein Treffer).

3. **Innen bemustern.** Auf jedem Stück Marken alle 25 cm. Marken
   außerhalb der Fahrbahn verwerfen.

4. **Auf Zellmitten einrasten.** Jede Marke auf einer Fahrbahnzelle
   rutscht auf die Mitte dieser Zelle. Dieselbe Zelle nur einmal, in der
   Reihenfolge entlang der Linie. Nicht auf Zellen einer anderen Straße
   oder von over/under einrasten.

5. **Randpunkte.** Am Anfang und Ende jedes Stücks die echten Punkte,
   wo die Vorlage den Fahrbahnrand trifft. Die rutschen nicht aufs
   Raster. Liegt ein Randpunkt auf derselben Stelle wie die erste oder
   letzte Zellmitte: ein Punkt behalten.

6. **Höhe.** Für jeden Punkt der Kette dieselbe Samplingmethode auf
   demselben Raster. Einmal berechnen, in beiden Dateien dasselbe
   Tripel `(x, y, z)` schreiben.

7. **Mesh bauen.** Links der Kette eine Datei, rechts die andere. Beide
   legen genau diese Punkte und die Kanten dazwischen. Innen bleibt das
   0,5-m-Gitter. Clip gegen den Fahrbahnrand und gegen diese Naht —
   nicht gegen Fishnet-Kacheln als Trennfläche. In der Botanik zwischen
   den Straßen liegt nichts.

## Prüfung (Pflicht)

Vor „fertig“ / Inject:

- Jede Nahtkante (aufeinanderfolgende Punkte der Kette) kommt in
  **beiden** angrenzenden DAE vor: XY auf mm gleich, Z auf mm gleich.
- Kein Vertex auf der Dateinaht, der weder Zellmitte noch vereinbarter
  Randpunkt ist.
- `tools/check_mesh_type_gaps.py` (oder Nachfolger): zwischen Dateien
  desselben Höhenrasters keine Stufe `|dZ| > 1 cm` bei gleichem mm-XY;
  Planlücken an der Dateinaht nicht durch versetzte Schnittgeometrie.

Over gegen under mit mehreren Metern ΔZ an derselben XY ist ein
**Stapel** (zwei Raster), keine Dateinaht-Lücke. Die Dateinaht gilt nur
innerhalb eines Höhenrasters.

## Was das Verfahren nicht ist

- Keine Zuweisung „Zelle gehört Datei A oder B“ als einzige Regel (enge
  Kurven: eine Datei darf beide Fahrbahnränder enthalten).
- Kein zweites unabhängiges Clip/Triangulieren der Naht pro Datei.
- Kein Einschnappen der **gesamten** Fahrbahnkante auf 0,5 m — nur die
  Nahtproben innen; Randpunkte der Naht bleiben am Polygon.
- Fishnet bleibt optional für Suche, nicht die Schnittkante.

## Bezug

Ausführlicher Mesh-Ablauf: [ROAD_MESH.md](ROAD_MESH.md).  
Lücken zwischen Mesh-Typen messen: `tools/check_mesh_type_gaps.py`.

Dateinaht-Prüfung (Fixture 7, Regeln 1–4, Regression 6):

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\test_mesh_dateinaht.py
```
