# GIS annotations (schema)

Hand-edited layers for road edge, guardrails, and gaps (junctions).  
OSM/DGM stay the **raw layer**; these files override the heuristic once the pipeline reads them.

## File and CRS

| | |
|--|--|
| Path | `data/annotations/<site>.gpkg` (preferred) or individual GeoJSON under `data/annotations/` |
| CRS | as `config/site.yaml` → `crs` (smoke test: **EPSG:31254**) |
| Editor | QGIS; orthophoto + DGM as background |
| Create | `python tools/init_annotations_gpkg.py` → empty layers `road_edge`, `guardrail`, `centerline` |

One GeoPackage per site, layer names exactly as below.

`config/site.yaml`:

```yaml
annotations:
  gpkg: data/annotations/tirol-m28-test-500m.gpkg
  guardrail_source: auto   # auto | gpkg | heuristic
  # seed_sample_step_m: 2.0
```

## Two-step workflow

```text
1) python tools\seed_annotations.py [--force]
      → heuristic writes a draft into the GPKG (centerline, road_edge, guardrail)

2) QGIS: move / split lines / leave gaps at junctions
      → present=false or missing line = no rail

3) python tools\build_guardrails.py
      → reads layer guardrail (auto), builds 3D; never writes the GPKG
```

| `guardrail_source` | Behaviour |
|--------------------|-----------|
| `auto` (default) | GPKG if `guardrail` has features, else heuristic |
| `gpkg` | GPKG only (error if empty) |
| `heuristic` | OSM offset only (ignore GPKG) |

### Seed (heuristic → GPKG)

```powershell
cd C:\temp\beamng_autoroad; python tools\seed_annotations.py
```

```powershell
cd C:\temp\beamng_autoroad; python tools\seed_annotations.py --force
```

Writes `centerline`, `road_edge` (`width/2`), `guardrail` (`width/2 + lateral_extra`) in the site CRS.  
**`build_smoke.py` / `build_guardrails.py` never call this** and do not overwrite the GPKG.

---

## Layers

### 1. `road_edge` — precise carriageway edge

**Geometry:** `LineString` (2D; Z optional, else from DGM)

Digitise the **visible asphalt / carriageway edge** (not the guardrail).  
Left and right separately; orientation is free, `side` is authoritative.

| Attribute | Type | Required | Values / meaning |
|-----------|------|----------|------------------|
| `id` | text/int | yes | stable key |
| `side` | text | yes | `left` \| `right` (driving direction of the matching centerline) |
| `road_ref` | text | no | OSM way id or local road name |
| `source` | text | no | `ortho` \| `survey` \| `derived` |
| `notes` | text | no | free |

**Later use:** asphalt mask / shoulder / lateral zero line instead of `width/2`.

---

### 2. `guardrail` — rail path and presence

**Geometry:** `LineString` along the desired **rail axis** (post line).  
Draw only where a rail should stand. **Gaps = openings** (junction, driveway, bus bay) — no extra polygon needed.

| Attribute | Type | Required | Values / meaning |
|-----------|------|----------|------------------|
| `id` | text/int | yes | stable key |
| `side` | text | yes | `left` \| `right` |
| `present` | boolean | yes | `true` = build; `false` = deliberately no rail (rarely needed if the line is missing) |
| `kind` | text | no | `wbeam` (default) \| `concrete` \| `gabion` \| `none` |
| `gap_reason` | text | no | at a gap / `present=false`: `junction` \| `driveway` \| `bus_stop` \| `bridge_joint` \| `other` |
| `road_ref` | text | no | assignment to the road |
| `section_m` | real | no | desired section length (default from `site.yaml`) |
| `notes` | text | no | free |

**Rules:**

1. Line = where rails stand. No line / gap in the line = opening.
2. `present=false` only for explicit “no heuristic here” blocks on a short stub.
3. `side` consistent with `road_edge` on the same roadside.
4. Do not offset the centerline and commit that if the edge already lives in `road_edge` — the pipeline can derive an offset from edge + `lateral`; **prefer** the edited `guardrail` line as truth.

**Later use:** `build_guardrails.py` reads these lines instead of OSM offset; heuristic only where the layer is missing.

---

### 3. `centerline` — optional axis correction

**Geometry:** `LineString`

| Attribute | Type | Required | Values |
|-----------|------|----------|--------|
| `id` | text/int | yes | |
| `width_m` | real | no | overrides OSM width |
| `highway` | text | no | `secondary` … |
| `notes` | text | no | |

Only needed if the OSM axis is badly wrong. Otherwise omit.

---

### 4. `gallery` / `bridge` / `tunnel` — derivable from GIP

Raw data: Tyrol road WFS — see [GIP.md](GIP.md).  
Fields `KUNSTBAUTEN` + `OBJEKT` / `OBJEKTBEZEICHNUNG` mark galleries and bridges.

| Attribute | Type | Required | Values |
|-----------|------|----------|--------|
| `id` | text | yes | stable (e.g. GIP OBJECTID) |
| `kind` | text | yes | `gallery` \| `bridge` \| `tunnel` \| `culvert` |
| `name` | text | no | from `KUNSTBAUTEN` |
| `open_side` | text | no | `left` \| `right` \| `both` \| `none` (gallery) |
| `notes` | text | no | |

Geometry starts as the GIP line segments; refine portals / cross-section later.

### 5. Reserved

| Layer | Geometry | Purpose |
|-------|----------|---------|
| `exclude` | Polygon | cable-car / DSM artefacts out of masks |
| `wall` | LineString | retaining wall / gabion (own mesh) |

---

## QGIS workflow (short)

1. `python tools\seed_annotations.py` — heuristic as draft (only if empty; else `--force`).
2. Load ortho + DGM, project CRS = site CRS; open GPKG layers.
3. Move / split `guardrail`, leave gaps at junctions (`present=false` optional).
4. Junction: break the line (**two features**).
5. `python tools\build_guardrails.py` — 3D from GPKG (`source=gpkg` in meta).

**Safety:** seed without `--force` aborts if features already exist. Builds do not write the GPKG.

`road_edge` / `centerline` are for later masks / axis correction; the guardrail build currently uses only `guardrail`.

---

## Pipeline roles

| Step | Behaviour |
|------|-----------|
| `seed_annotations.py` | heuristic → GPKG (explicit only, `--force` to replace) |
| QGIS | user edits |
| `build_guardrails.py` | reads `guardrail` (auto); **writes no GPKG**; fallback heuristic if empty |
