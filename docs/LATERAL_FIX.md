# Lateral fix of the GIP links (trial)

Moves the GIP routing links sideways onto the road in the DGM, measures the carriageway width from the
terrain, and flags bridges, tunnels and galleries per station. Built and checked on Pfunds-Spiss
(`config/sites/pfunds_spiess.yaml`, 41 % in Switzerland). Nothing else in the pipeline reads the output yet.

Main script: `tools/fix_lateral_rank.py`. Branch: `lateral-fix-rank` (from `junction-node-heights`).

## Run order for a border site

All commands with `$env:AUTOROAD_SITE = "config/sites/pfunds_spiess.yaml"`.

| Step | Command | Writes | Notes |
|------|---------|--------|-------|
| 1 | `python tools\fetch_dgm.py` | `data/raw/dgm_<site>_native.tif` | Needs `max_nodata_frac: 0.6` and `allow_incomplete: true` under `sources.dgm` (the Swiss part is nodata), and `resolution_m: 0.5`, `version: 1.0.0`. The cubic resample to 1 m afterwards needs more than 12 GB RAM; step 3 does without it. |
| 2 | `python tools\fetch_swissalti3d.py data\raw\swissalti3d_05 xmin ymin xmax ymax` | swissALTI3D 0.5 m tiles | bbox in the site CRS. |
| 3 | `python tools\build_mixed_dgm.py --swiss data\raw\swissalti3d_05 --borders data\raw\borders\swissBOUNDARIES3D_1_5_LV95_LN02.gpkg` | `dgm_<site>_mixed05.tif`, `dgm_<site>_mixed.tif` (+ `.aux.xml`) | Swiss model inside Switzerland, Tirol outside. Tirol lifted by the flat-overlap median (Pfunds: −0.063 m, so Tirol is raised 6.3 cm). Both products then blend linearly 30 m each side of the border. Set `GDAL_BIN` to `C:\OSGeo4W\bin`; do not put that directory on the parent `PATH` (rasterio then loads the wrong GDAL). |
| 4 | `python tools\fetch_bev_landcover.py --allow-incomplete` | BEV land cover | Water for `score_check_grade`. **Bug:** the WMS bbox is always sent northing,easting; in EPSG:25832 the server wants easting,northing and returns 100 % nodata. Fixed in the sandbox copy, not committed (see Open). |
| 5 | AV Graubünden | `data/raw/gr_av_bodenbedeckung_<site>.gpkg`, layer `Bodenbedeckung_ohne_Gebaeude_farbig`, field `art` | geodienste.ch WFS `ms:LCSF`, field `Art` renamed to `art`. Swiss water and the width checks. |
| 6 | `python tools\fetch_gip.py`, `python tools\gip_routing_load.py` | `gip_routing/gip_routing.gpkg` | Needs `data/raw/gip_routingexport/routingexport_ogd.txt` (open.gip.gv.at `A_routingexport_ogd.zip`). |
| 7 | `python tools\mask_dgm_by_links.py` | `dgm_link15/dgm_link15.tif` | |
| 8 | `python tools\score_check_grade.py`, `python tools\group_check_intervals.py`, `python tools\score_gap_chord.py` | `link_surface/check_grade/*` | `score_check_grade` imports `_to_working` from `export_gip_width_m.py`, which is not in the branch (see Open). |
| 9 | `python tools\fix_lateral_rank.py` | `lateral_fix/lateral_fix.gpkg` | ~3 min. `--dgm` overrides the 0.5 m DGM. |
| 10 | `python tools\check_lateral_fix_ch.py ...`, `python tools\check_lateral_width_ch.py ...` | printout | Swiss part only, against the cadastral carriageway. |

## What fix_lateral_rank does

**Ranks.** Links are corrected one road rank after the other, so a lower road ends on the corrected higher
one and a poor axis cannot pull a good one at a junction.

| Rank | Links | Search | Jump sigma / station | Smoothing | Pull to GIP |
|------|-------|-------:|---------------------:|----------:|------------:|
| B | edge category A, B, H | ±2 m | 0.05 m | 60 m | 4 × |
| L | category L, or FRC ≤ 4 | ±4 m | 0.10 m | 40 m | 1 × |
| Gemeinde | FRC 5-6 | ±6 m | 0.15 m | 25 m | 1 × |
| intern | FRC 7-8, 99 | ±8 m | 0.20 m | 20 m | 1 × |

Ways (FRC ≥ 10) are not moved; their ends follow the moved nodes over 30 m.

**Chains.** A chain runs through a node where only two links of the rank meet, or at a junction along the
straightest pair with the same street code. Longest chains first. A chain end on a node an earlier chain has
moved is held there (±0.3 m) and the end vertex set onto it.

**Centre.** Stations every 2 m, offsets every 0.1 m. Score per offset on the 0.5 m DGM: the central 80 % of a
band of the GIP width should be a plane (RMS, cap 0.15 m; eroded edges do not count), the cross slope should
break at both band edges (cap 0.6), plus the pull to the GIP line. Viterbi over the chain, then a Whittaker
smoother of the class wavelength. Structures and stations off the DGM score only the pull.

**Width.** With the centre fixed, each side separately (a crowned road breaks at the centre): the half band
stays on that side's cross line and breaks at the edge (local slope 1 m inside against 1 m outside). Viterbi
with a capped jump price (widenings stay possible), then a robust Whittaker smoother of 30 m weighted by the
knee strength; Tukey weights drop stations more than 0.6 m off the curve. An edge without a knee (< 15 %
slope change: asphalt beside a flat verge) keeps half the GIP width. Structures, widened by 6 m over abutments
and portals, take the median width of 10 stations on each approach, never less than the GIP width.

**Structures, per station.**
1. `<site>_structures.yaml` beside the site yaml wins (example: `config/sites/pfunds_spiess_structures.example.yaml`;
   `typ: bridge | tunnel | gallery | ground`, optional `from_pct`/`to_pct` along the link as digitised).
   `<site>_manual_invalid.txt` counts as whole-link bridges.
2. Between stations `check_grade` found on the ground (up to 15 each side, nearest within 60 m), a cubic through
   them is the road. DGM more than 1 m below: bridge; more than 2 m above: tunnel or gallery, named from the
   GIP label where there is one, else "Tunnel/Galerie". Inside a GIP label only intervals `check_grade`
   recovered onto its spline count as ground.
3. Otherwise the GIP label (`bridge_ref`).

**Output** `data/processed/<site>/lateral_fix/lateral_fix.gpkg`:

| Layer | Contents |
|-------|----------|
| `link` | all links, moved geometry; `corrected` 1 = moved, 0 = ends only |
| `station` | `offset_m`, `dp_offset_m`, `confidence` (not calibrated), `width_gip_m`, `half_left_m`, `half_right_m`, `width_m`, `edge_left_seen`, `edge_right_seen`, `structure`, `structure_typ`, `structure_src`, `dz_m` (DGM minus the line through the road) |
| `chain` | moved chain line, mean / max offset, anchored ends |
| `node` | moved nodes with `dx`, `dy` |
| `carriageway` | ribbon between both measured edges, per chain |
| `structures` | structure runs: `typ`, `src` (dgm / gip / manual), length, `dz_min/median/max_m`, `link_ids` - the review list for the manual file |

## Results on Pfunds-Spiss

Swiss part, against the middle and the width of the cadastral carriageway (AV `Strasse_Weg`), stations more
than 25 m from a swissTLM3D structure:

| | Centre: median / p90 (GIP → fix) | within 1.5 m (GIP → fix) | Width error median (GIP → fix) | Width within 1 m (GIP → fix) |
|---|---|---|---|---|
| B | 0.35 / 1.50 → 0.40 / 1.50 m | 90 → 90 % | 1.00 → 0.88 m | 46 → 55 % |
| L | 0.70 / 1.65 → 0.45 / 1.10 m | 87 → 95 % | 0.80 → 0.76 m | 62 → 63 % |

The GIP B axis in Switzerland is a swissTLM3D copy and already good; the 4 × pull keeps it there. The width only
beats the GIP class width where the cadastral edge is a terrain knee (B within 1 m: 57 % against 26 %). Ranks
Gemeinde and intern have no Swiss stations; in Austria they were only looked at on the orthophoto, where the
moved line sits in the carriageway.

Structures: 85 runs from the terrain (39 bridges, 12 galleries, 5 tunnels, 29 "Tunnel/Galerie"), 3 from GIP.
Against swissTLM3D most tunnels, galleries and bridges are found; missed are two long galleries (240 m, 381 m
at 32 %), one of 35 m and three bridges of 16-30 m. Galleries open to the valley show the road in the DGM.

## Findings on the way

- **WCS reprojection.** The Tirol WCS delivered in EPSG:25832 sits within ~0.3 m of swissALTI3D; a local warp of
  the M28 data through the BEV grid within ~0.5 m. No systematic shift. If GDAL does not find
  `at_bev_AT_GIS_GRID_2021_09_28.tif` in its PROJ data path it silently uses the 1.5 m Helmert and lands
  ~0.6 m off (`check_wcs_reprojection.py`, `fetch_tirol_m28.py`).
- **BEV land cover** returns nodata in EPSG:25832 (axis order, step 4).
- **GIP widths** are class constants (B, L 6.5 m; G, PS 5.0 m; Swiss links 7.0 m), not measurements.
- **GIP in Switzerland** follows swissTLM3D within ~1 m except at the Samnaun road tunnels (up to 150 m off),
  and carries no structures there.

## Border merge (Austria / Switzerland)

`tools/merge_border_network.py`, after `fix_lateral_rank.py` and `export_kunstbaute.py`, with swissTLM3D roads from
`tools/fetch_swisstlm3d_roads.py data\raw\swiss\tlm_strasse_<site>.gpkg xmin ymin xmax ymax`.

- **Border:** BEV national boundary (`data/raw/borders/AT_INSPIRE_BEV_AU_AB_epsg3416_20260402.gpkg`, unit "Staat",
  EPSG:3416 = ETRS89, so the step to 25832 is a projection only). On Pfunds-Spiss it lies 0.40 m (median, max
  0.74 m) from swissBOUNDARIES3D. Own pieces keep their part in Austria (23 km of GIP in Switzerland go),
  swissTLM3D and the AV their part outside. Own remnants under 3 m at the border are dropped.
- **Decisions:** `config/sites/<site>_border.yaml` holds ids only; every position is computed at run time.
  `join` (shared node where the TLM line crosses the border), `replace` (TLM lines take over up to the own node;
  the own road ends there, no width transition: the customs lanes are separate roads), `unite` (gravel ways,
  straight connector instead of a snap), `keep_tlm` (TLM lines kept whole, also in Austria).
- **Automatic proposal:** loose ends (no other line within 5 cm) within 10 m of the border. One own and one TLM end
  within 8 m pointing the same way (30°) join automatically; the rest goes to
  `border_merge/<site>_border_proposal.yaml`. Pfunds: no automatic join left (the file names them all);
  open own 3 (dead ends at the border), TLM 6 (Swiss ways and two 3m Strassen without an Austrian partner).
- **Width transition** at a join: own half widths go linearly to the AV half widths over 100 × the difference
  (5-80 m), per side. The AV width is taken at the first cross-section inside the AV carriageway that is clear
  of the border, because the AV polygon ends at the Swiss line, at a slant where the road crosses the border
  obliquely (B184: left 2.82 → 3.10 m over 28 m, right 3.89 → 3.35 m over 54 m).
- **Polygons:** own ribbons (no polygon on tunnels and galleries) in Austria plus the sliver up to the AV edge;
  AV `Strasse_Weg` / `Trottoir` / `Verkehrsinsel` outside Austria; TLM branches of a replace as strips of their
  class width in Austria.
- **GIP lanes:** GIP stores lanes per direction only; there is no single-lane two-way road. The customs link is
  1 + 1 lanes, 6.5 m, not split. The split around the container exists only in swissTLM3D.

Output `border_merge/border_merge.gpkg`: `line` (src own / tlm / connector), `node` (join / auto / replace / unite),
`carriageway` (src own / av / tlm, klass in Magliasina's names), `contact`, `border`.

## Plan and open work (for the next agent)

The plan agreed with the user, in five steps; 1 and 2 are done, the rest is open.

1. **Rank order with anchors** - done (`fix_lateral_rank.py`).
2. **Search range and stiffness per class** - done (table above). The values are set, not yet measured: take the
   95 % quantile of the found offsets per class plus a margin once more sites ran.
3. **Per chain: rigid shift first, then DP, and a calibrated confidence.** Open.
   - A rigid offset per chain (a few hundred metres) before the DP, so systematic digitising shifts are caught and
     the DP range can shrink.
   - `confidence` (best against best offset > 0.6 m away) is not calibrated; the "low confidence" share it gives
     means nothing yet. Calibrate it against the Swiss AV error, then keep the GIP line where it is low.
4. **Ways: retrace instead of shifting.** Open. Least-cost path between the corrected end nodes inside a ±25 m
   corridor of the GIP line; cost from cross slope / flatness of a 3 m strip, the edge knee, the canopy gap in
   DOM minus DGM (forest roads), and grade over the class limit (Weg 30-40 %). Then smooth.
5. **Validation.** Partly done (Swiss AV checks). Open: perturb swissTLM3D axes on purpose (rigid shift, wave, noise
   of the GIP size per class), run the fix, measure the recovery; orthophoto spot checks in Austria.

Further open points:

- **Width where the terrain shows no edge.** A second signal is needed: the orthophoto (asphalt edge) or the laser
  intensity (not open in Tirol). Fallback today: GIP class width.
- **Galleries open to the valley** are missed by the terrain test; set them in `<site>_structures.yaml`, or add a
  DOM test (roof above the road).
- **Junction surfaces.** Chains of the same rank meeting at a junction overlap; there is no carriageway partition
  yet. The Magliasina hand-over needs one (see `docs/GIP_MAGLIASINA.md` on branch `gip-magliasina-review`).
- **Swiss roads at the border.** Done as a trial: `tools/merge_border_network.py` (see the section below). Still
  open: the bridge over the Schalklbach at the customs post has no polygon between the BEV line and the AV
  carriageway (Magliasina builds bridge decks from the line); same-rank overlaps at junctions stay.
- **Not committed sandbox fixes:** the axis order in `fetch_bev_landcover.py` (step 4) and a stand-in
  `_to_working` in `export_gip_width_m.py` (the real one exists only locally with the author).
- **Pipeline wiring.** `fix_lateral_rank.py` is not in `pipeline_catalog.py` / `pipeline_gui.py`, and no build step
  reads `lateral_fix.gpkg` yet.
