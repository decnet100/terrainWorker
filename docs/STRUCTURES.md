# Kunstbauten: Brücken, Tunnel, Galerien, Rampen

Anleitung zum **Einbauen und Nachziehen** in einer Site-YAML (`config/sites/*.yaml`). Technischer Hintergrund und WFS-Cache: [GIP.md](GIP.md). Heightmap-Schichten: [HEIGHTMAP_COMPOSE.md](HEIGHTMAP_COMPOSE.md). Import ins Spiel: [BEAMNG_IMPORT.md](BEAMNG_IMPORT.md). Abnutzung und Einbauten auf einer späteren Fahrbahn-DAE (Idee): [ROAD_SURFACE_DETAIL.md](ROAD_SURFACE_DETAIL.md).

Die Fahrbahn ist immer die **Oberkante**. MeshRoad-Node-Z, Centerline-Z, Heightmap unter der Platte und Decals beziehen sich auf dieselbe Fläche, nicht auf Plattenmitte oder Unterkante.

Alle Kunstbauten sitzen auf **Auflagen / Widerlagern**. Heightmap an den Portalen auf die Oberkante, letzte Meter der Zufahrt als Pad, Bohrloch und Brückenspanne unberührt. `approach_conform` und `force_deck_z` sind Code-Defaults — nicht pro Site neu anschalten. Abschalten nur am Item. Vor der Tür eine eigene Platte: `append_unify` (zweites Unify, `kind: road`).

---

## Was das GIP liefert

Die Klasse steht in **`OBJEKT`**, nicht im Straßennamen. `KUNSTBAUTEN` ist Freitext und schwächer. `STRNAME` (z. B. „Landecker Tunnel“) ist **kein** Tunnel-Schalter.

| `OBJEKT` | Bedeutung | Pipeline |
|----------|-----------|----------|
| `S-A` / `S-B` | Stammstrecke Autobahn / Bundesstraße | volles Kit (Decal, Road-Bed, Guardrails) |
| `S-AT` / `S-BT` / `S-LT` | Tunnel | Galerie/Tunnel-Mesh, kein Oberflächen-Decal im Bohrloch |
| `S-AB` / `S-BB` | Brücke | MeshRoad-Deck |
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

1. GIP-OBJECTID der Brücke (`S-AB` / `S-BB`, oder `KUNSTBAUTEN` mit „Brücke“). Fehlt das Feature im Cache: `beamng.bridges.gip_extra` mit `objectid` (siehe [GIP.md](GIP.md)).
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
| Offene Galerie | `open_side: left` oder `right` (falsche Seite → im Item umdrehen). Gelände über der Spanne bleibt unangetastet (`terrain_roof: none`) |
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
