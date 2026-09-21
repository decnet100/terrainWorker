# Site data

## Short answer

**WCS links are enough**, if you also provide:

1. **BBOX** of the test crop (coordinates + EPSG)
2. **Layer / coverage names** (if GetCapabilities is ambiguous)
3. Optional: desired **resolution** (e.g. DGM 0.5 m, ortho 0.2 m, or coarser for tests)

You do **not** need to drop multi-GB GeoTIFFs into chat. `tools/fetch_dgm.py` (and related fetch scripts) download into `data/raw/`.

Alternatively: put already-downloaded tiles in `data/raw/` and point `config/site.yaml` at local paths.

---

## Recommended format: `config/site.yaml`

Template: [`../config/site.example.yaml`](../config/site.example.yaml)

Finished profiles live under [`../config/sites/`](../config/sites/README.md). Activate with `$env:AUTOROAD_SITE` or by copying onto `config/site.yaml`.

```yaml
name: my-test-pass
crs: EPSG:31254          # or 31255 — match the DGM tile
bbox:                    # xmin, ymin, xmax, ymax in the CRS above
  - <<<<<<<
  - <<<<<<<
  - <<<<<<<
  - <<<<<<

sources:
  dgm:
    type: wcs
    url: "https://.../WCS"
    coverage: "..."      # coverage / layer id
    resolution_m: 0.5
  ortho:
    type: wcs
    url: "https://..."
    coverage: "..."
    resolution_m: 0.2    # first try: 1.0 or 2.0 is often enough
  landuse:
    type: wfs             # or local
    url: "https://..."
    type_name: "..."
  roads:
    type: osm             # or gip / local gpkg
    # bbox from site.bbox
```

A chat message can be minimal:

> Test crop: EPSG:31254, BBOX=[…].  
> DGM WCS: `https://…` Coverage `…`  
> Ortho WCS: `https://…` Coverage `…`

---

## What lives where

```text
C:\temp\beamng_autoroad\
  config\site.yaml          # your manifest (do not commit if private)
  config\site.example.yaml
  config\sites\             # named profiles
  data\raw\                 # original downloads (large, gitignore)
  data\processed\           # heightmap, road JSON, masks
  data\roads\               # central GIP catalog (widths, not-tunnel)
  data\annotations\         # GPX/GeoJSON/GPKG: tunnels, edges, …
  docs\
```

---

## Annotations (GIS)

Binding schema: [`ANNOTATIONS.md`](ANNOTATIONS.md)

Short:

| Layer | Geometry | Role |
|-------|----------|------|
| `road_edge` | LineString | precise carriageway edge (`side`: left/right) |
| `guardrail` | LineString | rail axis; **gaps = openings** (junction, …) |
| `centerline` | LineString | optional axis correction |

File: `data/annotations/<site>.gpkg` (CRS = site CRS). Templates: `data/annotations/*.geojson`.

Google Earth screenshots for orientation only; geometry as vectors, not just a picture.

---

## WCS practice (Tyrol)

Official entry points:

- [tiris geodata services](https://www.tirol.gv.at/statistik-budget/tiris/tiris-geodatendienste/)
- [DGM Tyrol on data.gv.at](https://www.data.gv.at/datasets/0454f5f3-1d8c-464e-847d-541901eb021a)
- [Laser scan / terrain WCS](https://www.tirol.gv.at/sicherheit/geoinformation/geodaten-tiris/laserscandaten/)
- [Orthophotos WCS](https://www.tirol.gv.at/sicherheit/geoinformation/geodaten-tiris/orthofotos/)

Paste the **concrete GetCapabilities / WCS URLs** from the catalogue or QGIS “Layer properties → Source” — endpoints change from time to time.

Check:

```text
…/WCS?SERVICE=WCS&REQUEST=GetCapabilities
```

In QGIS: add WCS → export a crop also works; then set paths under `sources.*.type: local`.

---

## Size / first try

| Dataset | Recommendation for a first test |
|---------|----------------------------------|
| Crop | ~0.5–2 km edge length around the road |
| DGM | 0.5 m if feasible, else 1 m |
| Ortho | 1–2 m first; 0.2 m only if needed |
| DOM | optional, artefact check only |

Prefer a small, good crop with a visible **rock / forest ridge** over a whole pass.
