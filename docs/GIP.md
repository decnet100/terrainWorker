# GIP / Tyrol roads (WFS)

Provincial roads L+B via ArcGIS WFS (CC BY 3.0 AT):

`https://dservices3.arcgis.com/hG7UfxX49PQ8XkXh/arcgis/services/Verkehrswege/WFSServer`

FeatureType: `Verkehrswege:Verkehrswege` · native CRS: **EPSG:31254** (MGI / Austria GK West)

GIP is requested and cached in that local CRS (`WFS srsName`, FeatureServer `outSR`).
It is **not** fetched as EPSG:4326. Reprojection into the working CRS uses the
official correction grid: for MGI↔ETRS89 (GK↔UTM) the BEV
`at_bev_AT_GIS_GRID_2021_09_28.tif` from the [PROJ CDN](https://cdn.proj.org/at_bev_AT_GIS_GRID_2021_09_28.tif),
stored under `data/grids/` (gitignored, downloaded on first need). An explicit
`authorities.*.grid` (NTv2 `.gsb` or GeoTIFF) overrides that. A missing grid
for a datum change aborts — there is no silent Helmert / WGS84 hop.

Override the request CRS with `sources.gip.crs` when the service is not Tirol
GK West. Cache key includes `geom_crs`; old 4326 extracts are not reused
(`python tools\fetch_gip.py --force`).

The site must list the catalog authorities that CRS + bbox cover. Fill them
from the crop ([AUTHORITIES.md](AUTHORITIES.md)):

```powershell
cd C:\temp\beamng_autoroad; python tools\fill_site_authorities.py --write
```

Missing or `leave_empty` aborts before the WFS runs. Südtirol IDs are stored
as level ids (+10 000 000).

## Download rule

The service is large (~2e5 features) and slow. So:

```powershell
cd C:\temp\beamng_autoroad; python tools\fetch_gip.py
```

```powershell
cd C:\temp\beamng_autoroad; python tools\fetch_gip.py --force
```

- `sources.gip.str_code` is the **through route** (trunk), e.g. `B179` / `L13`
- Optional `include: bbox` — **all Verkehrswege** intersecting `site.bbox` via FeatureServer (local streets, forest/cycle ways included; WFS `bbox=` often returns **0 hits**). `str_code` remains the trunk. Guardrails stay off on minor/parking/ramp pieces.
- Optional `str_codes: [B179, L…]` — explicit codes (also fallback if spatial is empty)
- Then clip to `site.bbox`
- Cache: `data/raw/gip_<site>_<hash>.geojson` (+ `.meta.json`); hash includes `include` / `str_codes`
- Summary: `data/processed/<site>/gip_structures.json` including `str_code_counts`
- **Extras:** `beamng.bridges.gip_extra` — OBJECTIDs without `KUNSTBAUTEN` (also outside `STR_CODE`) fetched via FeatureServer; default name `Brücke {oid}` / `Tunnel {oid}` (or `name:`). YAML `objectid` is the **level** id (`authorities.gip_ids`); the service id is `SOURCE_OBJECTID`.

### Missing bridges (GIP does not flag Kunstbauten)

If a bridge OBJECTID is present as a Verkehrswege polyline but has no bridge
label in GIP, the road band follows the DGM notch and you get a short 1–2 m
dip in the driving surface. Before spending time hand-hunting OIDs, run the
precheck on **named** corridors (`STR_CODE` set):

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\diag_missing_bridges.py --only-str-code B189
```

The script writes a JSON report plus a small YAML snippet you can copy into
`beamng.bridges.gip_extra` (and optionally `beamng.bridges.items[].abutment_s`).

Details: [DIAG_MISSING_BRIDGES.md](DIAG_MISSING_BRIDGES.md)

```yaml
sources:
  gip:
    str_code: B179
    include: bbox            # full Verkehrswege layer in the site bbox; trunk stays B179
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
    #   - from_oid: 3852
    #     to_oid: 3901
    #     note: "stay on B179 after the bridge"
    # width_by_objectid:          # rare per-level exception; else data/roads/
    #   12345: 9.0
    # lanes_by_str_code:
    #   B179: 3               # 3+ lanes → skip the 8 m two-lane Bundesstraße default
```

Normal builds (`build_smoke` / masks / guardrails) do **not** call the WFS.

## Centerline sources (Fernpass Mega)

**Fernpass Mega (`fernpass_mega.yaml`) uses GIP Verkehrswege everywhere** — not OSM, not the Tyrol road network.

| Piece | YAML key | Loader |
|-------|----------|--------|
| Decals | `decal_roads.centerline_source: gip` | `load_gip_polylines_for_decals` — **DecalRoads only for pieces with `STR_CODE`** (`gip_decals: named`). Minor GIP still gets road-bed + terrain asphalt. |
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
5. Pieces **without** `STR_CODE` (local/forest/path) = one `minor` corridor each — no heading spine.

Guardrails (heuristic): none on missing `STR_CODE`, and none on subordinate one-lane strips (`S-AR` / `S-AP` / `S-BR` / `S-BP`) even when they share the trunk `STR_CODE`. Those strips still clip the trunk rails at the merge (`junction_mouth_m`). YAML `sides: none` still works on top.

Decals: `beamng.decal_roads.gip_decals: named` (default) writes DecalRoads only for `STR_CODE` pieces. Minor GIP still gets road-bed + terrain Asphalt (no DecalRoad). `gip_decals: all` restores the old full set.

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

### Follow parent (ramp Z)

A subordinate GIP piece (typically `S-AR`) can copy Z from a parent axis for a **chainage window from the join**, then blend back to DGM. That is **not** a radius around the node and **not** a `road_bed_items` stamp on the whole polyline — so the rest of a long ramp stays on the hillside.

```yaml
beamng:
  roads:
    follow_parent:
      - match: { objectid: 230743 }
        parent: landecker_approach   # unify id, or a GIP OBJECTID
        hold_m: 100
        blend_m: 30                  # smoothstep back to DGM
        max_raise_m: 8               # road-bed clamp vs DGM in that window
        max_cut_m: 8
        sink_m: 0.02
```

`parent` is a `unify` id first, else a GIP OBJECTID. Distance is metres along the child from the endpoint nearest the parent (join must be within `join_max_m`, default 40). Node Z in the hold window is the parent Z at the nearest XY; road-bed stamps that Z; DecalRoads skip `snap_to_heightmap` on these pieces so the previous compose cannot pull them back.

Near a MeshRoad plate this is not enough: gallery `span` is mixed **after** road-bed and the side-cut wins at the peel-off. Add `side_cut_preserve` and rebuild galleries. Cookbook: [STRUCTURES.md](STRUCTURES.md).

A heightmap-only terrace (variant 5) would instead need `from_join_m` / `to_join_m` on `road_bed_items` — without that window the stamp lifts the entire OBJECTID.

### Side-cut preserve (deck vs ramp)

MeshRoad `approach_conform_side_cut` drops terrain beside the slab (default: slab depth). That must not cut through another carriageway that shares the deck plane (an Autobahn ramp). A bike path (`S-FRW`) can stay in the ditch.

```yaml
beamng:
  roads:
    side_cut_preserve:
      objekt: [S-AR, S-AP, S-BR, S-BP]   # types (OR)
      objectid: [230743]                  # extra OBJECTIDs (OR)
      pad_m: 0.5                          # optional, default 0.5
```

Both matchers work and are unioned. A gallery/bridge item may add the same keys (`side_cut_preserve` or `approach_conform_side_cut_preserve`); they union with the site list. Missing key → the four subordinate types. Explicit empty dict → side-cut through everything. Under the slab, `force_deck_z` still owns the pixels. Full recipe: [STRUCTURES.md](STRUCTURES.md).

### Guardrails on bridges

Rails otherwise follow the **DGM** under the gorge. With `bridge_deck_z: true` + `bridges_items.level.json` / `bridges_decks.json`:

- Z = MeshRoad deck top
- `bridge_lateral_extra_m` (default 0) instead of roadside `lateral_extra_m` — closer to the deck edge

`build_bridges` writes **only** `level_objects/bridges/` — it does **not** overwrite guardrails.

### Guardrail clips (junctions + tunnels)

After offset joints, runs are split (same idea as gallery portal clip):

1. **`clip_centerlines`** — joint XY inside a *foreign* carriageway (`half_width + centerline_clip_pad_m`) → drop (opens T-junctions / crossings).
2. **`clip_tunnels`** — joint XY over a buried tunnel axis (S-BT / „tunnel“ / Unterflur) → drop (no rails on the mountain above the bore). Galleries stay on portal clip.
3. **`clip_galleries`** — existing portal bands from `galleries_centerlines.json`.

### YAML traps

- `guardrails.style` on Fernpass is **`sections`** (Italy rail). Do not silently reset to `posts` — delineators only when intended or per rule.
- Aliases: `gip` / `verkehrswege` / `objectid`; `strassennetz` / `sn`; default of many tools stays `osm` if the key is missing.

## Important attributes

| Field | Meaning |
|-------|---------|
| `STR_CODE` / `STRNAME` | road id (L13, …) |
| `KUNSTBAUTEN` | structure name (gallery, bridge, …) or empty |
| `OBJEKT` | class: `S-A` through Autobahn, `S-AR`/`S-AP` one-lane ramps, `S-AT` tunnel, `S-AB` bridge; `S-B` / `S-BR` / `S-BP` / `S-BT` / `S-BB` analog. Stronger than `KUNSTBAUTEN` for tunnel/bridge. Galleries are often `S-AT`/`S-BT` and still need the name (`Galerie`). |
| `OBJEKTBEZEICHNUNG` | type in plain language |
| `Shape__Length` | segment length (m) |

### L13 Kühtai crop (2048²)

| kind | Name | Length approx. |
|------|------|----------------|
| gallery | Mugkögele- und Marcheckgalerie | ~71 m + ~546 m (2 segments) |
| gallery | Rauheneckgalerie | ~203 m |
| bridge | Klammbachbrücke | ~11 m |

## Later use

How to **add or adjust** a bridge, tunnel, gallery, or a ramp beside a deck (including `follow_parent` and `side_cut_preserve`): [STRUCTURES.md](STRUCTURES.md).

Segments with `KUNSTBAUTEN` → annotation layers `gallery` / `bridge` / hole maps / meshes.  
Hand correction in QGIS stays possible (open gallery side, portals).

### Bridge config (site YAML)

`beamng.bridges.defaults` + `beamng.bridges.items[]` (match via `objectid` / `name`):

- **`profile: road_spline`** + **`centerline: gip`** (Fernpass Mega): deck = 4 MeshRoad strips from `road_span_profile` along the **GIP corridor of the structure**. Alternative `centerline: strassennetz`. Legacy: `hermite` + OSM.
- `extend_before_m` / `extend_after_m`: extend past the abutments (mesh + conform band only)
- `under_inset_m`: rock mask under the bridge = gap **without** extends, shortened further inward
- `portal_z_offset_m: [dz_s0, dz_s1]`: one side higher/lower; grade in between follows
- `approach_conform` / `force_deck_z` (Defaults an): Heightmap an den Auflagen auf die Oberkante; `max_raise_m` klein halten, sonst füllt die Heightmap die Schlucht. `max_cut_m` darf größer sein. Abschalten nur am Item.
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

#### Gelände über Brücke / Tunnel / Galerie (`terrain_roof`)

Zwischen den Auflagen bzw. Portalen bleibt das Gelände **unangetastet**: kein Terrain-Asphalt, kein Felsstreifen, kein Road-Bed, kein DecalRoad im Bohrloch. Das ist der Code-Default (`none`), auch für offene Galerien und Brücken. GIP-Stücke, die `is_gip_tunnel_segment` erkennt (S-AT / S-BT / S-LT / S-BG, oder Name/Kunstbauten mit Tunnel/Galerie/Unterflur), bekommen ebenfalls kein Terrain-Decal — Markierungen liegen auf der MeshRoad (`overObjects`).

Dach oder Straße oben nur mit **explizitem** YAML:

| Wert | Bedeutung |
|------|-----------|
| `none` (Default) | Spanne lässt die Landbedeckung |
| `rock` | Fels gewinnt — nur setzen, wenn das Dach gemalt werden soll |
| `keep_asphalt` | Fels nur wo **kein** Straßenasphalt liegt; die Straße **oben** (z. B. Fernpass über Unterführung 8082) bleibt. `build_decal_roads` schneidet diesen Korridor nicht weg. |

One-ended tunnels (only one portal on the map): `one_ended: true` plus `fake_end_drop_m` (default 10). The last in-map GIP end is a fake portal that far below the real portal Z. Portal mesh and hole only at the daylight end; MeshRoad **and** the closed wall/roof shell run the full bore.

With a prescribed outer centerline (`append_unify: <id>`), Z from the daylight portal to the far end is a straight line to a target height (`abutment_z`, or A12 / `max(road, DGM)` when `abut_snap_z`). `append_m` keeps only that many metres from the portal (omit = full unify). The last `abut_run_m` (default 5) of that span is the abutment pad — same seating idea as a bridge. The closed tube stays on `portal_s`; only the deck runs portal → pad. Terrain on that plate uses the **bridge** heightmap cut (`approach_conform` + `force_deck_z`): high DGM under the MeshRoad is lowered to the deck. Do not mean-axis the bore with the approach: the approach unify stays `kind: road`.

Parallel tubes that only share a mouth are **not** auto-merged (`merge_abutting` is end-to-end only). For a shared carriageway (Reschen: Landecker), name a mean axis under `beamng.roads.unify` and match it in galleries:

```yaml
beamng:
  roads:
    # Surface A12 pieces: data/roads/gip_overrides.yaml (not_tunnel_objectids)
    unify:
      - id: landecker
        objectids: [227551, 235599]   # the two S-AT halves
        mode: mean_axis
        width_m: 7.5
        replaces: [galleries, decals, guardrails, road_bed]
  galleries:
    items:
      - match: { unify: landecker }
        one_ended: true
```

`STRNAME` is not a tunnel flag. Only `OBJEKT` (S-AT / S-BT / S-LT / S-BG) and `KUNSTBAUTEN` count.

```yaml
- match: { objectid: 8082 }
  terrain_roof: keep_asphalt
```

Then: `build_galleries` (writes the flag into `galleries_centerlines.json`) → `build_terrain_masks` → `build_decal_roads` → terrain texture maps / reload the level.

### Gallery config (site YAML)

`beamng.galleries.defaults` + `beamng.galleries.items[]` (match like bridges):

| Key | Meaning |
|-----|---------|
| `width_m` / `width_from_road` | MeshRoad width; `false` = use `width_m` (Landeck 7.5 m) |
| `clear_width_m` | inner tube width (walls outside); Landeck 9.5 m with 1 m bankett each side |
| `clear_height_m` | clearance carriageway → roof underside (Landeck 4.0 m; + `roof_thickness_m` 0.5 = 4.5 m) |
| `wall_thickness_m` | wall outside the clear box (Landeck 0.4 m) |
| `wall_radius_m` | closed tube only: side-wall arc (chord = clear width, bulge out). `2.0` at 4 m height = semicircle; larger = flatter; omit/`0` = rectangle |
| `roof_thickness_m` | roof thickness (for a later mesh) |
| `extend_before_m` / `extend_after_m` | transition past GIP ends (axis) |
| `trim_s0_m` / `trim_s1_m` | metres to cut at GIP start / end (portal s0 / s1) |
| `open_side` | `left` \| `right` \| `both` \| `none` (tunnels default `none`) |
| `fitout` / `fitout_min_m` | ceiling lamps + jet fans in closed tubes (`auto` if bore ≥ 100 m); galleries stay empty |
| `lamp_spacing_m` / `fan_spacing_m` | 12 m midline lamps; 50 m fans over each lane |
| `one_ended` / `fake_end_drop_m` | one daylight portal; far end this many metres below portal Z |
| `append_unify` / `append_m` | one-ended: sequential XY of this unify past the portal; `append_m` = metres from the door (omit = full) |
| `abut_run_m` / `abutment_z` / `abut_snap_z` | last metres held at target Z (bridge pad); optional explicit Z |
| `force_deck_z` / `force_deck_z_fill` / `force_deck_z_sink_m` | under-slab heightmap to deck top (fill raises lows; sink 1–2 cm) |
| `approach_conform_side_cut` | drop empty terrain beside the slab (default drop = slab depth) |
| `side_cut_preserve` | types/OBJECTIDs that block that drop; see [STRUCTURES.md](STRUCTURES.md) |
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
