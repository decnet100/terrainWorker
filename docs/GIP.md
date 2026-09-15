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

- **`profile: road_spline`** + **`centerline: strassennetz`**: Deck = 4 MeshRoad-Streifen aus `road_span_profile` (Fernpass-Mega / L13-Splining). Legacy: `hermite` + OSM.
- `extend_before_m` / `extend_after_m`: Verlängerung über die Widerlager hinaus (nur Mesh + Conform-Band)
- `under_inset_m`: Fels-Maske unter der Brücke = Gap **ohne** Extends, zusätzlich nach innen verkürzt
- `portal_z_offset_m: [dz_s0, dz_s1]`: Seite höher/tiefer; Grade dazwischen passt sich an
- `approach_conform_*` / `max_raise_m` / `max_cut_m`: Heightmap an Deck; Raise klein halten → Schlucht nicht zuschütten
- `materials.*` / `style.*`: wie bisher (MeshRoad-Materials in `art/road/`)
- `bridges_decks.json`: `under_nodes_xyw` → `build_terrain_masks.py` malt darunter **rock**

#### `abutment_s` — Widerlager manuell (DGM-Kerben / Schwellen)

Wenn das **Original-DGM** unter der GIP-Brücke eine Kerbe/Schlucht hat (Fahrbahn-Z bleibt hoch, Terrain fällt ab), setzt Auto-Span die Portale oft zu weit in den Absturz. Dann sitzt das Deck falsch oder Conform/Road-Bed erzeugen Schwellen auf der Auffahrt.

**Lösung:** Widerlager auf die letzte/feste Stelle **vor** bzw. **nach** der Kerbe pinnen (Station der aktiven Centerline — bei Spline: **Strassennetz-Meter**, nicht OSM):

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
