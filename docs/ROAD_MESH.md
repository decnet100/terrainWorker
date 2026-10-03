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
| Kunstbauten | Layer `segments`, `structure = 1`. Diese Spannen sind weder Stützstelle noch Ziel. Ein GIP-Brückenstück, dessen Flag `bridge: false` ist (DGM ist schon die Fahrbahn, oder die DGM-Fläche *ist* das obere Deck), bleibt `structure = 0` und bekommt eine Fahrbahn aus dem DGM |

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
das Brückenstück in `centerline_shift.gpkg` (EPSG:31254). Im Querschnitt des
DOM endet die Fahrbahn an Geländer (unter 1,5 m über der Platte), Mauer
(darüber) und Schlucht. Ein Fahrzeug (mehr als 30 cm über der inneren Platte,
mit 0,5 m Saum für die verschmierte Flanke) ist ein Loch in der Platte, keine
Kante.

Die Breite ist ein Wert je Seite über die ganze Öffnung, nicht je Station:

1. Randstein: eine Stufe von 8 bis 35 cm quer zur Straße, über 0,75 m
   gelesen, die dahinter 0,75 m in diesem Band bleibt. Wird sie auf
   mindestens 10 m Stationen gefunden, setzt ihr Median die Halbbreite.
2. Sonst die halbe GIP-Fahrbahnbreite.
3. Das Geländer kann nur schmaler machen: 95-%-Quantil der Reichweite je
   Station bis zum Randobjekt (eine Fahrzeugreihe steht nicht an jeder
   Station, ein Geländer schon). Schneidet es eine GIP-Hälfte, liegt die
   Achse außermittig, und die andere Seite bekommt das Defizit, soweit ihr
   Geländer es erlaubt.

Die Höhen sind Achslinie plus Profil je Versatz. Löcher (Fahrzeuge) schließt
eine lineare Interpolation längs der Straße aus den Nachbarstationen, also
mit der Querneigung der Platte; den Saum neben dem Geländer, der an jeder
Station leer ist, füllt der letzte Profilwert quer. Danach Median (5 bis
15 m) und Gauß (4 m) längs, und auf 12 m an den Widerlagern eine Mischung mit
dem reparierten Straßen-DGM. Gestempelt wird an Stationen und
Zwischenstationen (0,25 m) und einen Versatz über die Kante hinaus, damit
jede Rasterzelle im Polygon eine Deckhöhe hat.

Ausgabe `03_with_bridges.tif`, `carriageway_bridged.gpkg` (Layer
`carriageway` für das Mesh, Layer `bridge_deck` mit den Platten allein) und
`bridge_deck_report.json` mit `half_left_m`, `half_right_m`,
`width_source_*` (`kerb`, `gip`, `gip+shift`, `rail`), `kerb_stations_*`,
`interpolated_px` und `axis_fill_px` (Notfüllung mit der Achshöhe, nur an
den Stückenden). Imst 30.09.: 2428 Randstein beidseitig 4,12 + 3,88 m,
4616 Randstein links 3,88 m, alle anderen GIP-Breite; vorher lagen alle
Platten bei 8,5 bis 9,0 m, weil das Geländer mit `GIP/2 + 1 m` die Grenze
war und Gehweg und Randstein zur Fahrbahn zählten.

`smooth_road_surface.py` schützt zusätzlich zur GIP-Zone den Rand der
Deckpolygone (`bridge_deck` geschnitten mit Zone + 1 m), weil die Platte
breiter sein kann als die Zone und sonst die Straße unter der Brücke dort
hineinschreibt (auf 2428 vorher 48 Pixel um bis zu 9 m).

Das Mesh baut `build_road_grid.py` daraus, mit denselben Regeln wie die
Straße.

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

## Glatte Fahrbahn: Modell plus Restschicht

Nach der Randreparatur ist das Innere der Fahrbahn noch das rohe DGM. Auf
Imst waren am 30.09. noch 12 % der Fahrbahnpixel „heiß“ (Abweichung vom
1,5-m-Fenstermittel ab 1,5 cm), p99 lag bei 12,6 cm: Fahrzeuge, Schachtdeckel,
Mischpixel, Scanrauschen von etwa 0,5 cm.

`tools/smooth_road_surface.py` glättet nicht lokal, sondern zerlegt. Eine
Straße ist konstruktiv glatt: Gradiente längs mit großen Ausrundungsradien,
Querprofil pro Station, das sich längs nur langsam ändert. Pro `objectid`:

1. Jedes Fahrbahnpixel bekommt Station `s` und vorzeichenbehafteten Abstand `t`
   zur Achse (links positiv).
2. Modell `z = a(s) + b_L(s)·max(t,0) + b_R(s)·min(t,0)`, für alle Stationen
   gemeinsam gelöst, mit Strafe auf der zweiten Differenz längs
   (Whittaker-Glätter, Grenzwellenlänge 10 m für `a`, 30 m für die
   Querneigungen). Anders als ein Gauß reproduziert das Parabeln: Kuppe und
   Wanne behalten ihre Höhe, nur kürzere Wellen werden gedämpft.
3. Robuste Gewichte (Tukey, 4 Durchläufe): Fahrzeuge und Mauern fallen aus der
   Anpassung heraus, ohne vorher per Rauheitsschwelle maskiert zu werden. Der
   äußere Meter des Polygons ist keine Stützstelle (Böschungsmischpixel; das
   Band füllt `build_road_grid.py` ohnehin aus dem Inneren).
4. Rest `z − Modell` wird in Straßenkoordinaten tiefpassgefiltert (σ 2 m längs,
   1 m quer), gewichtet mit einem Biweight fester Skala (null ab 8 cm). So
   bleiben Buchten, Aufweitungen und ein einspuriger Belagsaufbau erhalten;
   ein Fahrzeug nicht.
5. Ausgabe `04_smooth.tif = Modell + behaltener Rest`. `04_residual.tif` ist
   `Eingang − Ausgang`, die entfernte Rauheitsschicht; sie kann später
   skaliert und maskiert wieder aufgesetzt werden. `04_weight.tif` ist das
   Behalte-Gewicht (0 = Ausreißer, Pixel nahm das Modell).

### Verschmälerung der Gemeindestraßen

Die GIP-Polygone der Gemeindestraßen (`OBJEKT` = `S-G`, Konstante
`NARROW_OBJEKT`) sind auf Imst oft 7 bis 9 m breit, die Fahrbahn darunter 4
bis 5 m. Der Rest ist Böschung, Bankett oder Mauerfuß. Das Modell aus
Schritt 2 legt dort eine Fläche, die Meter unter oder über dem Gelände
liegt; im Spiel ragt dann das Terrain durch das Mesh (36279, 66314).

Darum bekommen nur diese Objekte einen zusätzlichen Schritt zwischen 3 und 4:

1. Pro 2 m Station und pro Seite wird in 0,5-m-Abstandsbins der Median von
   `|z − Modell|` gebildet. Von der Achse nach außen ist die Fahrbahnkante
   der letzte Bin, dessen Median ≤ 12 cm bleibt (ein leerer Bin wird
   toleriert, wenn der nächste wieder passt).
2. Die Kantenlinie wird längs median- (10 m) und gaußgefiltert (σ 4 m) und
   auf mindestens 1,5 m Halbbreite, höchstens die Polygonkante begrenzt.
3. Die Anpassung wird auf den Pixeln innerhalb der Kante wiederholt
   (bis zu 3 Runden). Damit verschwindet der Querneigungs-Clamp, der vorher
   die Böschung ausgleichen musste.
4. Wurde irgendwo mindestens 0,3 m abgeschnitten, ersetzt ein Band variabler
   Breite entlang der Achse das Polygon. Pixel außerhalb behalten den
   Eingang.

Alle Polygone, verschmälert oder nicht, landen in
`road_surface_smooth/carriageway_smooth.gpkg` (Layer `carriageway`, Felder
`objectid`, `narrowed`, `narrow_m`). `build_road_grid.py --clip` und
`apply_corridor_dgm.py` lesen diese Datei anstelle von
`carriageway_bridged.gpkg`. Landes- und Bundesstraßen (`S-L`, `S-B`) bleiben
unverändert; ihre Polygonbreite stammt aus den Fahrstreifenregeln. Der Report
nennt unter `narrowed_carriageways`, `narrowed_max_m` und `most_narrowed`,
was beschnitten wurde; im Unrolled-Plot markieren schwarze Linien die
behaltene Kante.

Bauwerksspannen behalten den Eingang. Querneigung über 15 % ist Böschung in
einem zu breiten Polygon und wird begrenzt (129 von 1 492 Objekten auf Imst
vor der Verschmälerung).
Behält die robuste Anpassung weniger als 35 % der Stützpixel, liegt im Polygon
keine Fahrbahn (Felseinschnitt, versetzter Stummel); das Objekt behält den
Eingang und steht im Report unter `unfit_carriageways` (6 auf Imst). Wo sich
zwei Fahrbahnpolygone überlappen, mischt der Abstand zur eigenen Polygonkante
die Modelle stetig; liegen sie mehr als 8 cm auseinander (Terrasse,
Stützmauer zwischen parallelen Straßen), blendet der Ausgang zum gemessenen
Eingang zurück (`conflict_px`, 16 000 Pixel auf Imst). Das ist kein Fehler
dieses Schritts, sondern ein Breitenproblem der Polygone; die größten
verbliebenen Stöße in der Radspur liegen genau dort (Kreuzung 96004/17963 bei
31090/233457).

Ergebnis Imst 30.09., 1 492 Objekte in 2 bis 7 Minuten: heiße Pixel 288 897 →
42 078 (12 % → 1,7 %), p90 1,7 → 0,5 cm, p99 12,6 → 2,0 cm. Radspur-Metrik
(Betrag der zweiten Differenz längs bei ±1,0 und ±2,5 m, Schritt 0,5 m, 2 m
Abstand zu Bauwerken): p90 1,9 → 0,2 cm, p99 9,9 → 2,1 cm, Anteil ab 1 cm
29 % → 2 %. Der Report `road_surface_smooth/report.json` enthält die
Verteilungen, die 15 Objekte mit den meisten Ausreißern und die unangepassten
Objekte; die Plots (englisch beschriftet) zeigen Radspur, Querschnitt, die
abgewickelte Fahrbahn und beide Metriken vorher und nachher.

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\smooth_road_surface.py
```

`--oid 6975` beschränkt auf einzelne Objekte und schreibt dann nach
`road_surface_smooth_trial/`, damit das Site-Ergebnis nicht überschrieben
wird. `--plot-oid` wählt die Objekte für die Einzelplots (Default 6975).

Das Mesh dann aus dem glatten Raster und den verschmälerten Polygonen:

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\build_road_grid.py --heights "data\processed\tirol-imst-tarrenz-8192\road_surface_smooth\04_smooth.tif" --clip "data\processed\tirol-imst-tarrenz-8192\road_surface_smooth\carriageway_smooth.gpkg" --clip-layer carriageway
```

Danach `apply_corridor_dgm.py` für die Heightmap-Klammer, Import von
`terrainPreset.json`, BeamNG ganz beenden und neu starten. Die Klammer liest
`road_grid/road_grid_z.tif`; ein älterer Stand der Heightmap passt nicht zu
einem neu gebauten Mesh.

## Mehrere Meshes an der Kreuzung ohne Knoten

Ein 2,5D-Raster hat eine Höhe je XY. Brücke und Unterführung liegen in der
Draufsicht übereinander, in der Höhe mehrere Meter auseinander. Sie können
nicht in derselben COLLADA-Fläche liegen: die Kante würde die 8 m Höhenunterschied
über eine Zelle triangulieren.

Der Routing-Graph markiert genau diese Stellen (`LEVEL_INTERMEDIATE`, keine
gemeinsame Node). Jedes WFS-Stück hängt an einer Kante, nicht an überlappender
Geometrie; Deck und Unterführung werden nicht in ein Polygon gelegt.
`smooth_road_surface.py` schreibt die untere Fläche nach `04_under.tif` und
lässt beide Polygone vollständig. `build_road_grid.py` schneidet dann getrennte
Meshes:

| Dateiname | Inhalt |
|---|---|
| `part_NNN.dae` | Fahrbahn in einer Ebene, ohne die gestapelten Paare |
| `over{OBJECTID}_NNN.dae` | obere Fläche (Brücke) |
| `under{OBJECTID}_NNN.dae` | untere Fläche (Unterführung) |
| `span{OBJECTID}_NNN.dae` | dieselbe Straße ist an einer Stelle oben und an einer anderen unten |

An so einer Stelle liegen mindestens zwei Meshes im Level. Die Heightmap-Klammer
nimmt dort die untere Fläche, damit das Gelände nicht bis an das Deck darf.
Der Randstreifen (`EDGE_BAND_M`, Spender `EDGE_DONOR_M`) läuft nur innerhalb
desselben Meshes, nicht von der Brücke in die Unterführung.

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
