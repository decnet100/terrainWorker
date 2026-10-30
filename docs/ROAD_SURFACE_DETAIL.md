# Straßendetaillierung auf dem Fahrbahn-Mesh

Gedankenaufnahme, keine Implementierung.  
Die Frage: was davon ist auf den bestehenden Karten möglich und lastbar.

Verwandt: [ROADS.md](ROADS.md) (Decal, Road-Bed), [CONCEPT.md](CONCEPT.md) (Schichten Look / Physik), [STRUCTURES.md](STRUCTURES.md) (Collada-Hierarchie, periodische Einbauten), [BACKDROP.md](BACKDROP.md) (LOD-Namen, Vertex-Grenze), [GIP.md](GIP.md) (OBJECTID als Segment).

---

## Anlass

Die Fahrbahn soll nicht nur ein geglättetes Profil sein. Auf dem Road-Mesh (Collada) lassen sich Unebenheiten **lokal** aufbringen, auch sehr feingliedrig. Pro GIP-Segment soll ein Rezept gelten, zum Beispiel:

> Segment 123: wenige Risse, mittlere Ausbesserung, wenig Bitumen. Gullideckel alle 100 m, ein Viehgatter bei Station 22,8 m.

Aus dem Rezept entstehen **zwei** Meshes, die zusammenpassen:

- Collision-Mesh — worauf das Fahrzeug steht
- visuelles Mesh — was man sieht, inklusive der feinen Abnutzung

Dazu Stufen der Detailreduktion (LOD), damit 8192-Karten nicht an der Fahrbahn ersticken.

---

## Stand in diesem Checkout

In `master` liegt die Road-Mesh-Pipeline **noch nicht**. Die befahrene Fläche ist heute:

| Schicht | Rolle |
|---------|--------|
| Heightmap + Terrain-Asphalt | Querneigung grob, Grip über Groundmodel `ASPHALT` |
| DecalRoad | Optik und Markierung; oft ohne eigenes Gefühl |
| MeshRoad | Platten von Brücke / Galerie / Tunnel, Node-Z = Oberkante |
| Gallery-DAE | sichtbares LOD `_a999` plus `Colmesh-1` — **dieselben** Dreiecke, Kommentar im Code: Collision später vereinfachen |

Zusätzlich (lokal, noch nicht im Remote): DAE-Fahrbahnstücke mit **gut geglättetem, im Querschnitt detailliertem Profil**. Collision-tauglich, **ohne** eigenes visuelles Mesh. Das ist die Fläche, auf die diese Idee aufsetzt — nicht das 1-m-Terrain und nicht die DecalRoad.

Quelle ist das **vorbearbeitete, geglättete 0,5-m-DGM**. Daraus wird das Road-Mesh abgeleitet (Profil, Oberkante). Diese Fläche — plus eine **passende Lücke unter der Spur**, damit Gelände und Mesh nicht doppelt tragen — geht später auf das Terrain. Das fertige Mesh ist bewusst **superglatt**. Sicht behält diese Geometrie. Collision wird mit **enger Winkeltoleranz** ausgedünnt (lange Gerade sollen zusammenfallen). Erst auf diesem glatten Ausgangspunkt liegen Risse, Flecken, Bitumen, Gullideckel und Viehgatter — sichtbar und, wo fühlbar, physisch.

[CONCEPT.md](CONCEPT.md) hält fest: Makrounruhe aus dem DGM, keine vollflächige Hochpoly-Körnung. Das bleibt die Lastgrenze für die **Einbauten**. Das 0,5-m-Profil selbst ist die Fahrbahn, nicht die Abnutzung.

---

## Was die Engine schon kann

Die Bausteine existieren in anderen Schritten. Neu wäre nur, sie auf die Fahrbahn anzuwenden.

**Sicht und Collision getrennt.** `build_galleries.write_collada` und `build_buildings` schreiben dieselbe Hierarchie, die BeamNG für `collisionType: Collision Mesh` erwartet:

```text
base00
  start01
    <stem>_a999          sichtbares LOD
  collision-1
    Colmesh-1            Physik
```

Heute teilen beide Geometrien die Vertices. Unterschiedliche Dreiecke in denselben Knoten sind vorgesehen und unbenutzt.

**LOD über den Knotennamen.** BeamNG liest die Pixel-Schwelle aus `_a<Zahl>`. Buchstabe vor der Zahl ist Pflicht. Ziffern **im Stem** werden als LOD-Größe 0 gelesen (Backdrop: `backdrop_mid_00_a999` war unsichtbar). Sichtbares Fern-LOD heißt hier `_a999`. Ein näheres LOD wäre z. B. `_a80` (nur bei großem Bildschirmanteil).

**Wiederholte Körper als Katalog.** Häuser und Leitplanken-Sektionen teilen sich wenige DAEs; jedes TSStatic setzt nur Lage, Gierwinkel und Maßstab. Gullideckel und Viehgatter gehören in genau dieses Muster — eine Form, viele Setzungen.

**Station auf der Achse.** Tunnel-Ausstattung setzt bereits Körper auf die Bogenlänge: Lampen alle 12 m, Jet-Fans alle 50 m (`STRUCTURES.md`). Dieselbe Station `s` trägt Gullideckel alle 100 m und ein Viehgatter bei 22,8 m.

**Rezept pro OBJECTID.** Breiten, Kunstbauten und `follow_parent` matchen schon `match: { objectid: … }`. Ein Abnutzungsrezept ist derselbe Hebel, kein neues Objektmodell.

**Harte Grenze: 65 535 Vertices** je Collada-Geometrie (16-Bit-Index). Backdrop und Walddach zerlegen daran. Ein feines Fahrbahn-LOD muss kacheln, bevor es diese Zahl erreicht.

---

## Vorgehen zur Erstellung

Kein gebauter Schritt. Gemeinte Reihenfolge:

```text
vorbearbeitetes, geglättetes 0,5-m-DGM
    → Road-Mesh ableiten (Profil, Oberkante, bewusst superglatt)
    → später aufs Terrain: dieselbe Fläche
         + Lücke unter der Spur (keine Doppel-Collision)
    → Sicht  = dieses Mesh, unverändert in der Dichte
    → Collision = dasselbe Mesh, ausgedünnt mit enger Winkeltoleranz
    → lokale Effekte auf den fertigen glatten Ausgangspunkt
         sichtbar auf dem Sicht-Mesh
         physisch auf der Collision, wo der Reifen es merken soll
```

### Ableitung aus dem 0,5-m-DGM

Die 0,5 m sind die **DGM-Zellenweite** und damit der mittlere Knotenabstand der abgeleiteten Fläche, nicht eine nachträglich verdichtete Fahrbahn. Vorbearbeitung und Glättung nehmen die Roh-Zacken des Geländemodells weg, bevor das Mesh entsteht. Querschnitt (Kuppe, Querneigung) bleibt erhalten — das ist das „im Profil detailliert“, nicht die Abnutzung.

### Lücke unter der Spur

Sobald das Fahrzeug auf der DAE steht, darf das Gelände darunter nicht mittragen. Heute macht das Road-Bed das Gegenteil: die Heightmap **ist** die Fahrbahn ([ROADS.md](ROADS.md)). Das neue Modell senkt oder nimmt die Terrain-Collision unter der Spur weg und setzt die Schulter an die Mesh-Kante, damit kein Absatz entsteht.

`theTerrain_holemap.png` ist dafür das falsche Werkzeug auf der freien Strecke. Weiß löscht die ganze XY-Säule — bei Galerien steht das als Himmelsschacht, deshalb wird dort die Heightmap geschnitten statt gelocht (`build_galleries`: LocKarte kann kein Höhenband). Unter der offenen Spur heißt die Lücke: Gelände unter der Platte tiefer als das Mesh (Einsenken, nicht Portal-Loch), Schulter bündig. Zwischen Widerlagern bleibt das Gelände in der Spanne unangetastet ([struktur-spanne-gelaende](../.cursor/rules/struktur-spanne-gelaende.mdc)); die Lücke gilt für die **freie** DAE-Spur, nicht als Auffüllen einer Schlucht.

Dieser Transfer aufs Terrain kommt **später**, nach dem Mesh. Die Abnutzung wartet nicht darauf.

### Sicht bleibt, Collision mit enger Winkeltoleranz

| Zweig | Geometrie | Zweck |
|-------|-----------|--------|
| Sicht | abgeleitetes 0,5-m-Mesh, Dichte behalten | Fläche, auf die optische Inseln kommen |
| Collision | dieselbe Fläche, **ausgedünnt** | worauf das Fahrzeug steht |

Ausdünnen mit **enger Winkeltoleranz**: Knoten fallen weg, wo die Nachbarflächen fast komplanar sind. Auf langen Geraden (Pass, Tal) sollen viele 0,5-m-Knoten zu großen Vierecken werden. Kuppe, Querneigung und Kurve bleiben, wenn ihr Knickwinkel über der Schwelle liegt. Zu weit, und die Kuppe wird eine Ebene. Zu eng, und die Gerade behält unnötige Knoten.

Das ist der erhoffte Gewinn gegenüber einem festen Längsschritt: die Collision wird dort billig, wo die Fläche wirklich flach ist.

### Lokale Effekte auf den glatten Ausgangspunkt

Erst wenn dieses Mesh fertig und glatt ist, kommen die Rezept-Inseln. Gemeinsame Herkunft, zwei Auftragungen:

- **sichtbar** auf dem behaltenen 0,5-m-Mesh (Risse, Schlangen, dichte Fleckränder, aufgesetzte Katalogteile)
- **physisch** nur was der Reifen merken soll (Fleck-Insel, Deckel-Lippe, Viehgatter)

Sie ersetzen das 0,5-m-Gitter nicht und verdichten die ganze Spur nicht. Der glatte Ausgangspunkt bleibt die Referenz-Oberkante (`z_road`).

Zwei Wege, die physische Auftragung zu halten:

1. **Nach** dem Ausdünnen auf das `Colmesh-1` setzen — Lippe und Stäbe kann der Vereinfacher nicht als Rauschen der Fläche lesen.
2. **Vorher** in das glatte Mesh legen und die neuen Kanten sperren, oder die Winkelschwelle enger halten als diese Kante. Ein Fleck von 2 cm auf 1 m ist etwa **1°**. Eine Schwelle darüber nimmt ihn mit, wenn er nicht gesperrt ist.

Katalogkörper (Gullideckel, Viehgatter) gehen nicht durch den Flächen-Vereinfacher. Risse und dünne Schlangen bleiben Sicht: schmaler als 0,5 m, nach dem Ausdünnen erst recht.

Was das 0,5-m-Profil selbst trägt — und was nicht:

| Merkmal | Auf dem glatten 0,5-m-Mesh | Nach Winkel-Ausdünnen | Folgerung |
|---------|----------------------------|------------------------|-----------|
| Längs- und Querprofil, Kuppe, Querneigung | ja | ja, auf Geraden grober | das ist die Fahrbahn |
| Ausbesserungsfleck ≥ etwa 1 m, 1–2 cm | ein paar verschobene Knoten | oft weg (≈ 1°) | Insel **auftragen**, nicht nur Knoten schieben |
| Gullideckel (≈ 0,6 m) | ein Knoten, keine Lippe | weg | Katalogkörper auftragen |
| Viehgatter-Stäbe | nicht abbildbar | — | Katalogkörper auftragen |
| Asphaltriss, dünne Bitumenschlange | nicht abbildbar | — | nur Sicht |

Der Reifenlatsch liegt bei etwa 0,2 m. Sub-halbmeter-Gefühl kommt aus den **aufgetragenen** Körpern.

---

## Drei Klassen von Elementen

Nicht jedes Detail gehört ins Collision-Mesh. Der Reifenkontakt liegt in der Größenordnung von etwa 20 cm. Was kleiner und flacher ist, sieht man — man fährt es nicht.

| Klasse | Beispiele | Sicht | Collision | Erzeugung |
|--------|-----------|-------|-----------|-----------|
| Abnutzung, zufällig | Asphaltrisse, Ausbesserungsflecken, Bitumenschlangen | ja, auf dem glatten 0,5-m-Mesh | Flecken: physisch auftragen, wenn Höhe ≥ etwa 1–2 cm und Fläche in Reifengröße. Risse und dünne Schlangen: nie | Dichte aus dem Segment-Rezept, fester Zufallsstart |
| Wiederkehrend | Gullideckel alle 100 m | ja | flache Lippe reicht | Raster auf `s`, Querversatz fest oder leicht streuend |
| Einmalig / gesetzt | Viehgatter bei 22,8 m | ja | **muss** die Stäbe und Lücken haben | genaue Station, Katalog-DAE |

### Asphaltrisse

Als Geometrie auf der ganzen Spur: ungeeignet. Ein Riss ist zentimeterbreit; die Basisfläche müsste überall so fein sein. Sichtbar als lokale Streifen im Near-LOD, als Normal-/Albedo-Karte oder als Decal. Nicht ins `Colmesh-1`.

### Ausbesserungsflecken

Flächig, oft 1–3 cm über oder unter der Nachbarfläche. Das ist die Abnutzung, die man **fühlen** kann, wenn der Fleck größer ist als der Reifenlatsch. Collision: Insel auf den glatten Ausgangspunkt auftragen (nach dem Ausdünnen oder mit gesperrter Kante), nicht nur einen 0,5-m-Knoten schieben. Sicht: dichtere Umrandung, anderes Material.

### Bitumenschlangen

Schmale, organische Wülste. Teuer als echte Verdrängung der ganzen Fläche. Besser: erzeugte Bänder nur im visuellen Near-LOD, oder Decal. Collision nur, wenn die Schlange bewusst als Schwelle dienen soll (dann als eigenes Band, nicht als 5-cm-Gitter).

### Gullideckel

Katalog-DAE wie ein Leitplankenstück. Ein Mesh, viele TSStatics. Collision: Ring plus leichtes Einsenken, nicht die Gitterrippen. Alle 100 m auf 15 km Stammstrecke sind etwa 150 Setzungen — gegenüber einzigartigen Hochpoly-Kacheln vernachlässigbar.

### Viehgatter

Einziges Abnutzungsobjekt, das **als Körper** in der Collision stehen muss. Stäbe und Lücken, sonst fährt man über eine glatte Platte. Eine Katalog-DAE, auf das geglättete Profil gesetzt (Lage, Gierwinkel, Breite der Spur). Nicht in die Basisfläche einschneiden, wenn der Körper selbst trägt.

---

## Rezept und Erzeugung

Die GIP-`OBJECTID` ist das Segment. Station `s` läuft in Metern entlang der schon gebauten Achse (`gip_roads_beamng.json` / Road-Mesh-Centerline).

Gedachtes YAML — nur zur Vorstellung, kein gebauter Schlüssel:

```yaml
beamng:
  road_detail:
    defaults:
      cracks: low          # none | low | medium | high
      patches: medium
      bitumen: low
      manhole_every_m: 100
    items:
      - match: { objectid: 123 }
        cracks: low
        patches: medium
        bitumen: low
        features:
          - { kind: cattle_grid, s: 22.8 }
```

**Zufall ist nicht frei.** Dichte und Startwert kommen aus OBJECTID plus Site-Name, damit ein erneuter Lauf dieselben Risse und Flecken legt. Neu würfeln nur nach bewusstem Wechsel des Startwerts.

**Einmalige Lage schlägt das Raster.** Ein Viehgatter bei 22,8 m verdrängt dort den Gullideckel und schneidet ein Stück Abnutzung aus, damit nichts in den Stäben sitzt.

**Orthofoto ist Referenz, nicht Textur.** Lizenz wie in [CONCEPT.md](CONCEPT.md). Dichten können von Hand oder später aus einer Klassifikation kommen; die Körper erzeugt die Pipeline.

---

## Zwei Meshes aus einem Rezept

„Optisch darauf passend“ heißt: dieselbe Oberkante, dieselben fühlbaren Körper an derselben Station. Es heißt **nicht**: dieselbe Dreieckszahl.

```text
glattes Road-Mesh aus 0,5-m-DGM (Ausgang)
        │
        ├─ Sicht: Dichte behalten
        │     darauf: Risse, Bitumen, dichte Patch-Ränder, Katalogteile
        │     LOD nur für die Inseln (fern: glattes Profil, _a999)
        │
        └─ Collision: enge Winkeltoleranz (Gerade fallen zusammen)
              darauf physisch: Viehgatter, Deckel-Lippe, Patch-Inseln
              ohne Risse, ohne feine Bitumen-Wülste
```

Die Oberkante bleibt `z_road` ([fahrbahn-oberkante](../.cursor/rules/fahrbahn-oberkante.mdc)). Verdrängung liegt relativ dazu, nach oben oder unten. Die Platte hängt nach unten; Details sitzen auf der Fahrfläche.

Wicklung und harte Kanten wie bei jedem neuen Mesh ([mesh-faces](../.cursor/rules/mesh-faces.mdc)): Normale nach außen, keine geteilten Vertices ohne eigene Normale, keine zweite Wicklung als Opacity-Trick.

---

## Last auf den Karten

Größenordnung, nicht Laborwert. Stammstrecke durch eine 8192-Karte: grob 10–20 km, plus Seitenäste. Reifenlatsch ≈ 0,2 m. Vertex-Deckel 65 535 je Geometrie.

### Was die Karte trägt

**Sicht behält 0,5 m, Collision wird winklig ausgedünnt.** 15 km × 8 m bei 0,5 m sind grob 5·10⁵ Vertices — über dem Collada-Deckel, die Sicht-Spur muss kacheln, nicht ausdünnen. Collision: dieselbe Fläche, enge Winkeltoleranz. Lange Gerade sollen stark schrumpfen; Kuppe und Kurve bleiben. Einbauten zählen extra und bleiben lokal.

**Vollfläche 5 cm.** 15 km × 8 m / 0,05² ≈ 5·10⁷ Quads. Ein Mesh. Unbrauchbar, auch zerlegt: Speicher, Build und Physik.

**Near-LOD nur in Abnutzungsinseln.** Riss als Streifen (einige Dutzend Dreiecke), Fleck als Insel, Bitumen als Band. 50 Inseln je km bleiben im Rahmen, wenn sie im Near-LOD sterben und nicht in die Collision wandern.

**Katalog-Körper.** Gullideckel und Viehgatter: eine DAE, viele TSStatics — dasselbe Muster wie Leitplanke und Hauskatalog. Die Last ist die Zahl der Objekte in der Nähe, nicht die Zahl der eindeutigen Dateien.

### LOD, ohne das die 8192-Karten kippen

| Distanz (Größenordnung) | Sicht | Collision |
|-------------------------|-------|-----------|
| nah (groß auf dem Schirm, `_a80` o. ä.) | 0,5-m-Profil + Inseln + Katalogteile | ausgedünntes Profil + aufgetragene Körper |
| mittel | 0,5-m-Profil + Flecken als Farbe, keine Riss-Geometrie | unverändert |
| fern (`_a999`) | 0,5-m-Profil ohne Insel-Geometrie | unverändert |

Collision wechselt **nicht** mit dem LOD. Sonst ändert sich das Fahrgefühl, wenn das Mesh wechselt.

Kacheln: 50–100 m je visuellem Stück, Stem ohne Ziffern (`rd_aa_a80`, nicht `rd_00_a80`). GIP-Segmente sind oft länger als eine Kachel; die OBJECTID bleibt das Rezept, die Kachel nur die Dateigrenze.

### Nach Kartengröße

| Site | Terrain | Einschätzung |
|------|---------|--------------|
| Testarena 512 | eine Struktur, kurze Spur | Rezept und beide Meshes hier zuerst prüfen |
| L13 / Fernpass 2048 | wenige Kilometer Stammstrecke | Near-LOD auf der ganzen benannten Spur vertretbar |
| Fernpass Mega, Reschen, Imst 8192 | Stamm plus GIP im Kartenausschnitt | nur mit Kacheln, LOD und Katalog; keine vollflächige Feintessellation |
| Forst- / Wirtschaftswege (`S-F`, `S-GW`) | Terrain-Kies, kein Road-Bed | außerhalb dieser Idee; bleiben Heightmap |

Seitenäste auf 8192 nicht automatisch mit Insel-LOD versehen. Default: glattes 0,5-m-Sichtmesh und winklig ausgedünnte Collision, Abnutzung nur auf benannter Stammstrecke (`gip_decals: named` / `STR_CODE`).

---

## Drei Wege, ein Detail zu legen

Nur zur Einordnung. Nichts davon ist gebaut.

| Weg | Wofür | Last | Gefühl |
|-----|-------|------|--------|
| **A — Decal / Material** | Risse und Bitumen, die man nur sieht | niedrig | keines |
| **B — Katalog-TSStatic** | Gullideckel, Viehgatter | niedrig bei Wiederholung | ja, eigenes `Colmesh-1` |
| **C — Insel auf dem ausgedünnten Colmesh** | Flecken mit Höhe, optisch bündig | mittel, wenn lokal | ja, wenn die Insel **nach** dem Ausdünnen angefügt wird |

Empfehlung: **A** für Risse und die meisten Schlangen, **B** für Deckel und Gatter, **C** nur für fühlbare Flecken. Das 0,5-m-Profil nicht vollflächig feiner legen, um C zu füttern.

DecalRoads für **Markierung** können auf dem Road-Mesh bleiben, wenn das TSStatic `decalType: Collision Mesh` hat (wie Gallery-Deck und Leitplanke). Abnutzung nicht als zweite DecalRoad-Lage über die ganze Karte — das ist das heutige Decal-Modell, nicht das Mesh-Rezept.

---

## Physik, sobald die DAE trägt

Heute kommt der Grip vom Terrain-Groundmodel unter der DecalRoad. Fährt das Fahrzeug auf dem Road-Mesh, gilt das Material der Collision-Geometrie, nicht mehr die Asphalt-Maske.

Das ist ein Schnitt, kein Nebeneffekt:

- Fahrbahn-DAE braucht ein Groundmodel (`ASPHALT` oder eine rauere Variante für starken Flickenteppich).
- Unter der Spur die **Lücke** (Gelände tiefer als das Mesh). Heutiges Road-Bed wäre das Gegenteil. Schulter an die Mesh-Kante, kein Absatz.
- MeshRoad-Decks von Brücke und Galerie bleiben vorerst eigene Körper. Dieselbe Rezeptlogik kann später auf die Platte, ist aber nicht der erste Schritt.
- Zwischen den Widerlagern gilt weiter: Gelände unangetastet ([struktur-spanne-gelaende](../.cursor/rules/struktur-spanne-gelaende.mdc)). Abnutzung sitzt auf der Platte oder der freien DAE-Spur, nicht in der Heightmap.

---

## Was sich lohnt — und was nicht

**Möglich und zur Kartengröße passend**

- Rezept pro GIP-OBJECTID (Risse / Flecken / Bitumen als Stufe).
- Fester Zufall für Inseln, Rebuild bleibt gleich.
- Gullideckel im Raster, Viehgatter auf genauer Station.
- Sicht = behaltenes 0,5-m-Mesh; Collision = dieselbe Fläche, enge Winkeltoleranz (Gerade fallen zusammen).
- Lokale Effekte erst auf den fertigen glatten Ausgangspunkt, sichtbar und — wo fühlbar — physisch.
- Katalog-DAE für wiederholte Körper (Muster Gebäude / Leitplanke).
- Kacheln vor 65 535 Vertices, LOD-Namen ohne Ziffern im Stem.

**Möglich, aber die 8192-Karten nicht wert**

- Risse oder Bitumen in die Collision legen, oder das 0,5-m-Profil vollflächig feiner tessellieren und erst danach ausdünnen.
- Ein einziges Hochpoly-Mesh je Kilometer ohne LOD.
- Ein TSStatic je Riss (zu viele einzelne Zeichenaufrufe).
- Feines Gitter unter Tunnel- und Galerie-Bohrloch in der Heightmap.

**Noch nicht in diesem Repo, aber kein Widerspruch zur Engine**

- Zweites sichtbares LOD neben `_a999` an einem Fahrbahn-TSStatic (Backdrop und Galerie nutzen bisher ein LOD).
- Groundmodel am Fahrbahn-Material statt an der Terrain-Maske.
- Die unveröffentlichte Profil-DAE (aus geglättetem 0,5-m-DGM) als verbindliche Unterlage.
- Lücke unter der freien Spur auf dem Terrain, ohne LocKarte (kein Höhenband).

**Erste sinnvolle Probe** (wenn gebaut werden soll): Testarena, ein Segment, `patches: medium`, ein Viehgatter auf `s`, Gullideckel im Raster. Collision zum Profil vergleichen, Near-LOD von der Seite ansehen, dann erst Fernpass.

---

## Einordnung in die Pipeline

Kein neuer Schritt in `pipeline_catalog.py`. Reihenfolge, sobald die Road-Mesh-Pipeline im Remote liegt:

```text
geglättetes 0,5-m-DGM → glattes Road-Mesh
    → Sicht behalten; Collision winklig ausdünnen
    → Rezept: Inseln auf den glatten Ausgangspunkt (sichtbar + physisch)
    → später: Fläche + Lücke unter der Spur aufs Terrain
    → TSStatic in die Level-JSON schreiben
```

Annotierte Punkte (Gullideckel, Gatter) können später in der GeoPackage-Schicht liegen, analog zu `guardrail`. Das Rezept selbst bleibt YAML pro OBJECTID.

Lua und injizierte DAEs bleiben über Freeroam-Reload im Speicher. Nach Mesh-Änderungen BeamNG beenden, nicht nur die Karte neu laden ([beamng-neustart](../.cursor/rules/beamng-neustart.mdc)).
