# GIP → Magliasina-Straßenpipeline: Was fehlt noch?

Stand 28.10.2026. Ausschnitt Pfunds–Spiss (`config/sites/pfunds_spiess.yaml`, EPSG:25832, 41 % in der Schweiz).
Gelesen: terrainWorker `origin/junction-node-heights` (enthält `Hillclimb`), magliasinaNG `master`.
Geprüft mit GIP-OGD 2026-07 (`gip_network_ogd.gpkg`, `gip_reference_ogd.gpkg`), swissTLM3D 2026-02,
swissBOUNDARIES3D 2026-01, AV Graubünden (geodienste.ch, LCSF). Skripte: `tools/review/`, Daten: `data/review/` (lokal, nicht im Repo; Pfad über `REVIEW_DATA`).

## 1. Kurzfassung

| Baustein | Was Magliasina erwartet | GIP-Netz (LINK) | terrainWorker heute | Status |
|---|---|---|---|---|
| Achsen an Kreuzungen geteilt | gemeinsame Endpunkte, Fang 0,6 m (`network.SNAP`) | ✅ 0 ungenodete T-Kreuzungen, 11 Enden < 1 m auseinander | Routing-Links genutzt | ✅ (Tirol-WFS „Verkehrswege“: 106 ungenodete T-Kreuzungen → nicht verwenden) |
| Höhenfreie Kreuzungen getrennt | Linien kreuzen ohne Knoten | ✅ alle 9 Innenkreuzungen haben verschiedenes `level_intermediate` | `grade_clip_pairs` | ✅ |
| Klasse → Nennbreite, Glättungslänge | `OBJEKTART` aus `network.CLASSES` (TLM-Namen) | `functional_class`, `form_of_way`, `edge_category` | – | ❌ Mapping fehlt |
| Breite | gemessen in Fahrbahnpolygonen, sonst Nennbreite | ✅ `LINEAR_USE` Typ 1 „Fahrbahn“ für 100 % der Links, Median 5,0 m | `WIDTH_AVERAGE` genutzt | ✅ als Rückfall |
| Brücken/Tunnel/Galerien als eigene Linien | `KUNSTBAUTE` pro Linie; Tunnel/Galerie werden verworfen, Brücke = Deck | ⚠ 57 BRUNNEL-Stücke, aber Enden liegen meist **mitten im Link** (Median 28 m vom nächsten Linkende) | `score_check_grade.py` + `score_gap_chord.py` bestimmen die Bauwerksbereiche am DGM; Ausgabe als geteilte Links ist geplant, aber noch nicht umgesetzt | ⚠ unter Annahme der geplanten Ausgabe weitgehend gelöst, siehe 2a |
| Achs-Z (Brückendecks, Stege, Durchlässe) | 3D-Achse (TLM), NaN erlaubt; das Deck rechnet Magliasina selbst als glatte Kurve zwischen den Rampen | ❌ GIP ist 2D | – | ✅ nicht nötig, NaN reicht (Ausnahmen siehe 2a) |
| Fahrbahnflächen | reine Fahrbahn (`strada_sentiero`), lückenlose Partition mit gemeinsamen Kanten, Achse ≤ 1,5 m von Flächenmitte | – | Flächen pro **Kette**, bei gleichem Rang überlappend, keine Ebenentrennung, Kreuzungen teils Löcher | ❌ Hauptlücke |
| Gehsteig/Insel/Hartflächen | eigene Klassen `marciapiede`, `spartitraffico`, `altro_rivestimento_duro` | `LINEAR_USE` Typ 7 Gehweg, 5 Verkehrsinsel | – | optional |
| Staatsgrenze | – (Malcantone ist einseitig) | GIP enthält 28,5 km in der Schweiz | Routing-Pfad schneidet nicht an der Grenze | ❌ siehe Abschnitt 3 |
| Koordinaten/Höhen | lokal aus LV95/LN02, Schweiz-Hardcodings | – | 25832, Tirol GHA | ❌ Adapter, siehe Abschnitt 4 |

**Kernaussage:** Das GIP-*Routingnetz* (`LINK` aus `gip_network_ogd.gpkg` bzw. IDF) erfüllt die Topologie-Anforderungen schon gut.
Was fehlt, sind (a) die Fahrbahnflächen als Partition, (b) die Ausgabe der am DGM bestimmten Bauwerksbereiche als geteilte Links mit Typ
(Verfahren steht in `score_check_grade.py`/`score_gap_chord.py`, Ausgabe noch nicht umgesetzt),
(c) ein Klassen-Mapping, (d) der Grenzschnitt Österreich/Schweiz und (e) der Koordinaten-Adapter auf Magliasina-Seite.
Die GIP-Link-ID auf der Fläche braucht Magliasina nicht (Zuordnung Fläche↔Achse ist rein geometrisch, ≤ 1 m),
sie ist aber als Kontrolle nützlich.

## 2. Was die Fahrbahnflächen erfüllen müssen

Aus `network.measure_width`, `network_mesh`, `smooth_geom` (magliasinaNG):

1. **Nur Fahrbahn.** Breite wird von der Achse bis zum Rand der Vereinigung aller getroffenen `strada_sentiero`-Polygone gemessen.
   Gehsteige, Parkstreifen, Bankett in derselben Klasse → Breiten zu groß.
2. **Plausibilitätsfenster pro Station (2 m):** 0,5·Nennbreite < w < max(2,2·Nennbreite, 6 m) und |Versatz Achse→Mitte| < 1,5 m.
   Außerhalb gilt die Nennbreite der Klasse. → Die Achse muss nach eurer Lateralkorrektur **in** der Fläche liegen.
3. **Partition:** benachbarte Flächen teilen Kanten und Stützpunkte (Fang 2 mm), sonst rundet `smooth_geom` beide Seiten verschieden und es entstehen Spalten.
   Keine Überlappungen auf derselben Ebene. Kreuzungsflächen gehören zur Fläche (keine Löcher).
4. **Brücken/Tunnel:** Unterführungen nicht unter Brücken wegschneiden. Bei Tunneln/Galerien liefert Magliasina keine Fahrbahn
   (`SKIP`), die baut terrainWorker selbst (`build_galleries.py`).
5. Optional SOSF `ponte_passerella` (Brückenumriss) für breite Decks.

**Heute in terrainWorker (Branch `junction-node-heights`):**
- `build_link_surfaces.py` erzeugt Flächen pro Kette mit `short_ids`; über Ränge hinweg überlappungsfrei, innerhalb eines Rangs nicht; Kreuzungs-Patches zusätzlich.
- `build_flat_shelf.py` ist ausdrücklich Inspektion („not a drivable network“), Kreuzungen leer.
- `narrow_regional.py`, `shift_fitted_again.py`, `shift_narrow_iter.py` sind verworfen (Commit d1e5e50).
- `unify_carriageway_mesh_clip.py` (Imst-Pfad) macht überlappungsfrei, verliert aber die Zuordnung.

**Fehlender Schritt („Partition bauen“), Vorschlag:**
1. Pro Link ein Band (Achse nach Lateralkorrektur, Breite aus Messung bzw. `width_average`), getrennt nach Ebene (`level_intermediate`, BRUNNEL).
2. Pro Ebene: Bänder vereinigen → eine Fläche je Ebene; Kreuzungen mit der Ecken-Rundung aus `build_junction_meshes._close_local`.
3. Die Fläche in Zellen pro Link zerlegen (Voronoi der Achsen, auf die Fläche geschnitten) → Partition mit gemeinsamen Kanten, jede Zelle trägt `link_id`.
4. Prüfen: keine Überlappung pro Ebene, keine Löcher < x m², jede Achse ≤ 1,5 m von der Mitte. (Ein Prüfskript für Polygone gibt es noch nicht; `check_road_mesh.py` prüft nur DAE.)

## 2a. Bauwerke aus der DGM-Prüfung (score_check_grade)

**Annahme** (laut Autor geplant, Ausgabe noch nicht umgesetzt): Die Links werden geometrisch in Abschnitte Straße / Brücke / Tunnel-Galerie
geteilt, die Grenzen kommen aus der DGM-Prüfung statt aus den GIP-Prozentangaben.

**Was das Verfahren heute schon liefert** (`junction-node-heights`):
- `score_check_grade.py`: Intervalle à 2,5 m pro Link (Mitte + ±1,5 m), gültig wenn das Gefälle zur Straße passt; Bauwerks-Labels aus `bridge_ref`
  werden nur auf ihrem Stationsbereich ausgenommen; ein Spline durch die gültigen Gruppen links und rechts holt Intervalle zurück,
  die doch auf dem Gelände liegen (Label zu lang). Ausgabe `check_grade.gpkg` (`interval`, `todo`, `link`, `valid_group`).
- `score_gap_chord.py`: pro Lücke zwischen zwei gültigen Gruppen DGM minus Sehne bzw. Kubik, **mit Vorzeichen**
  (negativ = Gelände unter der Straße → Öffnung/Brücke; positiv = Gelände über der Straße → Tunnel/Galerie/Einschnitt).

**Damit gelöst bzw. lösbar:**
- Teilen der Links an Bauwerksgrenzen (Hauptproblem der GIP-Angaben, Enden median 28 m daneben). Genauigkeit der Grenzen: Intervallraster 2,5 m.
- Durchlass vs. Brücke: über die Tiefe der Senke unter der Sehne (Magliasina: `DIP` 1,5 m).

**Achs-Z wird nicht gebraucht.** Die Kubik aus `score_check_grade`/`score_gap_chord` ist nur eine einfache Interpolation zwischen den Gruppen,
also ein Prüfmaßstab, keine Information über das echte Deck. Magliasina macht für Brücken dasselbe selbst: Auf Brückenstationen bekommt das DTM
das Gewicht 0, und das Deck ergibt sich als glatte Kurve zwischen den Rampen (`network_surface.solve`). `z_tlm` wird nur gelesen
(network_surface.py Z. 172–212) für
- die Durchlass-Erkennung (TLM-Linie liegt auf dem Gelände): mit NaN entscheidet allein die Senkentiefe (`DIP`), das passt;
- Brücken mit einem freien Ende (Sackgasse, Gebietsrand) als schwacher Zug aufs Deck (`W_DECK`): ohne Z ist deren Neigung frei → am Gebietsrand Brücken nicht abschneiden;
- Netzstücke ganz ohne DTM-Anbindung (freistehender Steg): fallen ohne Z aufs DTM zurück.
Für Tunnel/Galerie ist Z ohnehin egal, Magliasina verwirft sie.

**Was für die Übergabe an Magliasina noch nötig ist:**
1. **Teilstücke als echte Knoten:** Der Teilungspunkt muss Endpunkt beider Stücke sein (gleiche Koordinate), sonst entsteht ein offenes Ende.
   Neue Stücke brauchen eine eigene ID (z. B. `link_id#n`) für `UUID`/`ponti.json`.
2. **Typ pro Stück:** `KUNSTBAUTE` = `Bruecke` / `Tunnel` / `Galerie` / `Keine`. Brücke vs. Tunnel kommt aus dem Vorzeichen von `score_gap_chord`.
   Tunnel vs. Galerie geht am DGM nicht (beides „Gelände drüber“); dafür GIP/BRUNNEL-Typ oder DOM/Orthofoto (offene Talseite). Für Magliasina egal
   (beides wird verworfen), für `build_galleries.py` nicht.
3. **Kein Z nötig** (siehe oben); Z = NaN ausgeben.
4. **Vorzeichen auch im Spline-Recover:** `_end_against_spline` rechnet `abs(height - predicted)`, das Vorzeichen geht dort verloren
   (score_check_grade.py, `_end_against_spline`). Für die Typzuordnung entweder dort behalten oder aus `score_gap_chord` übernehmen.
5. **Nur Enden geprüft:** Recover testet bei einem gelabelten Intervall nur die Höhen an `s0`/`s1` gegen den Spline, nicht die Mitte.
   Bei 2,5-m-Intervallen unkritisch; bei längeren gelabelten Stücken (ganzer Link aus der manuellen Liste) die Mitte mitprüfen.
6. **Grenzfall „plausibles Gelände über einem Tunnel“:** Läuft ein nicht verzeichneter Tunnel unter flachem, straßentauglichem Gelände,
   besteht die Gefälleprüfung und das Stück gilt als Straße. Genau das passiert mit der GIP-Geometrie der Samnaunerstraße
   (offene Kurve statt Kehrtunnel). Abhilfe: in der Schweiz TLM nehmen (Abschnitt 3); in Österreich BRUNNEL aus `gip_reference_ogd.gpkg`
   als zweite Quelle gegenprüfen (57 Bauwerke im Ausschnitt; Abgleich mit `bridge_ref` aus dem Routing-Export nicht geprüft),
   und zusätzlich die Querrichtung prüfen: eine Straße hat quer eine ebene Plattform mit Böschungskanten, ein beliebiger flacher Hang nicht
   (`sample_cross_profile.py`/`score_shelf_cross.py` haben das schon als Diagnose).
7. **Fahrbahnflächen auf Bauwerken:** Brückenstücke nicht in die `strada_sentiero`-Partition der Ebene 0 legen, sondern als eigene Ebene
   bzw. als `ponte_passerella`-Umriss liefern; Tunnel/Galerie-Stücke bekommen keine Fläche (Magliasina baut dort nichts).
8. **Ebenen an Kreuzungen:** Wo ein Brückenstück über einen anderen Link führt, darf dessen Fläche nicht ausgeschnitten werden.
   `level_intermediate` des GIP liegt nur auf 5 befahrbaren Links vor; die DGM-Klassifikation liefert die Ebene implizit (Brücke = oben).

## 3. Zusammensetzen mit den Schweizer Daten

### Befund: GIP in der Schweiz

- 15 GIP-Links mit zusammen **28,5 km** liegen im Ausschnitt in der Schweiz:
  6,3 km FRC 1 (Engadiner Straße bei Martina), 10,7 km FRC 4 (Samnaunerstraße Vinadi–Samnaun),
  0,8 km FRC 10 (Rad-/Fußweg) und **10,9 km FRC 200 = MTB-Singletrails** (form_of_way 200) – keine Straßen.
- Abstand zu swissTLM3D (alle 5 m): Median **0,8 m**, p90 6,6 m, p99 94 m. GIP ist in der Schweiz also meist eine Kopie, aber:
- **An den Tunneln der Samnaunerstraße weicht GIP bis 151 m ab.** Wo swissTLM3D die Straße in Kehr- bzw. Umfahrungstunnels führt,
  zeichnet GIP eine offene Kurve (alte Trasse oder schematisch). Betroffen: Link `e50d…` (max 151 m), `de95…` (51 m), `9cac…` (25 m).
  Bild: [img/samnaun_tunnels.png](img/samnaun_tunnels.png), Übersicht [img/gip_in_ch.png](img/gip_in_ch.png).
- GIP kennt dort keine Bauwerke (BRUNNEL ist nur Österreich), TLM schon (`kunstbaute` Tunnel/Galerie).

### Befund: terrainWorker

- Der neue Routing-Pfad (`gip_routing_load.py`, `build_link_surfaces.py`, `score_check_grade.py`, `mask_dgm_by_links.py`) nimmt **alle** Links im Ausschnitt, auch die Schweizer.
- `authorities.py` hat Grenzabgleich (`match_border_ends`, Band 40 m, Fang 15 m), hängt aber am Tirol-WFS-Pfad.
  Die Gültigkeitsflächen kommen aus Dateien oder groben WGS84-Ringen bzw. NUTS 1:1 Mio. – für einen Schnitt auf Straßenniveau zu grob.
- In `pfunds_spiess.yaml` ist für `schweiz` noch keine Straßenquelle eingetragen.

### Vorschlag

1. **Grenze:** `swissBOUNDARIES3D` `tlm_landesgebiet` (CH) als exakte Linie, nicht NUTS. Download über die STAC-API von swisstopo.
2. **Österreich:** GIP-LINK, an der Grenze schneiden; Links mit Anteil Schweiz > 0 nur bis zur Grenze behalten. FRC ≥ 100/200 ohnehin raus.
3. **Schweiz:** swissTLM3D `tlm_strassen_strasse` (3D, `kunstbaute`, `objektart` – genau das Format, das Magliasina liest)
   plus AV-Bodenbedeckung (LCSF) als Flächen. Die AV ist eine echte Partition (0 Überlappungen im Ausschnitt).
   GR liefert deutsche Klassen im Feld `Art`; Magliasina erwartet italienische im Feld `Genere`:
   `Strasse_Weg→strada_sentiero`, `Trottoir→marciapiede`, `Verkehrsinsel→spartitraffico`, `uebrige_befestigte→altro_rivestimento_duro`, `Gewaesser_stehendes→specchio_acqua`.
4. **Grenzübergänge verknoten:** Im Ausschnitt kreuzen 3 GIP-Links die Grenze; der nächste TLM-Grenzübergang liegt jeweils 0,2 / 0,8 / 2,7 m daneben.
   Beide Linien auf einen gemeinsamen Punkt auf der Grenze ziehen (Mittelpunkt der zwei Schnittpunkte) → gemeinsamer Knoten.
   TLM hat 10 Grenzübergänge, 7 davon ohne GIP-Gegenstück (Klasse nicht einzeln geprüft): als Sackgassen behalten
   oder mit der österreichischen Seite aus OSM/BEV verbinden.
5. **Flächen an der Grenze:** die österreichischen Fahrbahnflächen exakt an derselben Grenzlinie schneiden wie die AV; dann teilen sie die Kante.
6. **Höhen:** Tirol DGM (Gebrauchshöhen, Pegel Triest) und swissALTI3D/TLM (LN02) unterscheiden sich um einen Betrag im Dezimeterbereich.
   Den Versatz im Überlappungsstreifen des Misch-DGM messen und eine Seite verschieben, bevor Profil-Fit und Netzglättung laufen;
   sonst entsteht an jedem Grenzübergang eine Stufe. Lage LV95→25832 über das offizielle Gitter (CHENyx06), nicht über die Helmert-Näherung.

## 4. Adapter auf Magliasina-Seite (Code-Änderungen in magliasinaNG)

1. `config.py`, `geo.py`, `terrain.py`: EPSG:2056 als Konstante ersetzen (25832), Ursprung E0/N0 = Gebietsmitte, Skalenfaktor K neu.
   Achtung: `geo.mosaic` filtert Kacheln über LV95-Grenzen und überspringt andere still.
2. `area.py`: eigenes Gebietspolygon, ohne Street-View-Route und Extras.
3. `network.load_lines()`: liefert `[(props, Q)]`, Q = (n,3) lokal, z darf NaN sein.
   props mindestens `OBJEKTART` (aus `CLASSES`), `KUNSTBAUTE` (`Keine`/`Bruecke`/…; Tunnel und Galerie vorher raus), `UUID` (GIP-`object_id`).
   Für die Schweizer Seite kommt das direkt aus TLM.
4. Klassen-Mapping GIP → TLM (Vorschlag, Breite danach aus `width_average`):
   FRC 0–1 → `8m Strasse`, 2–3 → `6m Strasse`, 4–6 → `4m Strasse`, 7–8 → `3m Strasse`, 11 / 105 → `2m Weg`, 10 / 12 / 101 → `1m Weg`; FRC ≥ 20 (Bahn, MTB, Fähre) verwerfen.
   `form_of_way` 2 (Fahrbahnteilung) → `RICHTUNGSG="Wahr"`, `form_of_way` 4 (Kreisverkehr) → `KREISEL="Wahr"`, 6/12 (Parkplatz) → `Platz`.
5. `WORK/av_local.pkl` aus euren Flächen (Österreich) + AV (Schweiz), Klassen wie in Abschnitt 3.3.
6. Strada-Cantonale-Sonderpfad abschalten: `network_surface.cantonale_hold` leer zurückgeben, Korridorblock in `build_level.stage_roads` überspringen,
   `roadheight` durch geglättetes DTM ersetzen, Hauptroute in `ai_roads.build` entfernen. Sonst Absturz (fehlende `poses.json`, `road_profile.npz`).
7. `dati/ponti.json` leeren, `dati/osm_area.json.gz` (Tessin) entfernen oder Tiroler OSM-Extrakt liefern.
8. Achs-Z: GIP hat keins, Magliasina kommt mit NaN aus (Deck aus den Rampen). Einzige Schwäche: Brücken mit freiem Ende und freistehende Stege (Abschnitt 2a).

## 5. Offene Punkte / nicht geprüft

- Die tatsächlichen Ausgaben von terrainWorker (`data/processed/…`) liegen nur lokal; geprüft wurden Code, Commits und Rohdaten.
- Ob die GIP-Abweichung an den Samnaun-Tunneln die alte Trasse ist oder nur schematisch – für die Pipeline egal, in der Schweiz gilt TLM.
- Wie gut die Lateralkorrektur die 1,5-m-Bedingung schafft: erst mit euren Flächen messbar (Abstand Achse→Flächenmitte pro 2-m-Station).
- Höhenversatz Tirol/Schweiz: Zahl nicht gemessen (DGM nicht geladen).
- Nachfolge-Stufen in Magliasina (`walls`, `props`, `guardrails`) nutzen teils noch `ctx["corridor"]`/`poses.json`; nicht vollständig geprüft.

## 6. Reproduzieren

Daten unter `$REVIEW_DATA` (Standard `data/review`, nicht im Repo):
`gip/gip_network_ogd.gpkg` (open.gip.gv.at `B_gip_network_ogd.zip`), `gip/gip_reference_ogd.gpkg` (`C_gip_reference_ogd.zip`),
`ch/SWISSTLM3D_2026_LV95_LN02.gpkg` (swisstopo STAC), `ch/sb3d/swissBOUNDARIES3D_1_5_LV95_LN02.gpkg`.

```powershell
python tools\review\extract_box.py
python tools\review\check_topology.py data\review\gip\net_box.gpkg LINK
python tools\review\compare_border.py
```
`fetch_ch.py` holt TLM-Straßen ohne Komplettdownload über die geo.admin-API (ohne `kunstbaute` und Z, nur für einen schnellen Blick).
