# Centerline shift by DGM roughness

Trial for the Imst main route. It does not write into a BeamNG level.

The question is where a measured carriageway width sits flattest on the raw
0.5 m DGM, and, where it never does, how much that width has to shrink.

Script: `tools/shift_centerline_by_roughness.py`.

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\shift_centerline_by_roughness.py
```

| Run | Folder |
|-----|--------|
| Lateral search only | `data/processed/tirol-imst-8192/centerline_shift/` |
| Lateral search, then narrowing up to 1 m | `data/processed/tirol-imst-8192/centerline_shift_narrow/` |
| Lateral search, then narrowing up to 2 m | `data/processed/tirol-imst-8192/centerline_shift_narrow_2m/` |
| Same, roughness clipped at 1.9 cm | `data/processed/tirol-imst-8192/centerline_shift_clip/` |
| Clipped run, width tapered into each pinch | `data/processed/tirol-imst-8192/centerline_shift_taper/` |
| Same, no capped pixel allowed in a kept placement (trial, rejected 27.09) | `data/processed/tirol-imst-8192/centerline_shift_strict/` |

Each run writes its own folder. Earlier folders stay as they are.

Since 30.09 the script writes `centerline_shift_taper/` with the mean-only rule
(`MAX_FRAC_HOT = 1.0`) for Landes-, Bundesstraßen, Autobahnen and `S-G` in one
run. `repair_dgm_transect.py`, `blend_bridge_deck.py` and `apply_corridor_dgm.py`
read that folder.

## Inputs

- GIP centerlines transformed to EPSG:31254 with the BEV grid. Main route only (`STR_CODE` matching `^[ABL]`, plus `OBJEKT` `S-A`).
- Measured width `width_mean_m` from `data/roads/gip_widths.json`.
- Raw corridor DGM `data/processed/tirol-imst-8192/corridor50_raw/corridor50_raw.tif`. The smoothed `filtered_7c_10d.tif` is not used.
- Bridge, tunnel and gallery pieces stay at offset 0. They are not neighbours for the matching step. The DGM there is not the deck.

## Roughness

Each DGM pixel stores the absolute difference between its height and the mean of the surrounding 1.5 m window (3×3 cells at 0.5 m). A uniform slope lies on that mean, so it is not an error. A break (edge, step, rock) is.

Before the search, roughness is clipped at 1.9 cm. That is the median, across pieces, of the lower of the two sides' 95th percentiles inside the ±1 m search at full width. Both directions of a typical piece already reach this level. Anything higher is a one-sided spike, such as a single rock pixel, and is cut down to 1.9 cm so it cannot outweigh a broader, milder slope. A pixel counts as hot once it sits on that cap.

## Lateral search

Each OBJECTID line is split into pieces of at most 20 m. A short tail is merged into the previous piece.

Each piece is moved on its own. Positive offset is left of the piece, from its first vertex to its last. The piece is translated as a rigid body: the ends are not bent toward the neighbours during the search. Offsets run from 0 outward in 10 cm steps to 1 m on both sides. Every offset is scored; the search does not stop early.

The sample area is a flat-capped buffer of the width under test (radius = half width). Flat caps keep the sample off the next piece. Inside that polygon the score is the mean roughness, plus the 90th percentile and the fraction of hot pixels.

A placement is acceptable when the mean is at most 0.8 cm and no pixel sits on the 1.9 cm cap. Capped pixels are the rough spikes. They no longer hide inside an acceptable mean. One placement is clearly best when it is at least 2 mm better than every other acceptable placement. If the best and worst means across the whole ±1 m search differ by less than 2 mm, the piece keeps the acceptable offset closest to the original axis. If none of those offsets is free of capped pixels, the piece is narrowed.

## Decision

1. A piece with a single clearly best acceptable offset is fixed.
2. A piece with several acceptable offsets takes the one nearest the mean of its already fixed neighbours. Example: previous neighbour +0.3 m, next neighbour +0.1 m, and +0.2 m is itself acceptable, so +0.2 m is kept. Otherwise the nearest acceptable offset is kept. A tie prefers the smaller absolute offset.
3. Neighbours are the previous and next non-structure piece of the same OBJECTID. Across OBJECTIDs, two pieces are neighbours only when an endpoint pair is within 3 m and the directions agree (dot product at least 0.85). The neighbour's sign is flipped when that piece runs the opposite way, so "left" stays left of the route.
4. A piece with several acceptable offsets but no fixed neighbour stays at offset 0. Its raw minimum is stored as `best_m` and is not applied.
5. A piece that is acceptable but has no lateral preference (`flat`) stays at offset 0.

## Narrowing

Narrowing runs before the neighbour pass, and only for a piece that is still not acceptable after every lateral offset. That is a piece whose every offset is either above 0.8 cm mean or still contains a capped pixel.

The width is reduced by 0.1 m and the full lateral search is repeated. A width that produces an acceptable placement is kept, together with the offset chosen at that width. A piece that is still too rough is narrowed again. The total reduction stops at 2.0 m. The last, narrowest attempt is what the buffers show even when it still fails (`passed = 0`).

Pieces that already fit, pieces outside the DGM, and structures are not narrowed.

## Centerline and carriageway

The kept offset is interpolated by station between piece midpoints. Each original vertex then moves along the left normal of its own local tangent. That is the centerline.

The carriageway is a ribbon along that centerline. Width is the accepted width of the piece under the station, except at a change of width: the ramp lies entirely on the wider piece, over up to 20 m, and reaches the narrower width at the start of the pinch. The pinch itself is not widened to meet the neighbour. Where two ramps overlap, the smaller width is kept, so a piece is never drawn wider than the width that passed the roughness test. `chosen_buffer` still shows the untapered per-piece width.

## Layers

`centerline_shift_taper/centerline_shift.gpkg` (earlier runs keep the same layers in their own folders):

| Layer | Contents |
|-------|----------|
| `segments` | Original pieces. `width_m` is the measured width, `width_used_m` the width that was kept, `narrow_m` the reduction, `kind` the decision. |
| `trials` | Every 10 cm offset at every width that was tried. `kept_width = 1` marks the width that was kept. `chosen = 1` marks the offset applied at that width. |
| `chosen` | Rigid shift of each piece by its kept offset. |
| `chosen_buffer` | Flat-capped buffer of `chosen` at the untapered `width_used_m`. |
| `centerline` | Smoothed shifted axis. |
| `carriageway` | Centerline ribbon. Width tapers on the wider side into each pinch. |

`01_roughness.tif` is the roughness raster. `report.json` holds the counts.

## Knobs

In `tools/shift_centerline_by_roughness.py`:

| Constant | Value | Meaning |
|----------|------:|---------|
| `SEG_MAX_M` | 20 m | Longest piece |
| `STEP_M` | 0.10 m | Lateral step |
| `MAX_SHIFT_M` | 1.0 m | Lateral search limit |
| `GOOD_MEAN_M` | 0.008 m | Acceptable mean roughness, on the clipped scale |
| `CLEAR_GAP_M` | 0.002 m | Gap required before one offset is "the" best |
| `WIDTH_STEP_M` | 0.10 m | Narrowing step |
| `MAX_NARROW_M` | 2.0 m | Largest width reduction |
| `TAPER_M` | 20 m | How early the wider piece starts narrowing into a pinch |
| `ROUGH_CLIP_M` | 0.019 m | Cap. Median of the quieter side's 95th percentile |
| `HOT_M` | 0.019 m | Pixel on the cap, counted in `frac_hot` |
| `MAX_FRAC_HOT` | 1.0 | Share of capped pixels a kept placement may contain. 1.0 = mean-only rule (kept); 0 = strict trial (rejected) |
