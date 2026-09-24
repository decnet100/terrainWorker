# Authorities (map countries / provinces)

A site crop must name every **catalog authority** its `crs` + `bbox` cover.
Road layers are not fetched or built until that list is set. Empty or
`leave_empty` aborts on purpose.

Catalog: [`config/authorities.yaml`](../config/authorities.yaml).
Site template: [`config/site.example.yaml`](../config/site.example.yaml).

## New site (short)

1. Copy the template, set `name`, `crs`, `bbox` (and DGM/ortho sources).
2. Fill `authorities` from the crop — do not type NUTS codes by hand:

```powershell
cd C:\temp\beamng_autoroad; python tools\fill_site_authorities.py --site config\sites\NEU.yaml --write
```

Same action: pipeline GUI → **Authorities**.

3. If the crop sits fully in known regions, the script writes e.g.
   `authorities: tirol` or a list. If a remainder lies outside the catalog,
   you get a warning and a dialog to add that NUTS region.

Missing `authorities` or `authorities: leave_empty` → no GIP / road load.

## Catalog

| Key | NUTS 2024 (level 2) | Default CRS | Road-id offset |
|-----|---------------------|-------------|----------------|
| `tirol` | AT33 | EPSG:31254 (MGI GK West) | 0 |
| `suedtirol` | ITH1 | EPSG:25832 (UTM 32N) | +10 000 000 |
| `schweiz` | CH01–CH07 | EPSG:2056 (LV95) | +20 000 000 |

Stride is 10 000 000. YAML `objectid` (gip_extra, items, spine_next, unify)
is the **level** id = offset + local service id. The service id is stored as
`SOURCE_OBJECTID`. A value from another band aborts.

A short site form inherits CRS, offset and land-cover mapper from the catalog:

```yaml
authorities: tirol
```

```yaml
authorities:
  - tirol
  - suedtirol
```

A mapping is only needed for extras (foreign roads file, grid, validity
polygon, class table):

```yaml
authorities:
  tirol: {}
  suedtirol:
    roads: data/raw/suedtirol_roads.geojson
    roads_crs: EPSG:25832
```

## Detection (GISCO NUTS)

Boundaries: [Eurostat GISCO NUTS 2024](https://gisco-services.ec.europa.eu/distribution/v2/nuts/nuts-2024-files.html),
polygons, 1:1 million, EPSG:4326, level 2.

```text
https://gisco-services.ec.europa.eu/distribution/v2/nuts/geojson/NUTS_RG_01M_2024_4326_LEVL_2.geojson
```

First run downloads into `data/boundaries/NUTS_RG_01M_2024_4326_LEVL_2.geojson`
(gitignored, ~16 MB). Later runs reuse that file.

The site bbox is transformed from the authored CRS to WGS84 and intersected
with the NUTS polygons of each catalog key. Coverage is measured in
EPSG:3035 (equal area). A leftover ≤ 0.2 % of the crop is treated as
complete (generalisation slivers at the border).

If the NUTS file is not present, detection falls back to the coarse
`wgs84_ring` in the catalog. Fill / GUI always download NUTS first.

Checks without writing:

```powershell
cd C:\temp\beamng_autoroad; python tools\fill_site_authorities.py --site config\sites\imst.yaml
```

Write known keys into the YAML:

```powershell
cd C:\temp\beamng_autoroad; python tools\fill_site_authorities.py --site config\sites\imst.yaml --write
```

Add a catalog row from a NUTS id (e.g. Vorarlberg AT34) and then fill again:

```powershell
cd C:\temp\beamng_autoroad; python tools\fill_site_authorities.py --add-key vorarlberg --nuts AT34 --crs EPSG:31254
```

Omit `--offset` to take the next free 10 000 000 band.

## Incomplete coverage

The crop is **not** accepted as “known only” when part of it lies outside
the catalog (example: Bregenz → AT34 Vorarlberg). Then:

- CLI (interactive terminal): lists the NUTS leftovers and asks whether to
  create a catalog entry (key, NUTS-ID, CRS, offset).
- GUI **Authorities**: same list; pick a row → key + CRS → append to
  `config/authorities.yaml` and rewrite the site keys.
- Non-interactive CLI: print the leftovers; create with `--add-key`.

Until the leftover is in the catalog **and** listed on the site, road
fetch/load stays blocked if that remainder maps to a catalog key that the
site omitted. A remainder with **no** catalog key is a warning at fill
time; `require_road_authorities` only demands the keys that the detector
already knows.

## GIP and datum

GIP is requested in the authority’s local CRS (Tirol: EPSG:31254), never
EPSG:4326. Into another working CRS (UTM / ETRS89) the official BEV grid
is used: `data/grids/at_bev_AT_GIS_GRID_2021_09_28.tif` (PROJ CDN, gitignored).
An explicit `authorities.*.grid` overrides that. A missing grid for a
datum change aborts — no silent Helmert / WGS84 hop.

Cached GIP GeoJSON is stamped on ingest: `OBJECTID` = level id,
`SOURCE_OBJECTID` = service id. FeatureServer queries use the source id.

Details: [GIP.md](GIP.md).

## Tools

| Tool | Role |
|------|------|
| `tools/fill_site_authorities.py` | Detect, write site YAML, add catalog row |
| `tools/pipeline_gui.py` → **Authorities** | Same, with dialog |
| `tools/authorities.py` | Catalog, NUTS, id bands, stamp, self-check |
| `tools/fetch_gip.py` | Calls `require_road_authorities` before download |
| `tools/gip_road_segments.py` | Same before loading the cache |

```powershell
cd C:\temp\beamng_autoroad; python tools\authorities.py
```

prints `authorities self-check ok` when the id and catalog tests pass.
