# GIP / Tyrol roads (WFS)

Provincial roads L+B via ArcGIS WFS (CC BY 3.0 AT):

`https://dservices3.arcgis.com/hG7UfxX49PQ8XkXh/arcgis/services/Verkehrswege/WFSServer`

FeatureType: `Verkehrswege:Verkehrswege` · default CRS: **EPSG:31254**

## Download rule

The service is large (~2e5 features) and slow. So:

```powershell
cd C:\temp\beamng_autoroad; python tools\fetch_gip.py
```

```powershell
cd C:\temp\beamng_autoroad; python tools\fetch_gip.py --force
```

- `sources.gip.str_code` is the **through route** (trunk), e.g. `B179` / `L13`
- Optional `include: bbox` — all **provincial roads with `STR_CODE`** in `site.bbox` via FeatureServer intersect (local lanes, forest/cycle ways without a code are dropped; WFS `bbox=` often returns **0 hits** here)
- Optional `str_codes: [B179, L…]` — explicit codes (also fallback if spatial is empty)
- Then clip to `site.bbox`
- Cache: `data/raw/gip_<site>_<hash>.geojson` (+ `.meta.json`); hash includes `include` / `str_codes`
- Summary: `data/processed/<site>/gip_structures.json` including `str_code_counts`
- **Extras:** `beamng.bridges.gip_extra` — OBJECTIDs without `KUNSTBAUTEN` (also outside `STR_CODE`) fetched via FeatureServer; default name `Brücke {oid}` / `Tunnel {oid}` (or `name:`)

```yaml
sources:
  gip:
    str_code: B179
    include: bbox            # all L+B in the site bbox; trunk stays B179
# str_codes: [B179, L354]  # fallback instead of spatial

beamng:
  bridges:
    gip_extra:
      - objectid: 3992            # → Brücke 3992
      - objectid: 8082
        kind: tunnel
        name: Tunnel 8082
  roads:
    spine_start_oid: null
    spine_next: []
    # - from_oid: 3852
    #   to_oid: 3901
    #   note: "stay on B179 after the bridge"
```

Normal builds (`build_smoke` / masks / guardrails) do **not** call the WFS.

## Centerline sources (Fernpass Mega)

**Fernpass Mega (`fernpass_mega.yaml`) uses GIP Verkehrswege everywhere** — not OSM, not the Tyrol road network.

| Piece | YAML key | Loader |
|-------|----------|--------|
| Decals | `decal_roads.centerline_source: gip` | `load_gip_polylines_for_decals` → all OBJECTID polylines in the cache, then decal `stitch_abutting` (no merge across `STR_CODE`) |
| Guardrails | `guardrails.centerline: gip` | `load_gip_road_segments` — **one decision unit = GIP OBJECTID** |
| Bridges (`road_spline`) | `bridges.defaults.centerline: gip` | `load_gip_corridor_for_feature` — corridor that contains the OBJECTID |
| Galleries | `galleries.defaults.centerline: gip` | Hermite: GIP segments; spline: the same per-feature corridor |

### Why not OSM / road network?

- **OSM:** often an ortho-clicked axis (even on 2+1) → a symmetric half-width offset sits on the wrong carriageway.
- **Road network (Straßennetz):** one smooth axis (~OID 473), ~5 m mean offset from the GIP axis — briefly tested, then dropped in favour of GIP consistency with decals/rails.
- **GIP:** same geometry as rules/OIDs; decals and rails share the axis.

### Corridors instead of one super-spine

Do not dump every GIP piece into **one** chain. `load_gip_corridors` (`tools/gip_road_segments.py`):

1. Split pieces by `STR_CODE`. **Trunk** = `sources.gip.str_code` (B179) — side roads must not steal this chain. `abutment_s` stays valid in trunk metres.
2. Per code, heading chain as before (longest piece or `beamng.roads.spine_start_oid`; at forks, continuation heading).
3. Remainder of the same code (ramps, parallel arms) = mini-corridors `gip_<code>_leftover_N`.
4. Other `STR_CODE`s = their own side corridors.

Structure → corridor via **OBJECTID membership**, not “nearest line on the map”. Dump: `data/processed/<site>/gip_corridors.json`.

At a fork after a bridge (two almost equally good B179 arms) heading is not enough:

```yaml
beamng:
  roads:
    spine_start_oid: 1234          # optional
    spine_next:
      - from_oid: 3852
        to_oid: 3901
```

`from_oid` at the chain end → only `to_oid` applies; otherwise a warning and stop at that end. Unused overrides are warned at the end. Overrides apply only inside the same `STR_CODE` group.

### Two GIP “merge” modes (important)

1. **Decal `stitch_abutting`** — only degree-2 end-to-end of the **same** `STR_CODE`. Stop at T-nodes / crossings. Fine for decals (many pieces).
2. **Corridor chain `_chain_gip_spine`** — per `STR_CODE` (trunk isolated). Fernpass trunk: **~15658 m / 66 of 90 B179 segments**. Bridges need **their** corridor, otherwise the station is missing outside the short stitch component.

Cache: `data/processed/<site>/gip_roads_beamng.json` (per OBJECTID nodes `[x,y,z,width]`).

### Guardrails on bridges

Rails otherwise follow the **DGM** under the gorge. With `bridge_deck_z: true` + `bridges_items.level.json` / `bridges_decks.json`:

- Z = MeshRoad deck top
- `bridge_lateral_extra_m` (default 0) instead of roadside `lateral_extra_m` — closer to the deck edge

`build_bridges` writes **only** `level_objects/bridges/` — it does **not** overwrite guardrails.

### YAML traps

- `guardrails.style` on Fernpass is **`sections`** (Italy rail). Do not silently reset to `posts` — delineators only when intended or per rule.
- Aliases: `gip` / `verkehrswege` / `objectid`; `strassennetz` / `sn`; default of many tools stays `osm` if the key is missing.

## Important attributes

| Field | Meaning |
|-------|---------|
| `STR_CODE` / `STRNAME` | road id (L13, …) |
| `KUNSTBAUTEN` | structure name (gallery, bridge, …) or empty |
| `OBJEKT` | e.g. `S-LT` tunnel/gallery, `S-LB` bridge |
| `OBJEKTBEZEICHNUNG` | type in plain language |
| `Shape__Length` | segment length (m) |

### L13 Kühtai crop (2048²)

| kind | Name | Length approx. |
|------|------|----------------|
| gallery | Mugkögele- und Marcheckgalerie | ~71 m + ~546 m (2 segments) |
| gallery | Rauheneckgalerie | ~203 m |
| bridge | Klammbachbrücke | ~11 m |

## Later use

Segments with `KUNSTBAUTEN` → annotation layers `gallery` / `bridge` / hole maps / meshes.  
Hand correction in QGIS stays possible (open gallery side, portals).

### Bridge config (site YAML)

`beamng.bridges.defaults` + `beamng.bridges.items[]` (match via `objectid` / `name`):

- **`profile: road_spline`** + **`centerline: gip`** (Fernpass Mega): deck = 4 MeshRoad strips from `road_span_profile` along the **GIP corridor of the structure**. Alternative `centerline: strassennetz`. Legacy: `hermite` + OSM.
- `extend_before_m` / `extend_after_m`: extend past the abutments (mesh + conform band only)
- `under_inset_m`: rock mask under the bridge = gap **without** extends, shortened further inward
- `portal_z_offset_m: [dz_s0, dz_s1]`: one side higher/lower; grade in between follows
- `approach_conform_*` / `max_raise_m` / `max_cut_m`: heightmap to deck; keep raise small → do not fill the gorge
- `materials.*` / `style.*`: as before (MeshRoad materials in `art/road/`)
- `bridges_decks.json`: `under_nodes_xyw` → `build_terrain_masks.py` paints **rock** underneath

#### `abutment_s` — abutments by hand (DGM notches / thresholds)

If the **original DGM** under the GIP bridge has a notch/gorge (road Z stays high, terrain drops), auto-span often places portals too far into the drop. Then the deck sits wrong or conform/road-bed create thresholds on the approach.

**Fix:** pin abutments to the last solid place **before** / **after** the notch (station of the active centerline — for spline: **metres on the GIP corridor of the structure** or road-network metres, not OSM):

```yaml
- match: { objectid: 3852, name: Brücke 3852 }
  abutment_s: [13408.0, 13425.0]   # s0/s1 from bridges_meta / DGM-vs-network check
  free_span: linear
  approach_conform_max_raise_m: 0.8   # do not fill the notch
  approach_conform_max_cut_m: 12.0    # cliff lips only
```

Values: sample along the centerline (network Z ≈ DGM), avoid the notch. Then `build_bridges` as the **last** heightmap step + terrain re-import; road-bed after that can wreck the approach again.

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/fernpass_mega.yaml"; python tools\build_bridges.py
```

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/l13_kuehtai.yaml"; python tools\build_bridges.py
```

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/l13_kuehtai.yaml"; python tools\build_terrain_masks.py
```

#### Gallery / tunnel roof on the terrain (`terrain_roof`)

Top-down, `build_terrain_masks` paints **rock** over the gallery/tunnel footprint by default (otherwise OSM asphalt of the underpassing axis would stay on the mountain).

| Value | Meaning |
|-------|---------|
| `rock` (default) | rock wins — typical mountain tunnel |
| `keep_asphalt` | rock only where there is **no** road asphalt mask; the road **above** (e.g. Fernpass over underpass 8082) stays asphalt. Also: `build_decal_roads` does **not** clip DecalRoads away on this corridor. |
| `none` | no roof rock for this object |

```yaml
- match: { objectid: 8082 }
  terrain_roof: keep_asphalt
```

Then: `build_galleries` (writes the flag into `galleries_centerlines.json`) → `build_terrain_masks` → `build_decal_roads` → terrain texture maps / reload the level.

### Gallery config (site YAML)

`beamng.galleries.defaults` + `beamng.galleries.items[]` (match like bridges):

| Key | Meaning |
|-----|---------|
| `clear_height_m` | clearance carriageway → roof underside |
| `roof_thickness_m` | roof thickness (for a later mesh) |
| `extend_before_m` / `extend_after_m` | transition past GIP ends (axis) |
| `trim_s0_m` / `trim_s1_m` | metres to cut at GIP start / end (portal s0 / s1) |
| `open_side` | `left` \| `right` \| `both` \| `none` |
| `hole_pad_m` / `blend_open_dgm` | later hole map / DGM mix |
| `style.shell/columns/edge/portal` | look per object (placeholder) |
| `materials.*` | like bridges, once a mesh exists |

L13 items: Mugkögele **7236** + **10099** (map edge, currently `enabled: false`), focus **Rauheneckgalerie 8276**.

- Axis: OSM XY; **`z_profile: road`** follows the road band (not DGM on the gallery roof). `hermite` optional.
- Portal anchors: search `terrain≈road` outside the GIP ends (`portal_search_*`); hole `z_ref` only from there.

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/l13_kuehtai.yaml"; python tools\build_galleries.py
```

Writes `galleries_meta.json` + `galleries_centerlines.json`, DAE under  
`art/shapes/galleries/`, TSStatics in SimGroup `galleries`, and  
`import/theTerrain_holemap.png` (preset `holeMapPath`). Magenta debug: SimGroup `gallery_debug`.

`open_side: left|right` = omit a wall (look out). Wrong way → flip in `items[]`.

**Hole map:** portals with distance ≤ half-width + pad;  
`road+hole_min_above < z ≤ road+clear_height`; slope ≥ `hole_min_slope_deg` (45°).  
Inner portals at abutting GIP OIDs (e.g. Mugkögele 7236↔10099) are dropped.

After build: reload the level, **re-import terrainPreset.json with the hole map**.
