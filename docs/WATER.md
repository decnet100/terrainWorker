# Water (lakes and rivers)

Standing water still comes from landcover **LN-GWS** polygons (`WaterBlock` + optional basin carve). Flowing water uses the **Tirol Gewässernetz** centerlines, not the LN-GWF floodplain polygons (those stay Mud paint).

Source: Land Tirol, Gewässernetz / Fließgewässer, [CC BY 3.0 AT](https://www.cc.gv.at/index.php?id=209). FeatureServer (same host as GIP):

`https://services3.arcgis.com/hG7UfxX49PQ8XkXh/arcgis/rest/services/Fliessgewaesser/FeatureServer/0/query`

Native CRS is `EPSG:31254`. Configure URL, widths, slope cut, and seasonal stages in the **site YAML**.

## Pipeline

```powershell
cd C:\temp\beamng_autoroad
$env:AUTOROAD_SITE = "config/sites/fernpass.yaml"
python tools\fetch_waterways.py
python tools\build_water.py
```

`fetch_waterways` caches under `data/raw/waterways_<slug>_<hash>.geojson` and writes `data/processed/<slug>/waterways.geojson`. Re-download with `--force`.

`build_water` with `beamng.water.flowing: river` densifies each line, samples DGM Z, skips spans steeper than `river_max_slope_deg` (waterfall gap — BeamNG `River` is a sloped ribbon, not a falling sheet), and injects `River` objects into `MissionGroup/water/` (same place as the River Editor / official maps). Standing `WaterBlock`s stay under `MissionGroup/level_objects/Water/`. River names look like `river_Gurglbach_358_0`. Each River / WaterBlock gets the editor water look (`baseColor`, ripple/foam/depth textures, cubemap). Without those fields the mesh exists but does not render.

Quit BeamNG, then reload the level **without** saving the World Editor MissionGroup. If the basin carve ran, re-import `terrainPreset.json` once.

## Site YAML

```yaml
sources:
  waterways:
    type: featureserver
    url: "https://services3.arcgis.com/hG7UfxX49PQ8XkXh/arcgis/rest/services/Fliessgewaesser/FeatureServer/0/query"

beamng:
  water:
    enabled: true
    flowing: river          # none | river
    standing: waterblock
    river_node_step_m: 12
    river_snap_halfwidth_m: 10  # perp. search for lowest DGM (0 = official midline)
    river_snap_step_m: 1
    river_max_slope_deg: 35 # drop steeper spans (waterfall gap)
    surface_lift_m: 0.4     # river / lake surface above DGM
    waterlevel_z_offset_m: 0 # extra WaterBlock Z; negative buries lakes
    # Do not copy Fernpass -4 m here unless the DGM sits high on that site.
    river_min_length_m: 12
    river_min_width_m: 2
    river_max_width_m: 16
    river_depth_m: 1.2
    stage: spring           # bake / default runtime stage (high water)
    width_by_typ:           # GRKATWRRL catchment class (GEW_TYP is almost always Fließgewässer)
      "< 10 km² Gewässer": 4
      "10 km² Gewässer": 6
      "100 km² Gewässer": 12
      "1000 km² Gewässer": 20
    stages:
      spring:
        months: [3, 4, 5]
        surface_dz_m: 0.0
        width_scale: 1.0
        depth_scale: 1.0
        flow_mps: 2.5
      autumn:
        months: [9, 10, 11]
        surface_dz_m: -0.7
        width_scale: 0.7
        depth_scale: 0.7
        flow_mps: 1.2
```

Bake the **spring** bed (high water). Other stages only lower the surface / shrink width. `fetch_waterways` prints `GEW_TYP` counts so the width table can be filled from real values.

## Runtime stages

`build_water` writes `water_stages.json` next to the processed items and into the BeamNG user level folder. The Alpine Roadtrip session mod applies the stage for the session calendar month on load. Console: `extensions.alpinert.applyWaterStage("autumn")`.
