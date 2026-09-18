# Concept: geodata → BeamNG pass roads (Tyrol)

## Goal

A semi-automated pipeline from public Tyrolean geodata to a **playable BeamNG.drive level** for alpine pass roads, then a multi-map tour (**Roadtrip Tyrol**) between those levels.

**In scope today:** road surface, terrain, materials, structures (bridges / galleries), ridge silhouette, first portal switch.

**Still later:** buildings / villages, a single streamed world, race UI across maps.

Other sims (Assetto Corsa, AC Rally UE5, AMS2, rF2) are out of scope.

---

## Why a naive DGM→mesh fails

A 0.5 m DGM often produces **slopes** instead of hard road edges (guardrails, gabions, retaining walls). A DSM (DOM) carries cable-car / vegetation spikes. Tunnels and galleries are missing from the heightfield.

So:

- Carriageway as its **own road** (spline + width / profile)
- Terrain beside it with limited terraforming
- Vertical features and structures as annotation + props / meshes
- Tunnels: human XY path + **cubic-Hermite Z** from portal heights and approach grades

---

## Data sources

| Dataset | Role | Access |
|---------|------|--------|
| DGM 0.5 m | Heightmap, road heights | WCS / tiled GeoTIFF |
| DOM 0.5 m | Artefact / vegetation height (optional) | WCS |
| Orthophoto ~20 cm | Texture / mask aid | WCS / download |
| GIP / OSM | Centerline; OSM `lanes`×3.75 m → width (default 2×3.75 = 7.5 m). Fernpass Mega uses **GIP** for the axis. | OGD / Overpass |
| Tyrol land use | Forest vs rock, coarse materials | WFS / download |

Google Earth is a **visual reference** while annotating, not a texture source (licence).

Typical CRS: MGI GK West/Central (EPSG:31254 / 31255), heights in GHA.

---

## Pipeline (overview)

```text
OGD (WCS/WFS/OSM/GIP)
    → Ingest + a single metre CRS
    → Centerline (editable in QGIS)
    → Road JSON (x,y,z,width) + DGM heightmap 16-bit PNG
    → Heightmap layers (water / road-bed / span) → composed PNG
    → Surface segments → terrain paint + groundmodels
    → Forest/rock mask → ridge impostors (canopy alpha)
    → Structures (MeshRoad decks, gallery DAE, hole maps)
    → BeamNG level (World Editor import)
```

Heightmap mixer (raw DGM stays untouched): [HEIGHTMAP_COMPOSE.md](HEIGHTMAP_COMPOSE.md).

### Road and edges

- Spline nodes with height from the DGM (carriageway lightly smoothed)
- Terraforming with a small margin — do not smear embankments upward
- Guardrails / walls are **not** terrain slopes (props / section meshes)

### Surfaces (not an AC surface INI)

1. **Physics:** groundmodels under painted terrain (`ASPHALT`, gravel variants, `roughnessCoefficient`)
2. **Look:** a few decal / terrain materials
3. **Macro:** DGM undulation; no wall-to-wall high-poly grain

DecalRoads are often look / navigation; grip usually comes from the terrain material underneath.

### Ridge silhouette

- Mask: land use forest vs rock (+ slope)
- Rock ridge: hard terrain material, **no** canopy cards
- Wooded slope: **alpha impostors / transparency** (Forest, no collision) for a broken horizon — no need for expensive unique trees
- Close to the road, real Forest meshes are optional later
- Beyond the playable heightmap: **backdrop rings** (near DGM / mid-far Copernicus) — [BACKDROP.md](BACKDROP.md)

### Tunnels / galleries

Input: horizontal centerline between portals.  
Height: cubic Hermite along arc length from portal Z + approach grade \(dz/ds\).  
Mesh loft + terrain holes at portals. Galleries: mix DGM where the side is open. Site YAML: [GIP.md](GIP.md).

### Buildings (later)

Footprint → category (housing, church, commercial, …) → variant pool → TSStatic near / proxy far.

---

## Robustness checks

1. Short open road (~300–800 m)
2. Section with a hard edge (wall / guardrail)
3. Visible ridge: rock vs broken forest horizon

Accept: starts in BeamNG, drivable, sensible silhouette, no cable-car spikes.

---

## Tech stack

- Python + GDAL/rasterio + geopandas/shapely + numpy
- QGIS for centerline / annotation
- BeamNG World Editor (heightmap, roads, painter, Forest)
- Blender only for billboard textures / later meshes

Working directory: `C:\temp\beamng_autoroad`

---

## Multi-map: Roadtrip Tyrol

Pass levels link with a hard switch and a session file (environment + vehicle config). The terrain pipeline stays independent.  
Status and commands: [ROADTRIP_TYROL.md](ROADTRIP_TYROL.md).

---

## Next steps

See [STATUS.md](STATUS.md) (current state + prioritized roadmap).

Short list:

1. QGIS: fine-tune guardrail gaps / edges (the build already reads the GPKG)
2. Fold `road_edge` into terrain masks
3. Soft cover toward moss / alpine meadow; geology / LISA later
