# Kunstbauten: Brücken, Tunnel, Galerien, Rampen

Anleitung zum **Einbauen und Nachziehen** in einer Site-YAML (`config/sites/*.yaml`). Technischer Hintergrund und WFS-Cache: [GIP.md](GIP.md). Heightmap-Schichten: [HEIGHTMAP_COMPOSE.md](HEIGHTMAP_COMPOSE.md). Import ins Spiel: [BEAMNG_IMPORT.md](BEAMNG_IMPORT.md).

Die Fahrbahn ist immer die **Oberkante**. MeshRoad-Node-Z, Centerline-Z, Heightmap unter der Platte und Decals beziehen sich auf dieselbe Fläche, nicht auf Plattenmitte oder Unterkante.

Alle Kunstbauten sitzen auf **Auflagen / Widerlagern**. Heightmap an den Portalen auf die Oberkante, letzte Meter der Zufahrt als Pad, Bohrloch und Brückenspanne unberührt. `approach_conform` und `force_deck_z` sind Code-Defaults — nicht pro Site neu anschalten. Abschalten nur am Item. Vor der Tür eine eigene Platte: `append_unify` (zweites Unify, `kind: road`).

---

## Was das GIP liefert

Die Klasse steht in **`OBJEKT`**, nicht im Straßennamen. `KUNSTBAUTEN` ist Freitext und schwächer. `STRNAME` (z. B. „Landecker Tunnel“) ist **kein** Tunnel-Schalter.

| `OBJEKT` | Bedeutung | Pipeline |
|----------|-----------|----------|
| `S-A` / `S-B` | Stammstrecke Autobahn / Bundesstraße | volles Kit (Decal, Road-Bed, Guardrails) |
| `S-AT` / `S-BT` / `S-LT` | Tunnel | Galerie/Tunnel-Mesh, kein Oberflächen-Decal im Bohrloch |
| `S-AB` / `S-BB` / `S-LB` / `S-GB` | Brücke (Autobahn, Bundes-, Landes-, Gemeindestraße) | MeshRoad-Deck |
| `S-BG` | Galerie, falls das GIP sie so taggt | offene Schale |
| `S-AR` / `S-AP` / `S-BR` / `S-BP` | eine Spur, der Stammstrecke **untergeordnet** | 3,75 m, keine eigenen Schienen; Einfahrt an der Stammstrecke bleibt offen |
| `S-FRW` / `S-STRAIL` | Fuß-/Radweg | Kies, kein Road-Bed; Seitenschnitt der Platte **darf** durchgehen |

Galerien stehen in `OBJEKT` oft als Tunnel. Dann entscheidet der **Name** (`Galerie` in `KUNSTBAUTEN` oder `STRNAME`).

Oberflächenstücke, deren Name „Tunnel“ enthält, aber frei liegen, gehören nach `data/roads/gip_overrides.yaml` (`not_tunnel_objectids`). Die Site-YAML kann dieselbe Liste ergänzen.

Zwei parallele GIP-Hälften (Richtungsfahrbahnen) **nicht** getrennt als zwei Tunnel bauen. Stattdessen eine Mittelachse:

```yaml
beamng:
  roads:
    unify:
      - id: landecker
        objectids: [227551, 235599]
        mode: mean_axis
        width_m: 7.5
        replaces: [galleries, decals, guardrails, road_bed]
```

`kind: tunnel` (Default) verbraucht die OIDs für das Bohrloch. Die freie Zufahrt davor ist ein **zweites** Unify mit `kind: road` — nie Bohrloch und Zufahrt in dieselbe Mittelachse legen.

---

## Nach einer YAML-Änderung

Nicht die ganze Pipeline neu fahren. Nur den betroffenen Schritt, dann Heightmap mischen (viele Builder tun das selbst).

| Du hast geändert | Befehl | Im Spiel |
|------------------|--------|----------|
| Brücke (`beamng.bridges`) | `python tools\build_bridges.py` | Heightmap neu importieren |
| Galerie/Tunnel (`beamng.galleries`, `unify` für das Bohrloch, Seitenschnitt) | `python tools\build_galleries.py` | **BeamNG ganz beenden**, neu starten, Heightmap importieren |
| Galerie/Tunnel an einer Magliasina-Straße | danach `python tools\import_magliasina_roads.py --crop ...` (schneidet an den Übergabelinien) | wie oben |
| Nur Heightmap-Schichten neu mischen | `python tools\compose_heightmap.py` | Heightmap importieren |
| Rampe-Z / Decal / Road-Bed (`follow_parent`, `decal_roads`) | `python tools\build_decal_roads.py` | Karte neu laden reicht für Decals; Heightmap importieren fürs Gelände |
| Guardrails | `python tools\build_guardrails.py` | **BeamNG ganz beenden** (TSStatics) |
| Asphalt-Maske (Breite der Spur) | `python tools\build_terrain_masks.py` | Terrain-Textur / Preset neu |

PowerShell, eine Zeile, Site setzen:

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/reschen.yaml"; python tools\build_galleries.py --only 227551 --skip-holemap
```

`--only` ist eine GIP-OBJECTID (bei Unify die erste der `objectids`). `--skip-holemap` lässt die LocKarte in Ruhe, wenn sich nur das Gelände neben der Platte ändert.

**World Editor nicht speichern**, wenn Inject die Quelle ist (Galerien, Guardrails, Decals). Sonst überschreibt die Session die JSON.

Lua, `portals.json`, injizierte TSStatics/DAE: Spiel **ganz beenden**, nicht nur Freeroam → Karte laden. Details: `.cursor/rules/beamng-neustart.mdc`.

---

## Neue Brücke

1. GIP-OBJECTID der Brücke (`S-AB` / `S-BB` / `S-LB` / `S-GB`, oder `KUNSTBAUTEN` mit „Brücke“). Fehlt das Feature im Cache: `beamng.bridges.gip_extra` mit `objectid` (siehe [GIP.md](GIP.md)).
2. In `beamng.bridges.items` matchen und nur Abweichungen von `defaults` setzen.

```yaml
beamng:
  bridges:
    items:
      - match: { objectid: 10154 }
        force_deck_z: true
        force_deck_z_sink_m: 0.02
```

3. Sitzt die automatische Spannweite in einer DGM-Kerbe, Widerlager per Station festnageln (`abutment_s` auf dem GIP-Korridor der Brücke). Werte aus `bridges_meta.json` / Längsschnitt, nicht raten.
4. `approach_conform_max_raise_m` klein halten, sonst füllt die Heightmap die Schlucht. `max_cut_m` darf größer sein (Klippenlippe).
5. Bauen, Heightmap importieren. Guardrails kommen **nicht** aus `build_bridges` — danach `build_guardrails.py`, wenn die Schienen auf dem Deck sitzen sollen (`bridge_deck_z: true`).

---

## Galerie oder geschlossener Tunnel

`beamng.galleries.defaults` plus `items[]` mit `match: { objectid: … }` oder `match: { unify: <id> }`.

| Ziel | Wichtige Schlüssel |
|------|-------------------|
| Offene Galerie | `open_side: auto` (Default): offen zur Talseite, also zur Seite, auf der das DGM 8–20 m neben der Achse tiefer liegt (Log `open_side auto`). `left` / `right` gelten zur Straßenrichtung und setzen die Seite fest. Gelände über der Spanne bleibt unangetastet (`terrain_roof: none`) |
| Geschlossener Tunnel | `style.shell: tunnel`, `open_side: none`. Gleicher Default: kein Geländematerial zwischen den Portalen |
| Dach/Straße oben erzwingen | nur explizit: `terrain_roof: rock` oder `keep_asphalt` (Unterführung) |
| Runde Seitenwände | `wall_radius_m` (nur geschlossene Röhre). Boden und Dach bleiben flach, die Seiten wölben nach außen. Sehne = lichte Breite. An der **Röhrengröße** orientieren (Landecker 9,5 × 4 m → **4–5 m**). `2.0` bei 4 m Höhe = Halbkreis (stärkste Wölbung), größer = flacher. Weglassen oder `0` = Rechteck. `wall_arc_segments` (Default 10) |
| Ein Portal auf der Karte | `one_ended: true`, `fake_end_drop_m: 10` |
| Platte vor dem Portal | `append_unify`, `append_m`, `abut_run_m` (nächster Abschnitt) |
| Zwei Röhren, ein Mund | nicht `merge_abutting` (das ist nur Ende-an-Ende). Unify der beiden `S-AT` |

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/reschen.yaml"; python tools\build_galleries.py
```

Ausgabe: `galleries_centerlines.json`, DAE unter `art/shapes/galleries/`, TSStatics in der SimGroup `galleries`, Span-Schicht `span_gallery`. LocKarte nur anfassen, wenn sich Portale ändern — nicht nebenbei vergrößern.

**Querneigung:** Zwischen den Portalen liegt das Deck quer waagrecht, bei Galerien wie bei Tunneln. Die Schale ist
quer waagrecht gebaut, ein geneigtes Deck ließe an einer Wand eine Lücke und liefe in die andere hinein. Bis 2026-11
nahm eine Galerie die Querneigung aus dem DGM an ihren Portalen. Dort liegt aber oft das Galeriedach, das zum Tal
abfällt, und an einer Kettenfuge das Dach der Nachbargalerie. Die Stuibagalerie kippte so durchgehend um etwa 12 %
(`crossfall_max`), passte nicht an den waagrechten Klammlecktunnel und klaffte an der Wand. Außerhalb der Portale wächst
die Querneigung des Decks über `crossfall_ramp_m` (10 m) von 0 auf die des Geländes.

### Lochkarte (Holemap)

Ein Pixel der Lochkarte ist ein **Eckpunkt** des Geländes, keine Zelle. Ein Loch am Eckpunkt (i, j) nimmt genau das
Quadrat (i, j)–(i+1, j+1) weg, also das Quadrat nordöstlich davon (im Bild rechts oberhalb). So rechnen Darstellung und
Kollision im Torque3D-Gelände (`terrFile.cpp` `_buildGridMap`, `terrCell.cpp`). Jedes Quadrat ist an einer festen
Diagonalen geteilt: bei geradem i+j von Südwest nach Nordost, sonst von Südost nach Nordwest.

Bis 2026-11 testeten `portal_hole` und `terrain_embed_portal_holes` die Pixelposition gegen die Öffnung und
markierten das Pixel selbst. Die Löcher lagen dadurch ein halbes Quadrat nach Nordost verschoben, und auf der Süd- und
Westseite ragte Gelände in die Einfahrt (Test: 7,4 von 73 m² der Öffnung blieben Gelände). Jetzt wird jedes Quadrat,
das die Öffnung schneidet, ganz gelocht (`tools/terrain_holes.py`). Der Rand ist dafür eine Treppe in 1-m-Stufen
außerhalb der Öffnung, die der Portalkragen abdecken muss.

Nach dem Compose prüft `check_tube_intrusion` jedes Gelände-Dreieck gegen den Fahrraum der Röhre (Deckbreite ×
`clear_height_m` über der Fahrbahn, Portal bis Portal). Ein Quadrat, halb auf Fahrbahnhöhe abgesenkt und halb an der
Portalfläche hoch, fällt so auf, auch wenn keiner seiner Eckpunkte im Fahrraum liegt. Gelocht wird in der
Portalzone jedes Tagportals: vom Mesh-Portal nach innen bis dorthin, wo das Gelände über die ganze Röhrenbreite über
dem Dach steht (Portalfläche und Einschnitt davor), mindestens `intrusion_fix_in_m` (6 m), höchstens
`intrusion_fix_max_m` (80 m). Dort liegt die Schale darüber. Die Länge steht als `face_m` im Meta. Ein fester 6-m-Streifen
reichte am Klammlecktunnel nicht: Das Mesh-Portal lag etwa 10 m vor der echten Portalfläche, im Einschnitt. Treffer tiefer
in der Röhre werden nur gemeldet: `galleries_meta.json` → `intrusion` (`before`, `holed`, `left`, je Röhre die Stellen).
`left` > 0 heißt meist, dass das Gelände über der Röhre unter das Dach fällt, oder die Achse liegt falsch.
Mit `--skip-holemap` wird nur gemeldet.

### Ketten: Galerie – Tunnel – Galerie

Liegen Bauwerke Ende an Ende hintereinander (GIP-Enden näher als `galleries.chain_tol_m`, Default 5 m, Art und Name
egal), baut `build_galleries` sie als **eine Kette** (`tools/structure_chains.py`). Grund: Im DGM sind Galeriedächer
als Gelände geblieben, einige Meter über der Straße. Ein Tunnel zwischen zwei Galerien hat kein Tagportal, und die
Portalsuche fand an seinen Enden den Sprung vom Galeriedach auf den Hang. Die Röhre lag dann auf dem Dach
(Klammlecktunnel zwischen Lobloch- und Stuibagalerie: 345,5/346,5 m statt etwa 337,7/338,7 m).

- Höhen gibt es nur an den beiden **äußeren Enden** der Kette, wo sie die offene Straße trifft. Dort wird das Portal wie
  bei einem einzelnen Bauwerk gesucht (Neigungssprung im DGM).
- Dazwischen läuft eine **gerade Linie**. Jede Fuge (`joint_ends` im Meta) nimmt ihre Höhe von dieser Linie, nie aus
  dem DGM.
- An einer Fuge gibt es kein Deck über das Bauwerk hinaus, keinen Portalsitz, keine Zufahrtsanpassung und keinen
  Portalkragen. Ein Tunnel behält dort seinen Portalrahmen (er steht in der Galerie), eine Galerie hat dort keinen.
  Die Lochprüfung behandelt die Fuge wie eine Portalfläche.
- Ist ein Glied `one_ended`, `road_spline` oder ein Unify, wird die Kette nicht geplant (Meldung im Log).
- Mit `--only` wird die ganze Kette geplant, gebaut werden nur die gewählten Glieder. Ihre Fugen liegen trotzdem auf der
  Kettenlinie.
- `galleries.chain: false` schaltet das ab.
- **Ein Deck für die ganze Kette:** Statt einer MeshRoad je Glied gibt es eine durchgehende
  (`gallery_deck_chain_<OIDs>`). Getrennte Decks stießen Stirn an Stirn: Eine Galerie verbreitert ihr Deck zur offenen
  Seite (`deck_open_extra_m`, 0,8 m), ein Tunnel nicht; dazu Splitter-Segmente von wenigen Zentimetern am Portal und
  ein Rundungsfehler, der den letzten Tunnelknoten wegfallen ließ (0,2 m Lücke). Jetzt läuft das Deck über die Fuge
  durch. Breite und Seitenversatz gehen über `galleries.chain_deck_blend_m` (10 m) vom schmaleren Glied an der Fuge
  linear in das breitere über, die geschlossene Kante bleibt dabei gerade. Werden nicht alle Glieder gebaut
  (`--only`), bekommt ein Glied ohne gebauten Nachbarn sein eigenes Deck.

Die Kette steht in `galleries_meta.json` → `chains` (Glieder, äußere Portale, Steigung, Fugenhöhen).

### Übergabe an eine Magliasina-Straße

Magliasina baut Galerien und Tunnel nicht (`SKIP` in magliasinaNG `network.py`), aber die Lücke im Export beginnt
nicht an unserem Portal. An der Klammleck-Kette lief der Magliasina-Asphalt rund 8 m in beide Galerien hinein, mit
7–9 % Querneigung, und unser waagrechtes Deck lag darüber. Zwei Fahrbahnen schnitten sich, im Spiel ein Flickwerk.

Deshalb gibt es an jedem Tagportal mit Magliasina-Straße davor eine **Übergabelinie**, `magliasina_handover_m`
(10 m) vor dem Portal:

- `build_galleries` liest die Magliasina-Meshes direkt aus `magliasina/magliasina_work/work/roads_only`
  (`tools/magliasina_surface.py`). An der Linie misst es den Querschnitt: Kanten, Mittelhöhe, Breite, Querneigung,
  Versatz der Mitte zu unserer Achse. Die Kanten werden auf den Millimeter eingegabelt.
- Die Linie braucht einen ruhigen Querschnitt: Über die nächsten 3 m nach außen darf jede Kante höchstens 3 cm von
  einer Geraden abweichen. Eine gleichmäßige Aufweitung ist erlaubt, ein Sprung im Mesh-Rand nicht. An der Stuiba
  sprang der Rand direkt an der 10-m-Linie um 29 cm, dort liegt die Linie jetzt bei 11 m. Ohne Asphalt oder ohne
  ruhigen Querschnitt rückt die Linie in 0,5-m-Schritten bis 10 m weiter nach außen.
- Das Deck beginnt genau an der Linie mit diesem Querschnitt und hält ihn über das erste Segment (1 m). Danach gehen
  Höhe, Breite, Querneigung und Versatz linear auf unsere Werte am Portal über: waagrecht, eigene Breite, eigene
  Achse. Die Endkante eines MeshRoads steht rechtwinklig zu seinem ersten Segment. Ändert sich der Versatz schon
  dort, ist sie um 1–3° verdreht.
- Geschnitten wird rechtwinklig zu diesem ersten Segment, also genau entlang der Endkante des Decks. Im Sandbox
  passen Höhe und Kanten an beiden Galerien auf ±5 mm.
- Die Linien stehen in `magliasina_handover.json`. Linien von Bauwerken, die im Lauf nicht gebaut werden, bleiben
  erhalten. `import_magliasina_roads` schneidet die Meshes dort: Dreiecke werden an der Linie geteilt, der Teil zum
  Bauwerk hin fällt weg (Breite + 2 m je Seite, bis 40 m hinter das Portal).
- Die Übergangsprüfung misst Stufe und Knick am Deckende gegen die Magliasina-Oberfläche.

Reihenfolge: **erst `build_galleries`, dann `import_magliasina_roads`**, danach `compose_heightmap`.
`galleries.magliasina_handover: false` schaltet das ab.

Sandbox, Klammleck-Kette: Linien 10 m vor beiden Portalen. Querschnitt dort: Stuiba 8,35 m breit, 1,7 %
Querneigung; Lobloch 8,80 m, −0,3 %. Deckende gegen Magliasina an Mitte und beiden Kanten auf 0–8 mm gleich,
Übergänge 4 von 4 ok.

### Übergangsprüfung (Messlatte)

Nach dem Bau prüft `check_structure_transitions` jeden Übergang. Kein Übergang darf einen unmöglichen Sprung haben.

- **Tagportal:** Geprüft wird das Deck von 20 m innen über das Portal bis zu seinem Ende: der größte Neigungsknick
  (Neigung über `transition_window_m` = 5 m vorher gegen nachher). Am Deckende wird außerdem die Stufe und der Knick
  auf die Straße draußen geprüft (Straßenachse, sonst DGM).
- **Fuge einer Kette:** Stufe und Knick zwischen den beiden Decks. Ist der Nachbar in diesem Lauf nicht gebaut, wird
  gegen die Kettenlinie geprüft.

Grenzen in `beamng.galleries`:

| Schlüssel | Default | |
|---|---|---|
| `transition_step_max_m` | 0,10 | Höhenstufe, darüber unmöglich |
| `transition_kink_warn` | 0,03 | Neigungsknick, darüber Warnung |
| `transition_kink_max` | 0,08 | Neigungsknick, darüber unmöglich |
| `transition_check` | `error` | `error` bricht den Bau ab, `warn` meldet nur, `off` |

Ergebnis: `galleries_transitions.json` (jede Zeile mit `verdict` ok / warn / fail), Zusammenfassung im Meta.
Gegenprobe Klammleck-Kette ohne Kettenlogik: 5 von 6 Übergängen unmöglich (Loblochgalerie stieg auf 372 m). Mit
Kettenlogik: 4 geprüft, alle ok.

### Längsprofil prüfen (`profile_check.py`)

Die Übergangsprüfung misst nur an Portalen und Fugen. `tools/profile_check.py` prüft den ganzen Weg: durch das Deck
und je 50 m (`--outside`) auf der Magliasina-Straße davor und dahinter. Es misst so, wie die Flächen im Level
liegen:

- das Deck-MeshRoad aus `galleries_items.level.json`, aus den Knoten nachgebaut. Zwischen den Knoten ist es gerade;
  BeamNG legt dort einen Spline;
- Magliasinas Straßenmesh, an den Übergabelinien geschnitten wie beim Import;
- das Gelände (zusammengesetztes Heightmap, in BeamNGs eigenen Dreiecken), ohne die Löcher der Hole Map.

Abgetastet wird alle 0,1 m auf fünf Spuren: Achse sowie ±0,9 m und ±2,6 m, das sind die Radspuren zweier 3,5-m-Spuren
(`--offsets`). Gefahren wird auf der höchsten Straßenfläche nahe der letzten Höhe, ohne Straße auf dem Gelände. Je Spur
gibt das Werkzeug aus:

| Größe | was | Grenzen (warn / fail) |
|---|---|---|
| Stufe | Höhensprung von Probe zu Probe abzüglich der örtlichen Steigung | 2 cm / `transition_step_max_m` (10 cm) |
| Knick 2, 5, 10 m | Steigung danach minus Steigung davor | `transition_kink_warn` / `transition_kink_max` (3 % / 8 %) |
| Überlappung | Deck und Magliasina beide da und mehr als 1 cm auseinander | Warnung |
| Gelände über der Straße | 2 cm bis 1,5 m über der Fahrfläche | Warnung |
| Lücke | keine Fläche | – |
| Randsprung | Sprung des Fahrbahnrands quer zur Achse von Station zu Station (alle 0,25 m), abzüglich des örtlichen Verlaufs | 2 cm / 10 cm |
| Randknick 2, 5 m | Richtungsänderung des Rands gegen die Achse (Versatz je Meter danach minus davor) | 5 % / 15 % |
| Achsecke | Richtungsänderung der Deckachse an einem einzelnen Knoten über 3° | Warnung |

Der Fahrbahnrand ist die Stelle, an der quer zur Achse weder Deck noch Magliasina-Asphalt mehr da ist. Er wird auf
den Millimeter gesucht, links und rechts getrennt; die Breite wird nur angezeigt. Quer heißt hier: quer zur Richtung
über ±2 m. An einer Achsecke würde eine örtliche Querrichtung sofort umschwenken, und der Rand schiene um Zentimeter
zu springen, ohne dass er sich bewegt.

Eine **Achsecke** ist ein Knick der Knotenlinie selbst. Die Klammleck-Achse folgt den Eckpunkten der GIP-Linie und
knickt an einzelnen Knoten um bis zu 10°. Die Schale wird von Knoten zu Knoten gerade gelofted und hat dort eine
Ecke, das Deck rundet BeamNG mit einem Spline ab. Rand und Wand laufen an solchen Stellen um einige Zentimeter
auseinander.

Befunde zählen nur auf dem Deck und bis 2 m davor oder dahinter. Was weiter draußen auf Magliasina liegt, steht in
einer eigenen Zeile („not judged“) und in `magliasina_only`. So kippt das Urteil nicht wegen Magliasinas eigener
Straße.

Zu jedem Höchstwert steht die Stelle (u = m ab Deckanfang, negativ davor) und die Fläche, auf der er liegt. Ein Knick
in Magliasinas eigener Straße ist so von einem an unserer Übergabe zu unterscheiden. Ergebnis:
`profile_check.json` und je Deck `profile_check_<deck>.png`. Das Bild zeigt je Übergang (Deckende, Portal, Fuge)
einen vergrößerten Ausschnitt mit vier Bahnen: Unebenheit (Höhe minus gleitendes 10-m-Mittel), Knick über 5 m,
Stufe und Fahrbahnrand (Versatz minus gleitendes 10-m-Mittel; links rot, rechts blau, Breite schwarz). Der Hintergrund zeigt die Fläche: Deck blau, Magliasina grün, Gelände braun.

Reihenfolge: nach `build_galleries`, `import_magliasina_roads` und `compose_heightmap`.

    $env:AUTOROAD_SITE = "config/sites/pfunds_spiess.yaml"; python tools\profile_check.py --only 3125,3127,3128

Klammleck-Kette im Sandbox: keine Stufe über 1,1 cm, kein Knick über 1,4 % auf 5 m, kein Randsprung über 0,2 cm.
Die größten Werte sind:

- 1,1 cm an der Lobloch-Übergabelinie auf der äußeren Radspur. Magliasinas Querschnitt ist dort nicht ganz eben,
  unser Deck schon.
- 1,7 % Knick am Stuiba-Portal. Dort wechselt die Steigung der Kette (−0,5 %) in die Übergabe zur Magliasina-Höhe.
- ein Sägezahn von ±0,3 cm je Knoten auf der Übergabestrecke. Die Querneigung ändert sich dort von Knoten zu Knoten,
  und jedes Viereck des MeshRoads knickt an seiner Diagonale.
- bis 4 % Knick auf 2 m auf Magliasina selbst, 35 m hinter der Stuiba.
- Randknicke von 3,1–3,3 % je Seite an den Fugen und am Stuiba-Deckende. Das sind die Ecken der Breiten- und
  Achsangleichung über 10 m: Galerie gegen Tunnel, Deck gegen Magliasina.
- 21 Achsecken von 3,7–10,4°.
- auf Magliasina selbst, 25–40 m hinter der Stuiba: der Rand franst aus, ±2 cm Sprung, bis 30 % Knick auf 2 m.

### Beleuchtung und Jet-Fans (nur geschlossene Röhren)

Offene Galerien bleiben leer. Ein **geschlossener** Tunnel (`kind: tunnel` / `open_side: none`) mit Bohrloch länger als 100 m bekommt automatisch:

- Lampengehäuse in der **Röhrenmitte** (Decke, alle 12 m), plus `PointLight` ohne Schatten
- Jet-Fans als doppelwandiger Zylinder (0,60 m Durchmesser, 1,5 m lang) **über jeder Fahrspur**, alle 50 m

Die Zufahrtsplatte (`append_m`) bleibt ohne Ausstattung. Abstände und Schalter stehen in `beamng.galleries.defaults` und gelten pro Item:

| YAML | Default | Wirkung |
|------|---------|---------|
| `fitout` | `auto` | `auto` = geschlossen und ≥ `fitout_min_m`; `true` / `false` erzwingen |
| `fitout_min_m` | 100 | Mindestlänge des Bohrlochs (Portal zu Portal) |
| `lamp_spacing_m` / `lamp_inset_m` | 12 / 10 | Abstand; Abstand vom Portal |
| `fan_spacing_m` / `fan_inset_m` | 50 / 18 | seltener als Lampen; tiefer in der Röhre |
| `fan_rpm` | 60 | Schaulauf über UV-Rotation auf einer Impeller-Textur (statische TSStatics, gegenläufig über den Spuren). `0` = Schaufeln stehen. Echte Jet-Fans: oft aus, bei Bedarf ~1000–1500 U/min |
| `lamp_light` | true | `false` = nur Mesh, keine `PointLight` |

Es gibt kein Impeller-Stock-Asset; Gehäuse und Impeller-Scheibe liegen unter `art/shapes/galleries/tunnel_jetfan.dae` (plus `tunnel_jetfan_ccw.dae` für die Gegenrichtung). Die Schaufeln sind eine UV-Schleife, keine Mesh-Animation. Collision aus. Nach dem Inject **BeamNG ganz beenden**.

---

## Einendiger Tunnel mit Zufahrtsplatte

Muster **Landecker** (`config/sites/reschen.yaml`): Bohrloch und freie A12 sind zwei Unify. Die Platte ist eine verkürzte Fortsetzung der Approach-Achse, Gelände wie bei einer Brücke auf die Oberkante gezogen.

```yaml
beamng:
  roads:
    # Surface A12 oids: data/roads/gip_overrides.yaml (not_tunnel_objectids)
    unify:
      - id: landecker
        objectids: [227551, 235599]
        mode: mean_axis
        width_m: 7.5
        replaces: [galleries, decals, guardrails, road_bed]
      - id: landecker_approach
        objectids: [227576, 229085, 227552]
        mode: mean_axis
        width_m: 7.5
        kind: road
        replaces: [decals, road_bed]
  galleries:
    items:
      - match: { unify: landecker }
        one_ended: true
        fake_end_drop_m: 10
        append_unify: landecker_approach
        append_m: 40                 # nur so viele Meter vor der Tür
        abut_run_m: 5.0              # letzte Meter = Widerlager-Pad
        width_m: 7.5
        width_from_road: false
```

Z vom Portal bis zum Pad ist eine Gerade auf die Zielhöhe. Die geschlossene Röhre bleibt auf `portal_s`; nur das Deck läuft Portal → Pad.

`approach_conform_side_cut` senkt **leeres** Gelände neben der Platte um die Plattendicke (sonst bleibt auf 1-m-Zellen eine Stufe in Deckhöhe). Fahrbahnen, die dieselbe Ebene brauchen, müssen den Schnitt **blockieren** — nächster Abschnitt.

---

## Rampe / Abbiegespur neben einer Platte

Eine `S-AR` (und die Pendants `S-AP`, `S-BR`, `S-BP`) ist **eine Spur**, keine zweite volle Fahrbahn. Guardrails der Rampe aus; die Stammstrecke lässt an der Mündung eine Lücke (`junction_mouth_m`). Decal: durchgezogene **Randlinien**, keine gestrichelte Mittellinie (`autoLanes` aus).

Zwei getrennte Hebel, oft beide nötig:

### 1. Höhe der Rampe: `follow_parent`

Die Node-Z folgt der Eltern-Achse **entlang der Rampe vom Treffpunkt**, nicht als Kreis um den Knoten. Danach weicher Übergang zurück aufs DGM. Der Rest einer langen Rampe bleibt am Hang.

```yaml
beamng:
  roads:
    follow_parent:
      - match: { objectid: 230743 }
        parent: landecker_approach   # Unify-Id, sonst GIP-OBJECTID
        hold_m: 100
        blend_m: 30
        max_raise_m: 8
        max_cut_m: 8
        sink_m: 0.02
```

`parent` sucht zuerst `beamng.roads.unify[].id`, sonst eine OBJECTID. Der Treffpunkt muss näher als `join_max_m` (Default 40 m) an der Eltern-Polylinie liegen.

Danach:

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/reschen.yaml"; python tools\build_decal_roads.py
```

Road-Bed und Decal nutzen diese Node-Z. `snap_to_heightmap` ist für diese Stücke aus, damit eine alte Compose die Kopplung nicht zurückzieht.

### 2. Seitenschnitt nicht durch die Spur: `side_cut_preserve`

Die Galerie-/Brücken-Schicht (`span`, Priorität 55) kommt **nach** dem Road-Bed (40). Der Seitenschnitt neben der Platte gewinnt sonst gegen die Rampe, genau wo sie sich vom Deck löst. Weiter weg hat `span` keine Deckkraft — dort reicht oft `follow_parent` allein.

```yaml
beamng:
  roads:
    side_cut_preserve:
      objekt: [S-AR, S-AP, S-BR, S-BP]   # alle Stücke dieses Typs
      objectid: [230743]                  # zusätzliche einzelne OIDs
      pad_m: 0.5                          # optional, Default 0,5 m um die Achse
```

**Typ und OBJECTID sind Oder, die Listen werden vereinigt.** Ein Radweg (`S-FRW`) gehört nicht in `objekt`, wenn der Graben durch ihn durchgehen soll. Eine einzelne Fläche, die trotz Typ geschützt sein soll, kommt nach `objectid`.

Dasselbe Blockschema geht am Gallery- oder Brücken-Item (`side_cut_preserve` oder `approach_conform_side_cut_preserve`) und wird mit der Site-Liste vereinigt.

| YAML | Wirkung |
|------|---------|
| Schlüssel fehlt | Default: die vier untergeordneten Spurtypen |
| `objekt` + `objectid` gesetzt | genau diese Typen und OIDs (plus Item-Zugaben) |
| `side_cut_preserve: {}` | nichts geschützt, Seitenschnitt durch alles |

Unter der MeshRoad bleibt `force_deck_z` Eigentümer. Der Skip gilt für den seitlichen Abtrag **und** den kurzen Ansatz hinter dem Plattenende, nicht für die Platte selbst.

Logzeile nach dem Galerie-Build, zur Kontrolle:

```text
side_cut_preserve roads=19 objekt=S-AP,S-AR,S-BP,S-BR objectid=230743 skip_px=86
```

`skip_px=0` bei einer Rampe direkt an der Kante heißt: Match greift nicht (falsche OID, Typ, oder die Spur liegt noch unter der Platte).

Danach Galerie neu bauen (schreibt `span` und mixed die Heightmap):

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/reschen.yaml"; python tools\build_galleries.py --only 227551 --skip-holemap
```

Reihenfolge, wenn beides neu ist: zuerst Galerie (Span + Seitenschnitt), dann Decals/Road-Bed (`follow_parent`). Span überschreibt Road-Bed nur noch dort, wo kein Preserve-Streifen liegt.

---

## Was man nicht tun sollte

- `approach_conform: false` in Site-Defaults, weil „marry reicht“. Die Auflage ist der Default.
- Zwischen Auflagen Fels oder Asphalt aufs Gelände malen. Default ist `terrain_roof: none`; `rock` / `keep_asphalt` nur am Item.
- Bohrloch-Unify und Approach-Unify in **eine** Mittelachse legen.
- Die Platte mit `append_m` so lang lassen wie die ganze Zufahrt — nur die Meter vor der Tür.
- `force_deck_z_sink_m` auf Plattendicke (0,5 m) stellen. Das ist der **Seitenschnitt**, nicht die Fahrbahn. Auf der Platte bleiben 1–2 cm gegen Z-Fight.
- LocKarte vergrößern, um ein Portal „freizuschneiden“.
- `.ter` von Hand patchen. Quelle ist die Compose-PNG plus Import.
- World Editor speichern nach einem Inject.

Referenz-Site für das volle Muster: `config/sites/reschen.yaml` (Landecker, OID 230743).
