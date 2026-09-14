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

- Deck-XY folgt der **OSM-Straßenachse** (Enden liegen auf der geraden Road-Geometrie)
- Z / Neigung / Breite: **Portal-Querprofile** der Straße, dazwischen Hermite (Z) + Lerp (Breite); Normal aus Achstangente + Pitch
- `extend_before_m` / `extend_after_m`: Verlängerung entlang der Straße (oft 0.5, manchmal ~3) — nur MeshRoad
- `under_inset_m`: Fels-Maske unter der Brücke = Gap **ohne** Extends, zusätzlich um diesen Betrag nach innen verkürzt (Asphalt bleibt auf Auflagern)
- `materials.texture_length`: MeshRoad-UV in m/Repeat (kleiner = feiner; Default ~2.5)
- `width_from_road: true`: Portalbreiten aus OSM; `step_m` dichter → folgt Kurve besser
- `style.understructure` / `style.edge`: Platzhalter (slab/piers/guardrail/curb …) für spätere Builds
- `materials.top/bottom/side`: MeshRoad-Materials (echte `Material`-Einträge in `art/road/`, nicht Terrain-Paint-Namen). Default `Asphalt`/`Concrete` werden aus den Terrain-Texturen des Levels angelegt.
- `bridges_decks.json`: `under_nodes_xyw` → `build_terrain_masks.py` malt darunter **rock**

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE='config/sites/l13_kuehtai.yaml'; python tools\build_bridges.py; python tools\build_terrain_masks.py
```

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
