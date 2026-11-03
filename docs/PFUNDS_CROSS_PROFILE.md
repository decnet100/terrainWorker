# Pfunds carriageway placement — handoff

Stop. Do not implement, rerun, mesh, or "improve" anything in this note. The cross profile is unsolved. Several assistants have already guessed the next parameter and made the placement worse. Guessing again will fail. Ask the user what they want done with a file before writing code. A finding is not a task.

The user has rejected the current direction. Searching the 0.5 m DGM for flat carriageway samples, and treating "flat everywhere" as the first goal, is the wrong problem. Their words: the main problem is the cross profile, and making flat roads everywhere first is stupid. They also said the written rules are not actually clear to an assistant and cannot be. The German text in `.cursor/rules/querprofil-lage.mdc` records one rejected attempt. It is not permission to code.

## Site

`config/sites/pfunds_spiess.yaml`. Working CRS is EPSG:25832. Do not reproject this site into MGI / EPSG:31254.

| | |
|---|---|
| Name | `pfunds-spiess-8192` |
| Level | `pfunds_spiess` |
| Center | 612243 / 5199204 |
| Size | 8192², 1 m per sample |
| Bbox | 608147.5, 5195108.5, 616338.5, 5203299.5 |
| Authorities | `tirol`, `schweiz` |

Austrian GIP routing does not cover the Swiss part of the frame (about 41 %). Swiss DGM fetch is a TODO in the YAML. Do not implement it. Do not refetch the full Austrian DGM.

Terrain used by the placement tools is the raw corridor raster `data/processed/pfunds-spiess-8192/corridor50_raw/corridor50_raw.tif`, 0.5 m cells. The site YAML points `sources.dgm.path` at `data/raw/dgm_pfunds-spiess-8192_mixed.tif`. Those are not the same file.

Off unless the user continues the road path: forest, buildings, ortho, decals, tunnel tubes, gallery shells, bridge slabs. The failed Imst bridge experiment (`blend_bridge_deck.py`, DOM deck) stays stopped. Do not mesh. Do not run `apply_corridor_dgm.py`. World Editor must not be saved when inject is the source. After Lua, DAE, or inject changes, BeamNG has to be quit and restarted.

## What the road is

The network is built only from GIP routing links. The link is the road piece. The node is the junction. Joining Tirol WFS `Verkehrswege` features by a 3 m gap, or chaining them by angle, is the wrong identity. `tools/build_ranked_carriageway.py` is that old path. Do not "correct" it unless the user asks. The link product is `tools/build_link_surfaces.py`.

Routing extract, from the national IDF `data/raw/gip_routingexport/routingexport_ogd.txt`, window = site bbox + 300 m:

- `data/processed/pfunds-spiess-8192/gip_routing/gip_routing.gpkg` (layer `link`, EPSG:25832)
- `data/processed/pfunds-spiess-8192/gip_routing/gip_routing.json`

875 links, 773 nodes. Loader: `tools/gip_routing_load.py`. Link geometry may carry Z. Length for width stations is 2D. 691 links have a carriageway width. 184 have none (paths, not chained, no surface). 454 lack a street code.

Width is `LINEAR_USE` base type 1, `WIDTH_AVERAGE`. Do not use the truck tables for carriageway width or for soffit height. The link surfaces use that width. They do not use the old class widths. The user does not want the 8 m class width on the B184 links.

Functional class (FRC) is the hierarchy. A smaller integer is higher. `EDGE_CATEGORY` (B/L/G/R/W/…) is the legal class and is not this column. `S-B` / `S-L` strings do not exist on the routing link. Tunnel, bridge, and ramp are `FORM_OF_WAY` and reference objects. The short `code` column is an equivalence table for this Austrian extract only. Switzerland and Italy would later get their own rows into the same rank order, not these letters.

| FRC | Code in this extract | Meaning the user locked | In this frame |
|---|---|---|---|
| 0 | `S-A` | transnational | none |
| 1 | `S-B` | transregional | 88 links. B180 Reschenstraße, B184 Engadiner Straße, 3 unnamed Hauptstraße (edge category H). Longest `short_id` 1452874, about 5.1 km |
| 2 | none | zentralörtlich | none |
| 3 | none | regional | none. Do not stamp `S-L` on "regional" |
| 4 | `S-L` | Gemeindeverbindung, not regional | L348 Spisser Straße, about 7.1 km, 28 links. L66 Spisser Dorfstraße, about 0.6 km. Plus 7 unnamed edge-category R links, about 10.7 km. Longest `short_id`s 1552408, 1552475, 1551498, 1551500, 1497794 |
| 5 | `S-G` | innerörtlich | none. Do not stamp `S-G` on this |
| 6 | `Sammelstrasse` | its own class, not a path and not innerörtlich | 48 links, all edge category G, no street names |
| 7 | `S-GW` | interne Erschließung in this table | 122 links. This is not the old repo meaning of `S-GW` as Wirtschaftsweg |
| ≥ 8 | `Weg` | Wirtschaftsweg, Forstaufschließung, Rad-/Fußweg, Alm, Wander, rest | 562 links. Wirtschaftsweg is FRC 11, code `Weg` |

Old class widths still live in `tools/gip_road_segments.py` and `.cursor/rules/gip-fahrbahn-defaults.mdc` for the WFS path. They are not the link-surface widths.

Lanes: −1 means not applicable. Lanes 1/0 is not "two lanes". Do not treat it as such.

## Junctions

User rule, also in `.cursor/rules/junction-knick.mdc`. This part has not been withdrawn. Do not re-explain it, and do not replace it with a new geometry.

At a node with three or more links of the same functional class, the pair with the smallest deviation from 180° is the through road. The other same-class links are side arms. The through road keeps its geometry, including a kink. Do not replace the kink with a straight chord. Do not pull points back 20 m (`STRAIGHT_M`) and connect them. A kink is legitimate. Turning a kink into a curve is a later, separate step.

The junction point lies on the through road's centerline, where the extension of a side arm hits it. Several side arms: the mean of those hit positions, measured along the through centerline. The terrain fit also moves the through road at the junction. Do not discard that shift and do not snap the axis back onto a previously built straight line. Side arms end on the fitted through centerline. A lower class arriving at the same node takes that point.

After every narrowing or search round, seat junctions again. The through axis is not pulled by the side arms.

Implementation in `tools/build_link_surfaces.py`: `_plan_junctions` picks the through pair and does not edit the line. After the fit and `_smooth_curve` (8 passes, weight 0.55; this smooths the fitted line, it does not replace a kink with a chord), side-arm rays (`_ray_hits`, `BEARING_M` 15 m, `HIT_MAX_M` 40 m) hit the fitted polyline. `_snap_segment`, `_settle_junctions`, `_pin_known_ends`, `_insert_joint`, and `STRAIGHT_M` were removed. Do not put them back.

Same-rank overlap of at least 1 m² (`JUNCTION_MIN_M2`) is layer `junction`. The side ribbon's flat cap crosses the through carriageway because the side axis ends on the through centerline. Which height wins in a same-rank overlap is not decided. A lower rank polygon is `difference` of all higher raw ribbons. Parts under 1 m² (`SLIVER_M2`) are not written.

Known node, do not rebuild the old chord: `short_id` 895760 at 614961.722, 5200951.617, degree 3. Links 1392991 (bearing 206.8°, 176.9 m), 1113816 (Rampe, bearing 4.7°, 47.5 m), 1111050 (bearing 59.3°, 16.9 m). Straightest pair is 1113816 × 1392991, about 20.1° off straight. 1111050 is the side arm. There is no link `short_id` 111050. Do not cite Verkehrswege object ids 3400 or 3408. A one-off script bug (`link_by_short = {rec["short_id"]: lid for rec in links.values()}` captured the last loop variable) produced those wrong ids.

## The cross profile, as the user stated it

The regression line describes the cross profile. It is not the template and not the target. The same is true of a cubic curve. The search must not minimise residual to the line or to the cubic.

A placement stays when both of these hold:

- The cross slope is possible for a carriageway. Several metres of edge-height difference is not a placement.
- Roughness of the ground on the carriageway stays low. Roughness is absolute deviation from the local neighbourhood height (1.5 m, 3×3 mean of the raw DGM), not the cross slope itself.

Edge measurement used by `tools/score_link_surfaces.py`: stations every 4 m (`STATION_M`), edges inset 0.25 m (`EDGE_INSET_M`), left = positive normal `(−dy, dx)`, samples across every 0.5 m (`ACROSS_STEP_M`). Structure spans within 1 m (`STRUCT_SKIP_M`) are excluded. `CROSS_MARK_M` = 0.50 m was the entry threshold the user accepted as "uncontroversially bad", especially on a 4 m road. They said optimisation can go further later. 0.50 m was not claimed to be a good road.

Stored width holds until the next break. `_width_at` linearly interpolates and must not be used on the sparse `width_along` list. `_held` holds. `_vertex_widths` inserts a duplicate point at each break so the ribbon steps. Using `_width_at` on the sparse list made 4.0 m climb to 5.0 m over hundreds of metres, grew the polygon, and made the before/after comparison lie.

Narrowing, if it is used at all, is regional. One station does not narrow a whole chain. A narrowing is kept only where more than two stations in the same vicinity (gaps of at most 24 m) benefit from the same reduction (about 10 or 20 cm). Apply the median. If the median falls between steps, use the weaker reduction. One width on that stretch. Adjacent unnarrowed stretches taper over 12 m. No step in width. Lateral offset changes by at most 1 m over 12 m (`_smooth_offsets`), clamped from both ends. That 12 m ramp was an implementation choice tied to the width taper. Say so if asked. Do not invent the next distance.

A stretch that would become narrower than half its original width stops, and is written as a problem. Width never increases. After each round, junctions are seated again.

Not implemented, and not to be implemented until the user says so:

- Manual widths. The user named `short_id` 111050. That link does not exist. Nearby is 1111050 (object 4344) and 1113816, both to be treated as single-lane about 4 m. 1392991 as 6.5 m two-lane. At a 4 m → 6.5 m merge, shared points are the carriageway edges, not the centerline. The narrow centerline is offset by (6.5 − 4) / 2 = 1.25 m so one edge is continuous. GIP width on all of these is 6.5 m. 1111050 and 1392991 are lanes 1/0. 1113816 is −1/1 and `FORM_OF_WAY` Rampe. 1392953 and 2632796 are 1/1.
- Width jumps. After detection, a short width change under 50 m that differs by ≥ 1.5 m is invalid. A jump is the change between direct neighbours. Which long side wins an abrupt ≥ 1.5 m step between two long widths is not decided.

## What was built, in order

All of these gpkg files are under `data/processed/pfunds-spiess-8192/link_surface/`. QGIS often locks a gpkg (`WinError 32`). Writers then fall through to the next filename. Do not treat a fallback file as the adopted product.

| File | What it is | Status |
|---|---|---|
| `surface.gpkg` | Unfitted ribbons from the GIP lines | Early. The user said these looked better at one point. Not the current axis |
| `surface_next.gpkg` | Next filename while a gpkg was locked | Not current |
| `surface_junc.gpkg` | Chord junctions. Through road replaced by a straight segment | Rejected. "Das erzeugt ja in solchen Fällen immer komplett neue Straßenverläufe" |
| `surface_curve.gpkg` | Chord, then the fit snapped back onto that chord | Rejected. Wall mouth (fid 28 / 38) sat in the wall because the 20 m chord was kept and the terrain shift was thrown away |
| `surface_meet.gpkg` | Kink kept. No `width_m` on `axis_fit` because the file was locked during the stamp | Geometry ancestor of the next file. Do not adopt it as the scored surface |
| `surface_width.gpkg` | Same kink-preserving geometry as `surface_meet`, plus `width_m` and `width_along` on `axis_fit`. 249 chains | The axis the user had accepted enough to score. Start point of the later searches. `tools/score_link_surfaces.py` still reads this file, because `SURFACE_NAMES` in `build_link_surfaces.py` does not list the later gpkg files |
| `score.gpkg`, `score.json` | Evaluation of `surface_width` only | Not an evaluation of `surface_seek` or `surface_flat` |
| `surface_shift2.gpkg` | Second cubic refit (`tools/shift_fitted_again.py`) of the 136 chains that had a station ≥ 0.50 m | Worse. Do not adopt. Do not start a new search from it |
| `surface_narrow.gpkg` | Chain-wide 10 cm narrow plus the same cubic refit (`tools/shift_narrow_iter.py`). 52 of 136 chains kept a round | Not the method. It narrows whole chains and still minimises cubic RMSE |
| `surface_region.gpkg` | First regional width-only attempt. The `_width_at` ramp bug | Superseded as a filename. Do not treat it as current |
| `surface_seek.gpkg` | 20 rounds of edge-difference search plus regional 10 cm narrowing (`tools/narrow_regional.py` as it was then). Started from `surface_width` | The user stopped it to look. Round 20 was still finding shifts. They have not adopted it. The script no longer reproduces this file |
| `surface_flat.gpkg` | One pass that maximises the share of DGM samples under axial grade + 15 percentage points, ±2 m (`tools/narrow_regional.py` as it is now) | Rejected in direction. "es wird nur schlechter". "flache Straßen überall" is not the goal |

Layers:

- `surface_width.gpkg`: `axis`, `axis_fit`, `surface`, `junction`, `junction_point`
- `surface_seek.gpkg`: those, plus `narrow`, `taper`, `obstacle`. No `problem` layer. The half-width list was empty
- `surface_flat.gpkg`: those except `obstacle`, plus `bauwerk`. No `problem` layer
- `surface_shift2.gpkg`: adds `compare`
- `surface_narrow.gpkg`: `axis`, `axis_fit`, `compare`, `junction`, `surface`
- `score.gpkg`: `chain`, `cross_worst`, `kink`, `overlap`, `structure`

`axis` in these files is the original GIP links, copied through. `axis_fit` is the moved line. Links that were never a chain (no width) are only in `axis`. 130 of 875 links are more than 3 m from any surface in `surface_seek`, and all 130 were already bare in `surface_width`.

Production centerline, still what a mesh would read, and not to be overwritten: `data/processed/pfunds-spiess-8192/centerline_shift_taper/centerline_shift.gpkg` (15 mm roughness, width hold 7). `centerline_shift_edge/` was rejected as placement chaos. Do not mesh either of them. `tools/shift_centerline_by_edge.py` is the older edge-difference search. Do not rerun it on that production file.

### Why the cubic fit walks onto the slope

`_fit_to_ground` calls `_smooth_line` from `tools/build_ranked_carriageway.py`. That search minimises cubic RMSE inside ±2 m (`OFFSET_MAX_M` 2.0, `OFFSET_STEP_M` 0.2), then walks 0.2 m toward the neighbour mean while class-width residual stays within `SMOOTH_TOL_M` 0.05. A uniform side slope fits a cubic almost exactly, so the search walks onto slopes and walls. Sampler constants in `tools/sample_cross_profile.py`: `STATION_M` 4.0, `SAMPLE_M` 0.25, `WIDTH_DELTAS_M` (+1, 0, −1, −2), `MIN_WIDTH_M` 2.5, `MIN_SAMPLES` 8, `RMSE_SLACK_M` 0.03, `SMOOTH_PASSES` 40. The cubic residual is not the placement target.

`surface_shift2` made the edge metric worse. On the rescored chains, cross p50 0.325 → 0.341 m, share ≥ 0.50 m 0.382 → 0.403, share ≥ 1 m 0.208 → 0.233. Reschenstraße max 37.8 m → 76.6 m.

`surface_narrow`, aggregate on the 52 kept chains: p50 0.325 → 0.302, share ≥ 0.50 0.382 → 0.358, share ≥ 1 0.208 → 0.199. Named long roads mostly rejected the 10 cm step. An earlier explanation that a shared width "pulled" stations and made two chains worse was wrong. On the actual narrowed spans the 10 cm trial lowered the edge gap (Weg 1600206 span median 0.786 → 0.574 m; S-GW 1487245 1.140 → 1.092 m). The chain score rose because the width ramp grew the polygon (1600206: 15330 → 16878 m²) and the right-hand score used the new polygon.

### Score of `surface_width` (`score.json`)

Length-weighted cross, structures excluded: p50 0.313 m, p90 1.554 m, p99 5.292 m, share ≥ 0.50 m = 0.372, share ≥ 1 m = 0.203. Roughness p50 0.009 m, p90 0.052 m, p99 0.18 m. 159 of 249 chains have `n_cross` > 0. Chains with a station ≥ 0.50 m: 136, length 100620 m. Chains with a station ≥ 1 m: 119, length 97869 m. Same-rank overlap 1502.1 m² in 73 pieces. Other-rank overlap 0, because the lower polygon is already cut. Kink, `min(d, 360−d)` so 0° is straight: p90 9.1°, p99 40.5°, 1563 points ≥ 15°. Do not go back to `abs(180 − angle)` on two bearings that both point along the chain. That printed ~180°.

Named roads on that baseline:

| Chain | Length | p50 | share ≥ 0.5 | share ≥ 1 | max |
|---|---:|---:|---:|---:|---:|
| Reschenstraße, S-B, short ids from 1110205 through 1322639 | ~7949 m | 0.20 m | 0.11 | 0.07 | 37.79 m at 613705.1 / 5199180.8. `structure_m` 3603 |
| Engadiner Straße + Reschenstraße, S-B, includes 1452874, 1392991, 1113816, 1110209, ends 1112448, 1112449 | ~5861 m | 0.20 m | 0.03 | 0.02 | 4.51 m at 613002.1 / 5198581.0 |
| Spisser Straße, S-L | ~9349 m | 0.18 m | 0.06 | 0.03 | 13.51 m at 608313.6 / 5201683.3, western frame edge |
| Unnamed S-L, short ids 1530202, 1552408, 1552475, 1551500, 1551498 | ~7079 m | 0.70 m | 0.56 | 0.43 | 53.51 m at 612764.5 / 5198763.4. `structure_m` 0. Bad along its length |
| Spisser Dorfstraße, S-L | ~647 m | 0.24 m | 0.22 | | max 1.97 m |
| Sammelstrasse, short ids from 1436384 | ~4577 m | 0.19 m | 0.23 | | max 4.67 m |
| Wall mouth, fid 28/38, short ids 1197320, 1175642, 1433766, S-GW | ~740 m | 0.13 m | 0.04 | | max 0.75 m. Not in the ≥ 1 m set |

S-GW chains whose median itself was high: 1213851… (526 m, p50 0.93 m), 1448719 and 1212814 (402 m, p50 0.97 m), 1459345 (144 m, p50 1.27 m), 1175724 (39 m, p50 1.10 m).

After the kink-preserving rebuild, the long axis at the wall mouth is 1.05 m on the flat side (z 975.09), signed offset −1.04 m. The wall is the positive side.

Structures, from routing reference objects on the links, not a geometric join to Verkehrswege. Dip test as in `gip_brunnel_match` (`DIP_OPEN_M` 1.0, `CULVERT_MAX_M` 10). 63 objects: bridge 19, check 14, culvert 3, gallery 18, tunnel 9. Full table in `score.json` key `structures` and `score.gpkg` layer `structure`. Named: Spisser Tunnel 672.4 m, dip 8.51 m, S-L 2630973 and 2630981. Finstermünztunnel 520.6 m, dip 0.00, S-B 2620023. Unterer Finstermünztunnel 474.3 m, dip 4.78 m, S-B 1110750, 1110752, 1110754. Annatunnel 458.9 m, dip 0.00, S-L 1111531. Klammlecktunnel 184.0 m, dip 0.08 m, S-B 2640830 and 2640835. Dip 0 means the ground does not fall below the chord. The console code page garbles ü. The files are UTF-8.

### `surface_seek.gpkg` (20 rounds, then stopped)

`MAX_ROUNDS` 20 is an assistant cap, not a user rule. The user said to look at the result and not continue. Round 20 still had 1484 station samples with |smoothed offset| > 0.02 m, 278 narrow stations, 57 stretches, and no junction end move above 0.05 m. Earlier rounds moved junction ends by more than 1 m. That distance is reseating a side end onto a through line that itself moved, not the 1 m search step. Over 20 rounds a through road can walk much further than 1 m. 26 fitted axes are more than 5 m (Hausdorff) from their `surface_width` line.

On the 140 chains that changed, measured with `_crosses` (vertex widths, structures skipped, not clipped to the old polygon): p50 0.324 → 0.232 m, share ≥ 0.50 0.380 → 0.109, share ≥ 1 0.207 → 0.034. Zero stations hit the half-width floor.

Named roads after those 20 rounds:

| Chain | p50 | share ≥ 0.5 | share ≥ 1 | max | max narrowing |
|---|---|---|---|---|---|
| Reschenstraße | 0.20 → 0.204 | 0.11 → 0.105 | 0.073 → 0.065 | 37.79 → 37.88 m. The extreme stayed | 1.40 m |
| Engadiner Straße + Reschenstraße | 0.204 → 0.207 | 0.03 → 0.023 | 0.018 → 0.009 | 4.51 → 4.88 m | 1.04 m |
| Spisser Straße | 0.176 → 0.173 | 0.062 → 0.027 | 0.028 → 0.016 | 13.51 → 5.36 m | 1.90 m |
| Unnamed S-L 1530202… | 0.699 → 0.286 | 0.562 → 0.302 | 0.435 → 0.222 | 53.51 → 22.23 m | 1.81 m. Still bad along much of its length |
| Spisser Dorfstraße | 0.244 → 0.204 | 0.219 → 0.099 | | 1.97 → 1.19 m | 0.30 m |
| Sammelstrasse 1436384… | 0.193 → 0.144 | 0.232 → 0.011 | 0.066 → 0.001 | 4.67 → 1.31 m | 0.30 m |
| Wall mouth | 0.127 → 0.126 | 0.044 → 0.005 | | 0.75 → 0.53 m | 0.00 m, shift only |
| Weg 1600206, ~3832 m | 0.476 → 0.228 | 0.476 → 0.077 | 0.234 → 0.004 | 5.34 → 4.36 m | 0.30 m |
| S-GW 1487245, ~571 m | 0.332 → 0.275 | 0.287 → 0.022 | | 1.80 → 0.75 m | 0.30 m |

A few maxima rose while the shares fell. Weg 1225476 (276 m): max 1.36 → 4.41 m, share ≥ 0.5 0.125 → 0.054. Do not hide that.

Census of `surface_seek` against `surface_width`: 249/249 chains still have a surface. No chain fell below half its area. Area ratio min 0.71, median 1.00, max 1.03. Surface parts 255 → 254. One part under 5 m². Invalid 0, empty 0.

When the search keeps nothing on a station, the offset target stays 0 and the width is not reduced there. `_smooth_offsets` can still move it if a neighbour inside the 12 m ramp has an offset. `_seat` still moves an endpoint that is a side arm or a lower class. `_rebuild` redraws every surface and subtracts higher ribbons. Under 1 m², the part is not written. If `_ribbon` returns `None`, the chain used to keep the polygon loaded at start (the old surface at the old place) while the axis moved. `_rebuild` in the flat pass now clears `chain["poly"]` when the ribbon is `None`. Do not "fix" a missing surface by assuming the search skipped it. Trace the `short_id` through `_seat` and `_rebuild` first.

### `surface_flat.gpkg` (the rejected flat pass)

One pass, start `surface_width.gpkg`. Every station, both directions, −2.0 m to +2.0 m in 0.10 m steps (`FLAT_SHIFT_M`, `SHIFT_STEP_M`). Positive is left of the direction of travel, normal `(−dy, dx)`. A sample on the carriageway (inset 0.25 m, every 0.5 m) counts when the DGM slope magnitude is ≤ the axial grade plus 15 percentage points (`FLAT_ADD`). Axial grade is `|z(station + 4 m) − z(station)| / distance` from the DGM under the current axis, one number for the whole cross profile. The user explicitly said that grade comes from the terrain under the axis, including the case where a gorge inflates it and the +15 points then let the side slopes count as flat. Highest share wins. Equal share keeps the smaller offset. A 10 cm narrower width wins only when its share is strictly higher, and only inside a cluster of more than two stations within 24 m. Edge difference and roughness do not accept or reject this pass. Known GIP structures stay excluded within 1 m. A station with share 0 anywhere in ±2 m, including after the narrowing test is discarded, becomes a `bauwerk` span from the previous station that has a flat sample to the next one. The axis on that span is pinned at offset 0. The surface is cut out. Ends of the chain are not searched (offset 0) so seating can own them.

Result, 26 October 2026: 34679 stations, 12105 with |smoothed offset| > 0.02 m, 12638 empty, 7955 narrow stations kept, 382 narrow stretches, 413 stations where axial grade + 15 ≥ 60 %. Axial grade p50 7.8 %, p90 16.5 %. Flat share at offset 0: 0.492 → 0.538. `bauwerk`: 250 pieces, 51759 m. Narrow pieces 382, taper pieces 768, half-width problems 0. Junction ends moved 26, max 2.27 m. 160 of 249 chains still have a surface. 89 have none.

`bauwerk` length by code: Weg 43520 m (146 pieces), S-L 3978 m (45), S-GW 2621 m (34), Sammelstrasse 1400 m (4), S-B 240 m (21).

Named roads, flat share before → after, and `bauwerk` length: Spisser Straße 0.955 → 0.972, 48 m. Reschenstraße 0.929 → 0.933, 188 m. Unnamed S-L 1530202 0.351 → 0.371, 3931 m of 7099 m. Engadiner Straße 0.981 → 0.984, 52 m. Spisser Dorfstraße 0.861 → 0.900, 0 m. Several entire Weg chains are share 0.000 and the whole line is `bauwerk`, because a uniform hillside has no sample under axial+15 % inside ±2 m. That is not a bridge. The user looked at this and said it only gets worse.

`tools/narrow_regional.py` now runs this flat pass and writes `surface_flat.gpkg` or, if that file is locked, `surface_flat_next.gpkg`. Running it again does not reproduce `surface_seek.gpkg`. The edge-difference loop that wrote `surface_seek` is gone from the script.

## Published method that was checked and does not apply

Roussel, Bourdon, Morley, Coops, Achim (2022), *Correction, update, and enhancement of vectorial forestry road maps using ALS data, a pathfinder, and seven metrics*, International Journal of Applied Earth Observation and Geoinformation 114, 103020. https://doi.org/10.1016/j.jag.2022.103020

The implementation is the R package ALSroads, marked experimental by the authors. `remotes::install_github("metafor-ulaval/ALSroads")`. `measure_road(ctg, road, dtm)` needs a lidR point-cloud catalog. Conductivity uses slope (full below 5°, zero at 20°, linear between), roughness, intensity, canopy height, and point density. A floor of 0.1 stops one steep cell from breaking the path. The path is one least-cost line from the start node to the end node inside a buffer (default 80 m). Width is measured afterwards on cross sections. Poor classes keep the old geometry.

Not usable here. This site has the 0.5 m DGM and the GIP lines, not the point cloud. The package is for Québec forest roads. Retaining walls, tunnels, and galleries are outside it. Do not re-derive the paper in Python.

## Code map

| File | Role |
|---|---|
| `tools/build_link_surfaces.py` | Kink-preserving ribbons, cubic `_fit_to_ground`, rank cut, junction seating |
| `tools/narrow_regional.py` | Currently the rejected flat-share pass. Do not run it to "continue" |
| `tools/score_link_surfaces.py` | Scores `surface_width.gpkg` only |
| `tools/shift_fitted_again.py` | Wrote `surface_shift2.gpkg` |
| `tools/shift_narrow_iter.py` | Wrote `surface_narrow.gpkg` |
| `tools/build_ranked_carriageway.py` | Old WFS identity and `_smooth_line` (cubic RMSE) |
| `tools/sample_cross_profile.py` | Cubic sampler. Not the placement target |
| `tools/shift_centerline_by_edge.py` | Older edge search. Do not rerun on the production centerline |
| `tools/gip_routing_load.py` | Routing extract |
| `.cursor/rules/junction-knick.mdc` | Through road keeps the kink. Still the user's junction rule |
| `.cursor/rules/querprofil-lage.mdc` | Rejected flat-sample text. Stop banner at the top |
| `.cursor/rules/ziel-klaeren.mdc` | If the user asks whether you understood, name the open points and wait. Do not fill a gap and then build |
| `.cursor/rules/gip-fahrbahn-defaults.mdc` | Old WFS class widths. Not the link-surface widths |

`_tmp_rail_look.py` stays. Do not leave new `_tmp_*.py` files. PowerShell 5.1: one line, no `&&`, no `python -c` with braces. Write a temp `.py` and delete it. `_load_geotiff` needs a `Path`. rasterio is not installed. `RES_M` is 0.5. Float NaN is truthy. Street-name filters need `np.isfinite`.

Mesh readiness was answered and not built. Raw DGM on these polygons is too rough for a driving surface. Same-rank overlap height, and whether a side ribbon stops at the through edge, are not decided.

## What the next session does

Read this file. Look at a gpkg only if the user names one. The files to open for the rejected flat pass are:

`data/processed/pfunds-spiess-8192/link_surface/surface_flat.gpkg`

`data/processed/pfunds-spiess-8192/link_surface/surface_width.gpkg`

The scored baseline is `surface_width.gpkg` plus `score.gpkg` / `score.json`. The 20-round edge search, which the user also has not adopted, is `surface_seek.gpkg`.

Then ask. Do not choose a new objective, a new buffer, a new threshold, or a new output file.
