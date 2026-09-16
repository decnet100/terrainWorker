# GIP / Tirol Verkehrswege (WFS)

Landesstraßen L+B über ArcGIS WFS (CC BY 3.0 AT):

`https://dservices3.arcgis.com/hG7UfxX49PQ8XkXh/arcgis/services/Verkehrswege/WFSServer`

FeatureType: `Verkehrswege:Verkehrswege` · Default-CRS: **EPSG:31254**

## Download-Regel

Der Dienst ist groß (~2e5 Features) und langsam. Deshalb:

```powershell
python tools\fetch_gip.py          # Cache nutzen, wenn BBOX/str_code passen
python tools\fetch_gip.py --force  # bewusst neu laden
```

- Filter zuerst nach `sources.gip.str_code` (z. B. `L13`)
- Dann Clip auf `site.bbox`
- Spatial `bbox=` am WFS liefert hier oft **0 Treffer** — daher Road-Filter + lokaler Clip
- Cache: `data/raw/gip_<site>_<hash>.geojson` (+ `.meta.json`)
- Zusammenfassung: `data/processed/gip_structures.json`
- **Nachträge:** `beamng.bridges.gip_extra` — OBJECTIDs ohne `KUNSTBAUTEN` (auch außerhalb `STR_CODE`) per FeatureServer holen; Default-Name `Brücke {oid}` / `Tunnel {oid}` (oder `name:`)

```yaml
beamng:
  bridges:
    gip_extra:
      - objectid: 3992            # → Brücke 3992
      - objectid: 8082
        kind: tunnel
        name: Tunnel 8082
```

Normale Builds (`build_smoke` / Masken / Guardrails) rufen den WFS **nicht** an.

## Centerline-Quellen (Stand 2026-09-16 / Fernpass-Mega)

**Fernpass-Mega (`fernpass_mega.yaml`) nutzt überall GIP Verkehrswege** — nicht OSM, nicht Straßennetz.

| Baustein | YAML-Key | Loader |
|----------|----------|--------|
| Decals | `decal_roads.centerline_source: gip` | `gip_road_segments.load_gip_polylines_for_decals` → ~90 OBJECTID-Polylines, dann Decal-`stitch_abutting` |
| Guardrails | `guardrails.centerline: gip` | `load_gip_road_segments` — **eine Entscheidungseinheit = GIP OBJECTID** (Rules, steep-sides, Tunnel-Skip) |
| Bridges (`road_spline`) | `bridges.defaults.centerline: gip` | `load_gip_stitched_span_road` → **eine** ~15,7 km-Spine |
| Galleries | `galleries.defaults.centerline: gip` | Hermite: GIP-Segmente als `roads`; Spline: dieselbe Spine |

### Warum nicht OSM / Straßennetz?

- **OSM:** oft Ortho-geklickte Achse (auch bei 2+1) → symmetrischer Half-Width-Offset sitzt falsch auf der realen Fahrbahn.
- **Straßennetz:** eine glatte Achse (~OID 473), ~5 m mittlerer Versatz zur GIP-Achse — kurz getestet, dann verworfen zugunsten GIP-Konsistenz mit Decals/Rails.
- **GIP:** gleiche Geometrie wie Regeln/OIDs; Decals und Rails teilen dieselbe Achse.

### Zwei GIP-„Merge“-Modi (wichtig)

1. **Decal-`stitch_abutting`** — nur Degree-2 End-an-End (wie OSM-Stubs). An Rampen/Knoten stoppt der Merge → längste Komponente oft nur ~700 m. Für Decals OK (viele Stücke).
2. **Span-Spine `_chain_gip_spine`** (`tools/gip_road_segments.py`) — startet am längsten Stück, wächst an Enden; bei Verzweigung Partner mit bester Fortsetzungsrichtung. Ergebnis auf Fernpass: **~15658 m / 66 von 90 Segmenten** (gleiche Länge wie Straßennetz). **Brücken brauchen diese Spine**, sonst fehlt die Station außerhalb der kurzen Stitch-Komponente.

Cache: `data/processed/<site>/gip_roads_beamng.json` (pro OBJECTID Nodes `[x,y,z,width]`).

### Guardrails auf Brücken

Rails folgen sonst dem **DGM** unter der Schlucht. Mit `bridge_deck_z: true` + `bridges_items.level.json` / `bridges_decks.json`:

- Z = MeshRoad-Deckoberseite
- `bridge_lateral_extra_m` (Default 0) statt roadside `lateral_extra_m` — näher an die Deck-Kante

`build_bridges` schreibt **nur** `level_objects/bridges/` — überschreibt Guardrails **nicht**.

### YAML-Fallen

- `guardrails.style` auf Fernpass ist **`sections`** (Italy-Schiene). Nicht still auf `posts` zurücksetzen — Leitpoller nur bewusst oder per Rule.
- Aliase: `gip` / `verkehrswege` / `objectid`; `strassennetz` / `sn`; Default vieler Tools bleibt `osm` wenn der Key fehlt.

## Wichtige Attribute

| Feld | Bedeutung |
|------|-----------|
| `STR_CODE` / `STRNAME` | Straßenkennung (L13, …) |
| `KUNSTBAUTEN` | Name der Kunstbaute (Galerie, Brücke, …) oder leer |
| `OBJEKT` | z. B. `S-LT` Tunnel/Galerie, `S-LB` Brücke |
| `OBJEKTBEZEICHNUNG` | Klartext-Typ |
| `Shape__Length` | Segmentlänge (m) |

### L13-Ausschnitt Kühtai (1024², Stand Seed)

| kind | Name | Länge ca. |
|------|------|-----------|
| gallery | Mugkögele- und Marcheckgalerie | ~71 m + ~546 m (2 Segmente) |
| gallery | Rauheneckgalerie | ~203 m |
| bridge | Klammbachbrücke | ~11 m |

## Nutzung später

Segmente mit `KUNSTBAUTEN` → Annotations-Layer `gallery` / `bridge` / Hole-Maps / Meshes.  
Hand-Korrektur in QGIS bleibt möglich (offene Galerie-Seite, Portale).

### Brücken-Config (Site-YAML)

`beamng.bridges.defaults` + `beamng.bridges.items[]` (Match über `objectid` / `name`):

- **`profile: road_spline`** + **`centerline: gip`** (Fernpass-Mega): Deck = 4 MeshRoad-Streifen aus `road_span_profile` entlang der GIP-Spine. Alternativ `centerline: strassennetz`. Legacy: `hermite` + OSM.
- `extend_before_m` / `extend_after_m`: Verlängerung über die Widerlager hinaus (nur Mesh + Conform-Band)
- `under_inset_m`: Fels-Maske unter der Brücke = Gap **ohne** Extends, zusätzlich nach innen verkürzt
- `portal_z_offset_m: [dz_s0, dz_s1]`: Seite höher/tiefer; Grade dazwischen passt sich an
- `approach_conform_*` / `max_raise_m` / `max_cut_m`: Heightmap an Deck; Raise klein halten → Schlucht nicht zuschütten
- `materials.*` / `style.*`: wie bisher (MeshRoad-Materials in `art/road/`)
- `bridges_decks.json`: `under_nodes_xyw` → `build_terrain_masks.py` malt darunter **rock**

#### `abutment_s` — Widerlager manuell (DGM-Kerben / Schwellen)

Wenn das **Original-DGM** unter der GIP-Brücke eine Kerbe/Schlucht hat (Fahrbahn-Z bleibt hoch, Terrain fällt ab), setzt Auto-Span die Portale oft zu weit in den Absturz. Dann sitzt das Deck falsch oder Conform/Road-Bed erzeugen Schwellen auf der Auffahrt.

**Lösung:** Widerlager auf die letzte/feste Stelle **vor** bzw. **nach** der Kerbe pinnen (Station der aktiven Centerline — bei Spline: **GIP-Spine-Meter** bzw. Straßennetz-Meter, nicht OSM):

```yaml
- match: { objectid: 3852, name: Brücke 3852 }
  abutment_s: [13408.0, 13425.0]   # s0/s1 aus bridges_meta / DGM-vs-Netz-Check
  free_span: linear
  approach_conform_max_raise_m: 0.8   # Kerbe nicht füllen
  approach_conform_max_cut_m: 12.0    # nur Cliff-Lippen
```

Werte: entlang der Centerline sampeln (Netz-Z ≈ DGM), Kerbe meiden. Danach `build_bridges` als **letzter** Heightmap-Schritt + Terrain-Reimport; Road-Bed danach kann die Auffahrt wieder zerstören.

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE='config/sites/fernpass_mega.yaml'; python tools\build_bridges.py
```

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE='config/sites/l13_kuehtai.yaml'; python tools\build_bridges.py; python tools\build_terrain_masks.py
```

#### Galerie-/Tunnel-Dach auf dem Terrain (`terrain_roof`)

Top-down malt `build_terrain_masks` standardmäßig **Fels** über dem Galerie-/Tunnel-Grundriss (sonst bliebe OSM-Asphalt der untertunnelnden Achse auf dem Berg).

| Wert | Bedeutung |
|------|-----------|
| `rock` (Default) | Fels gewinnt — typischer Bergtunnel |
| `keep_asphalt` | Fels nur wo **keine** Straßen-Asphalt-Maske; Straße **darüber** (z. B. Fernpass über Unterführung 8082) bleibt Asphalt. Zusätzlich: `build_decal_roads` clippt DecalRoads an diesem Korridor **nicht** weg. |
| `none` | kein Dach-Fels für dieses Objekt |

```yaml
- match: { objectid: 8082 }
  terrain_roof: keep_asphalt
```

Danach: `build_galleries` (schreibt Flag in `galleries_centerlines.json`) → `build_terrain_masks` → `build_decal_roads` → Terrain-Texturmaps / Level neu laden.

### Galerien-Config (Site-YAML)

`beamng.galleries.defaults` + `beamng.galleries.items[]` (Match wie Brücken):

| Key | Bedeutung |
|-----|-----------|
| `clear_height_m` | lichte Höhe Fahrbahn → Dach-Unterkante |
| `roof_thickness_m` | Dachstärke (für späteres Mesh) |
| `extend_before_m` / `extend_after_m` | Übergang über GIP-Enden hinaus (Achse) |
| `trim_s0_m` / `trim_s1_m` | Meter abschneiden am GIP-Start / -Ende (Portal s0 / s1) |
| `open_side` | `left` \| `right` \| `both` \| `none` |
| `hole_pad_m` / `blend_open_dgm` | später Hole-Map / DGM-Mischung |
| `style.shell/columns/edge/portal` | Darstellungseigenschaften je Objekt (Platzhalter) |
| `materials.*` | wie Brücken, sobald Mesh kommt |

L13-Items: Mugkögele **7236** + **10099** (Map-Rand, vorerst `enabled: false`), Fokus **Rauheneckgalerie 8276**.

- Achse: OSM-XY; **`z_profile: road`** folgt dem Straßenband (nicht DGM auf dem Galeriedach). `hermite` optional.
- Portal-Anker: Suche `terrain≈road` außerhalb der GIP-Enden (`portal_search_*`); Hole-`z_ref` nur von dort.

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE='config/sites/l13_kuehtai.yaml'; python tools\build_galleries.py
```

Schreibt `galleries_meta.json` + `galleries_centerlines.json`, DAE unter  
`art/shapes/galleries/`, TSStatics in SimGroup `galleries`, und  
`import/theTerrain_holemap.png` (Preset-`holeMapPath`). Magenta-Debug: SimGroup `gallery_debug`.

`open_side: left|right` = Wand weglassen (Rausschauen). Falsch rum → in `items[]` flippen.

**Hole-Map:** Portale mit Abstand≤Halbbreite+Pad;  
`road+hole_min_above < z ≤ road+clear_height`; Neigung ≥ `hole_min_slope_deg` (45°).  
Innen-Portale an aneinanderstoßenden GIP-OIDs (z. B. Mugkögele 7236↔10099) entfallen.

Nach Build: Level neu laden, **terrainPreset.json mit Hole-Map** neu importieren.
