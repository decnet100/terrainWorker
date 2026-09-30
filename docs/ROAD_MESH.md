# Straßenmesh aus dem 0,5-m-DGM

Stand Imst (`config/sites/imst.yaml`). Die Zahlen unten stammen aus dem
Level `autoroad_imst_8192` (Slug `tirol-imst-8192`, Box 8000 m auf 8191
Samples gestreckt). Seit dem 29.09. heißt die Site `tirol-imst-tarrenz-8192`,
Level `imst_tarrenz`, Box 8191 m, 1 Sample = 1 m; Rahmen und Zählwerte
ändern sich mit dem Neuaufbau. In der YAML gibt es keine `decal_roads` und
keine `bridges` mehr: Fahrbahn und Brückendecks sind das Mesh.
Dieses Dokument ist die Quelle für den Ablauf, der die Fahrbahn erzeugt.
Der Brückenversuch am Ende ist kein zweiter Ablauf. Er ist gescheitert und
darf nicht fortgesetzt werden, als wäre er derselbe.

Die älteren Grade-Versuche (gerade Querneigung, Settle, Side-Fill) bleiben
verworfen. Sie stehen in [DGM_EDGE_REPAIR.md](DGM_EDGE_REPAIR.md).

## Was im Level liegt

Das Rad fährt ein COLLADA-Mesh, nicht die Heightmap.

Der letzte erfolgreiche Inject ist nicht das reine Straßenmesh. Er ist der
Brückenversuch: Höhen aus `data/processed/tirol-imst-8192/dom_bridge_deck/deck.tif`,
Clip aus `dom_bridge_deck/clip.gpkg`, Layer `carriageway`. Build danach:
Nähte geschlossen, 605 227 Zellen, 18 Teile, 743 787 Vertices, 1 348 013
Dreiecke. Der User hat dieses Ergebnis verworfen.

`dgm_repair_transect/02_repaired.tif` ist dabei nicht überschrieben worden.
Die Heightmap ist nicht beschrieben worden. Ein reines Straßenmesh entsteht
erst wieder, wenn `build_road_grid.py` mit dem reparierten Raster und dem
ursprünglichen Fahrbahn-Clip läuft (Befehl unten). Danach BeamNG ganz beenden
und neu starten. Den World Editor nicht speichern, wenn die Objekte aus dem
Inject kommen.

## Quelle

| | |
|---|---|
| Höhen | `corridor50_raw/corridor50_raw.tif`, DGM 0,5 m, nicht das DOM |
| Fläche und Achse | `centerline_shift_taper/centerline_shift.gpkg`, Layer `carriageway` und `centerline`. Landes- und Bundesstraßen, Autobahnen, und Gemeindestraße `S-G` (5 m, wenn keine gemessene Breite vorliegt). `S-GW` ist kein Mesh |
| Rahmen | xmin 26316, ymin 232503, xmax 34316, ymax 239159, 16000×13312, 0,5 m. Zeile 0 ist Norden. Die World-Datei nennt die Zellmitte, der GeoTIFF-Anker die äußere Nordwest-Ecke |
| Rauheit | Betrag der Abweichung vom Mittel in einem Fenster von 1,5 m, nur auf der Fahrbahn (`_road_only`). Schwelle `DROP_ROUGH_M` = 1,5 cm |
| Kunstbauten | Layer `segments`, `structure = 1`. Diese Spannen sind weder Stützstelle noch Ziel |

Die Spiel-Heightmap ist ein anderes Raster: 8192 Knoten, `squareSize` 1,0 m, fest. Knoten `i` wird bei `i` m gezeichnet, die Bounding-Box liegt also auf dem Span 8191 m (`crs_to_terrain`). Seit der Umstellung rechnen `crs_to_beamng` und `beamng_to_crs` mit demselben Span; GIP-Knoten, Decals, Guardrails, Masken und die Basis-Heightmap aus `build_smoke.py` liegen auf diesem Gitter. Pixel aus BeamNG-Metern: `px = bx / squareSize`, nicht `bx / extent · (n − 1)`. Level, die vor der Umstellung erzeugt wurden, haben Objekte bis 1 m zu weit vom Ursprung; sie müssen neu gebaut werden. `squareSize` nicht anfassen.

## Schritt 1: Querprofil längs der Achse

`tools/repair_dgm_transect.py` schreibt `dgm_repair_transect/`.

Die Achse ist die verschobene Centerline, geschnitten mit dem Fahrbahnpolygon.
Stationen alle 0,5 m. Je Hälfte ein Strahl auf der Normalen, Versatz alle
0,25 m, bis das Polygon endet, höchstens 12 m. Links und rechts bleiben getrennt.

Eine Probe ist eine Stützstelle, wenn sie im Polygon liegt, endlich ist und
in der fahrbahn-eigenen 1,5-m-Rauheit unter 1,5 cm liegt. Ein raues Pixel ist
keine Stützstelle. Das gilt besonders für den Rand, der schon die Böschung
oder die Felswand ist. Die Rauheit ist nur die Prüfung. Gespeichert wird die
eigene Höhe der Probe, nicht das Fenstermittel.

Die Form ist die Höhendifferenz zur Mittellinie. Die Mittellinie wird zuerst
aus ihren eigenen sauberen Proben entlang des Bogens gemischt. Jeder andere
Versatz speichert nur an seinen eigenen Stützstellen die Differenz zur
Mittellinie und mischt diese Differenz entlang des Bogens. Die Lücke darf
höchstens 50 m sein (`MAX_GAP_M`). Über die erste und die letzte Stützstelle
hinaus wird nichts ergänzt. An der kaputten Station wird keine neue
Querneigung gelegt. Gibt es für diesen Versatz dieser Hälfte keine Stützstelle
in Reichweite, bleibt das Pixel roh.

Geschrieben wird nur das raue Pixel, und nur mit dem gemischten Wert seines
eigenen Versatzes. Saubere Fahrbahn bleibt das Roh-DGM. Pixel außerhalb des
Polygons werden nicht geschrieben. Stationen auf Brücke, Tunnel oder Galerie
sind gesperrt: keine Stützstelle, kein Ziel. Das Bohrloch wird nicht an die
Straße gezogen.

Ausgabe `02_repaired.tif`: im Fahrbahnpolygon das so gefüllte DGM, außerhalb
NaN. `01_class.tif` unterscheidet sauber (1), rau und ohne Profil gelassen (2),
aus dem Halbprofil gefüllt (3).

Auf diesem Korridor: raue Pixel 54 544 → 35 520, 50 811 ersetzt, 3 733 roh
geblieben. Median der Änderung 2,3 cm, p90 7,3 cm.

## Schritt 2: Mesh

`tools/build_road_grid.py` liest `02_repaired.tif` und schneidet auf den Layer
`carriageway`. Eine 0,5-m-Zelle kommt ins Mesh, wenn ihr Mittelpunkt im
Polygon liegt und die Höhe endlich ist. Ein Viereck, das die Umrisslinie
schneidet, wird auf das Polygon geschnitten.

Vor dem Schnitt, nur auf diesem Raster:

1. Im äußeren Meter bekommt jede Zelle die Höhe der nächsten inneren
   Fahrbahnzelle. Der Spender liegt mehr als 1 m innen, oder auf der Mitte
   einer Straße schmaler als 2 m, und höchstens 4 m entfernt. Eine parallele
   Straße wird nicht kopiert.
2. Auf den äußeren zwei Zellringen: Median 3 m, dann Gauß 4 m, längs der
   Kante. Links und rechts bleiben getrennt. Eine Kette stoppt an einer
   Einmündung. Das Innere der Fahrbahn wird nicht geglättet.
3. Dieselbe Höhe wird in den 1-m-Streifen unmittelbar außerhalb des Polygons
   kopiert, damit ein Schnitt-Vertex die Straße tastet und nicht die Böschung.

Danach erst werden die Teile geschnitten, und nur wenn ein Teil 65 535
Vertices überschreiten würde. Das ist die Grenze des BeamNG-Imports, nicht
der COLLADA-Datei. Pro Teil wird keine Höhe mehr geändert. Der Bau bricht ab,
wenn ein gemeinsamer Schnitt um mehr als 1 cm auseinanderliegt oder eine
offene Kante mehr als 0,75 m innerhalb des Polygons liegt. In dem Fall wird
nicht ins Level kopiert.

Jeder Vertex liegt 4 cm über dem Raster (`CLEARANCE_M`). Die absolute Höhe
ist Rasterhöhe; im DAE steht `(z - z_min) + 0,04`, die Lage kommt aus
`crs_to_terrain` (Span 8191). `squareSize` bleibt 1,0.

Jede DAE enthält zwei Flächen. `part_XXX_a999` wird gezeichnet.
`collision-1/Colmesh-1` wird nicht gezeichnet. Das Levelobjekt nutzt
`collisionType: Collision Mesh`, das Rad liegt auf dem Colmesh. Beide Flächen
haben dieselbe Höhe, einschließlich der 4 cm. Das Colmesh ist das sichtbare
Mesh, innen auf ein 2-m-Raster zusammengezogen. Rand-Vertices bleiben, damit
Umriss und Schnitte nicht aufgehen. Eine Winkelgrenze von 2° ist nicht gelaufen.
Das Colmesh nach unten zu schieben würde das Fahrzeug durch den sichtbaren
Asphalt fallen lassen. Eine Dicke nach unten, deren Oberkante auf der
sichtbaren Fläche bleibt, ist nicht gebaut.

Das Gelände neben und unter dem Mesh ist das rohe 0,5-m-DGM
(`apply_corridor_dgm.py`, Layer `corridor_dgm`, Priorität 45). Das reparierte
Fahrbahn-Raster geht nicht in die Heightmap. Die Spannen-Schicht bleibt bei
55, das Bohrloch wird nicht aufgefüllt. Eine Absenkung des Geländes um 6 cm
unter dem Mesh ist nicht gebaut.

## Befehl für das reine Straßenmesh

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\repair_dgm_transect.py
```

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\build_road_grid.py --heights "data\processed\tirol-imst-8192\dgm_repair_transect\02_repaired.tif" --clip "data\processed\tirol-imst-8192\centerline_shift_taper\centerline_shift.gpkg" --clip-layer carriageway
```

Der zweite Befehl kopiert die DAE ins Level und löscht nicht mehr genutzte
Namen. BeamNG danach ganz beenden und neu starten.

## Brücke im selben Mesh

`tools/blend_bridge_deck.py` hängt die Brücke an dieses Raster. Die Achse ist
das Brückenstück in `centerline_shift.gpkg` (EPSG:31254). Das DOM wird längs
der Straße vorgefiltert, damit ein Fahrzeug herausfällt. Im Querschnitt endet
die Fahrbahn an Geländer (unter 1,5 m über der Platte), Mauer (darüber) und
Schlucht. Danach Glättung längs der Straße, und auf 12 m an den Widerlagern
eine Mischung mit dem reparierten Straßen-DGM. Ausgabe
`03_with_bridges.tif` und `carriageway_bridged.gpkg`. Das Mesh baut
`build_road_grid.py` daraus, mit denselben Regeln wie die Straße.

Das Gelände begrenzt `apply_corridor_dgm.py` an der Fläche, aus der das Mesh
geschnitten ist: `road_grid/road_grid_z.tif`, das `build_road_grid.py` nach
Randfüllung und Glättung schreibt (Fahrbahnzellen plus der kopierte 1-m-Streifen
außerhalb des Umrisses). Im Streifen von 1,1 m außerhalb der Fahrbahn darf
kein Pixel höher liegen als die nächste Zelle dieses Rasters minus eine
Höhenstufe (auf dieser Karte etwa 3,3 cm). Auf der Fahrbahn selbst darf es
nicht höher liegen als das Raster an der Stelle minus zwei Stufen (etwa
6,6 cm). Das Mesh liegt weitere 4 cm über dem Raster. Tieferes Gelände bleibt.
Die Spanne wird dabei nicht aufgefüllt.

Das reparierte DGM (`03_with_bridges.tif`) ist dafür nicht der richtige Bezug:
im äußeren Meter liegt das Mesh auf Spenderhöhen aus dem Inneren, auf der
Bergseite bis zu 30 cm unter dem DGM. Am 30.09. an `part_004` gemessen: mit
dem DGM als Bezug lagen 2 902 von 33 703 Vertices mehr als 3,3 cm unter der
Grenze und das Gelände schaute durch; mit `road_grid_z.tif` liegen noch 3
Vertices unter dem Gelände. Fehlt das Raster, fällt das Skript auf das DGM
zurück und sagt das.

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\blend_bridge_deck.py
```

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\build_road_grid.py --heights "data\processed\tirol-imst-8192\dgm_repair_transect\03_with_bridges.tif" --clip "data\processed\tirol-imst-8192\dgm_repair_transect\carriageway_bridged.gpkg" --clip-layer carriageway
```

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\apply_corridor_dgm.py
```

## Brückenversuch

Verworfen. Nicht fortsetzen und nicht mit dem Abschnitt darüber verwechseln.
`tools/apply_bridge_deck.py` bleibt unbenutzt.

Gewollt war: dieselbe Vorschrift auf der Brücke. Querprofile je Hälfte, längs
der Achse gemischt, ein zusammenhängendes Raster, damit Straße und Brücke
keine Naht haben können. Fahrzeuge und Mauern wären dabei keine Stützstellen,
analog zum rauen Randpixel. Die Heightmap bleibt unberührt. Die Fahrbahn der
Brücke liegt im selben Straßenmesh.

Gebaut wurde ein eigenes Werkzeug, `tools/apply_bridge_deck.py`. Es kopiert
`02_repaired.tif` nach `dom_bridge_deck/deck.tif`, holt je markierter Brücke
das DOM mit 0,5 m (`data/raw/dom_tirol-imst-8192_oid{oid}.tif`) und hängt
zusätzliche Polygone an den Clip. `build_road_grid.py` läuft danach auf
dieses Raster und diesen Clip. Die Funktionen `_blend_1d`, `_known_half`,
`_blend_rows` und `_paint_half` werden aufgerufen. Die Eingaben und das, was
geschrieben wird, sind andere.

Zwischenstände, die alle im Level waren und verworfen wurden:

1. Median über die inneren ±4 m, und nur angehoben. Ein Fahrzeug, das mehr
   als die halbe Breite bedeckt, wurde zur Fahrbahn.
2. Unteres Cluster statt Median, eine Höhe über die ganze Breite. Das ist
   keine Rekonstruktion.
3. Je Versatz die Differenz zur Mittellinie, längs gemischt, höchstens 50 m.
   Geschrieben, sobald die Zelle um mehr als 25 cm abwich. Eigenes Polygon
   aus der Reichweite des Profils. Die sauberen DOM-Proben blieben stehen,
   die Fläche war wellig, die Kante gestuft.
4. Zusätzlich Median 4 m und Gauß 8 m längs jedes Versatzes, dann 1 m und
   1,5 m quer, nur auf endlichen Läufen. Die Fläche wurde glatter, die Seite
   nicht. Dieser Glätter liegt nicht mehr im Code.
5. Der Glätter wurde entfernt. Stützstellen auf der Zufahrt sind das
   reparierte Straßenraster, auf der Öffnung das DOM. Das ist der Stand im
   Level. Er funktioniert nicht.

Markiert und geprüft, nicht als Mesh-Road und nicht in der Heightmap:
Spitzackerbrücke 3952, Malchbachbrücke 2428, dazu 2847, 4616, 4617.
2837, 11723 und 3948 wurden ausgelassen (eine Seite fällt nicht ab, oder
keine Öffnung). Lärmschutzwände und Brüstungen sind nicht gebaut. Die
vereinbarte Schwelle, unter 1,5 m Geländer und darüber Mauer, ist nur eine
Regel, kein Objekt.

### Unterschiede zur Straßenmethode

| | Straße | Brückenversuch |
|---|---|---|
| Achse | verschobene Centerline, am Fahrbahnpolygon geschnitten | GIP-Knoten der Brücke, über `beamng_to_crs` (damals noch 8192-Raum), 80 m über die Enden hinaus verlängert. Die verschobene Centerline quert die Schlucht nicht |
| Höhenquelle | DGM | auf der Öffnung das DOM; auf der Zufahrt das schon reparierte DGM |
| Was eine Stützstelle ist | im Polygon, Rauheit unter 1,5 cm | auf der Öffnung: innerhalb ±0,35 m um das Minimum der inneren 4 m. Darüber (Fahrzeug, Mauer) zählt als drin, aber nicht als Stützstelle. Darunter (Schlucht) beendet den Strahl. Die 1,5-cm-Rauheit wird nicht verwendet |
| Ganze Station gesperrt | Kunstbau-Zone | wenn das Minimum selbst mehr als 0,35 m über dem längs gemischten Minimum liegt |
| Was geschrieben wird | nur das raue Pixel | jede Zelle im neu gebauten Streifen, deren Profil endlich ist. Sauberes DOM bleibt die Fläche |
| Breite | das vorhandene Fahrbahnpolygon | äußerster Versatz, der vom Zentrum nach außen lückenlos endlich ist, danach das Minimum in einem Fenster von 10 m. Daraus werden Trapeze und ein zusätzliches Polygon |
| Anschluss | ein Polygon, ein Raster | das neue Polygon überlappt die Zufahrt um 12 m Straßenproben, höchstens 40 m weit. Eine vorhandene Fahrbahn, die um mehr als 2,5 m angehoben würde, wird zurückgesetzt (Straße unter der Brücke) |
| Glättung | nur die äußeren zwei Ringe, 4 m, nach dem Raster | im letzten Stand ebenfalls nur dieser Kantendurchgang. Davor ein eigener Gauß von 8 m auf dem ganzen Profil |
| Öffnung erkennen | die Strukturzone ist tabu | DOM minus DGM über 0,4 m auf mindestens 8 m, und beide Seiten bei 9 m Versatz im Median mehr als 2 m tiefer |

Gleich geblieben sind nur die Mischregel (Differenz zur Mittellinie, je
Versatz, je Hälfte, höchstens 50 m, keine Ergänzung über die äußeren
Stützstellen) und der Kantendurchgang von `build_road_grid.py`, weil der auf
jedem Clip läuft.

Damit ist das Raster nicht das Straßenraster mit einer weiteren Achse. Es ist
ein zweites Profil aus einer anderen Höhenquelle, mit einer anderen
Stützstellenregel, in ein eigenes Polygon geschrieben und an die Zufahrt
angeklebt.
