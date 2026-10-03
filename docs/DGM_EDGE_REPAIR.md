# DGM edge repair

Carriageway-edge fill for the Imst corridor. Heights come from
`data/processed/tirol-imst-8192/corridor50_raw/corridor50_raw.tif`.
The carriageway polygon and centerline come from
`centerline_shift_taper/centerline_shift.gpkg`. The transect rasters themselves
are not copied into the level. What the level shows is the mesh section below.
Terrain outside the carriageway polygon is never written by the transect.

Roughness is the absolute deviation from the mean in a 1.5 m window (3×3
pixels at 0.5 m). The drop threshold is 1.5 cm. A uniform cross-slope lies on
that mean and is not an error. At the edge the same window also contains the
embankment, so a correct road lip next to a bank still counts as rough. Compare
methods on the carriageway-only window (`_road_only` in
`tools/repair_dgm_cross_var.py`) as well as on the full window.

Raw carriageway, full window, 596 290 pixels: 62 842 at or above 1.5 cm,
median 0.48 cm, p99 6.8 cm. Carriageway-only p99 was 5.36 cm.

## Rejected: straight grade through the first smooth pixel

Do not continue `dgm_repair_cross_cover` or anything built on the same anchor.
It does not reconstruct a cross-section. It waits for a pixel that happens to
sit within 1.5 cm of the mean of its own 3×3 neighbourhood, calls that pixel
the anchor, and lays one straight line from that pixel back to the edge.
Curvature is not kept. A flat spot inside a gap passes the test and becomes
the reference. Pixels inward of the anchor stay raw, so one smooth pixel
between the edge and a rough band hides that band.

The 1.5 m mean is nine DGM heights in a 3×3 square. At the edge the square
includes terrain. It is only the pass/fail test. The stored anchor height is
the pixel's own raw height, not the mean. The slope is the least-squares fit
of other passing pixels from the anchor inward (smallest residual, not the
smallest slope). That one slope is then written on the whole outward gap.

Schematic, no measured heights:
`data/processed/tirol-imst-8192/dgm_repair_cross_cover/schema_gerade.png`.

Measured example, OID 6975. The typed point 283269 / 28279 is outside the
corridor. With the 3 and the 8 swapped it is easting 28279, northing 238269,
1.1 m off the centreline. Foot 28280 / 238269, station 631 m. Plot, every 1 m
from −4 m to +4 m along the road, positive = left of travel:
`data/processed/tirol-imst-8192/dgm_repair_cross_cover/oid6975_querprofil.png`.

On that stretch the middle of the road is unchanged (median |Δz| = 0 on every
section). The left anchor jumps from metre to metre, and so does the written
correction. Right side usually has its first passing pixel within 0.1–0.4 m,
so almost nothing is written.

| along | left xa | left source | right | max \|Δz\| on the road | pixels ≥ 2 cm |
|---:|---:|---:|---|---:|---:|
| −4 m | 1.50 m | local 1.5 m | xa 0.34 m | 33 cm | 4 |
| −3 m | 1.50 m | local 1.5 m | xa 0.22 m | 20 cm | 2 |
| −2 m | 0.10 m | wider search | no grade | 0 | 0 |
| −1 m | 1.45 m | linear gap | xa 0.13 m | 18 cm | 3 |
| 0 | 1.30 m | local 1.5 m | xa 0.20 m | 20 cm | 2 |
| +1 m | 1.50 m | local 1.5 m | no grade | 63 cm | 4 |
| +2 m | 2.15 m | local 1.5 m | no grade | 82 cm | 4 |
| +3 m | 1.85 m | linear gap | xa 0.41 m | 60 cm | 4 |
| +4 m | 1.50 m | local 1.5 m | xa 0.21 m | 24 cm | 2 |

Source "local 1.5 m" means the 1.5 m window already had RMSE ≤ 2 cm. "Wider
search" borrows a slope. "Linear gap" means the station had no fit of its own
and took edge height and slope from the neighbouring valid stations. The 82 cm
step at +2 m and the 0 cm step at −2 m are four metres apart. That is the
visible wave. Two to four pixels per section carry it.

Also rejected, same grade, do not revive:

- `dgm_repair_cross_extend` (`repair_dgm_cross_settle.py` as it stands): the
  line continued up to 1.5 m inward. Road-only hot pixels 45 266 → 50 083,
  p99 3.63 → 4.27 cm. It overwrote road that was already under 1 cm.
- The interior median (class 10 in `dgm_repair_cross_settle`, no longer
  applied by the script): 582 spikes, p99 of those pixels 21 cm → 89 cm.
- `dgm_repair_cross_shoulder`: first metre outside the polygon set to the lip
  height, flat, structures skipped. 170 903 pixels, median move 4 cm, p90
  40 cm, max 9.9 m where the bank is steep. Not in the game.

The level no longer shows this fill. The group `road_grid` in
`autoroad_imst_8192` is the transect mesh described below.

## Do not repeat

These runs are kept under `data/processed/tirol-imst-8192/` and are not the method.

- `dgm_repair_side`, `dgm_repair_side_fill`: 1 m band per edge, linear height
  along the edge, interior gaps blended by distance. One clean station was
  written onto the whole side (`filled[:] = med[good][0]` in
  `_heights_along`). Past the last clean station the height was held.
- `dgm_repair_iter`: the same fill up to 10 times, roughness remeasured after
  each pass. Hot pixels stayed near 60 000. Full-window p99 rose from 6.8 cm
  to about 12 cm and was still 10.2 cm after pass 10.
- `dgm_repair_lock`: pixels that passed on the raw DGM stayed fixed; only the
  originally rough pixels moved, first to the mean of locked neighbours, then
  along the edge between two locked stations. Hot count fell, full-window p99
  rose to 11.8 cm. A pixel is "good" when it matches the local mean, not when
  it lies on the cross-section. Replacing its neighbours moves the mean, so
  the next pass flags the pixel that was just kept.

Do not smear one height across the width. Do not use inverse-distance weights
in the plane (they cut across bends and across the other edge). Do not grow a
fill by remeasuring roughness and rewriting whatever is now hot. Do not put
the embankment into the blur.

## Cross-section

`tools/repair_dgm_cross_section.py`, `tools/repair_dgm_cross_var.py`,
`tools/repair_dgm_cross_cover.py`.

At each edge station, pixels under 1.5 cm inward of the edge define one
straight grade. The grade is forced through the first of those pixels, so the
inner end meets the existing surface. The search keeps the local 1.5 m grade
when its RMSE is already at most 2 cm. Otherwise it tries spans of 2.5, 4 and
6 m and neighbouring stations, and keeps the grade with the smallest residual.
Along the road the search stops when the chord between its ends leaves the
edge by more than 0.25 m, so it does not cut the inside of a bend. A station
with no usable grade borrows edge height and slope from the neighbouring valid
stations only when that chord also stays within 0.25 m: up to 20 m linear,
beyond that a parabola on edge height and a linear slope. There is no
extrapolation past the first or last valid station. Slope is limited to 0.25,
RMSE to 4 cm, at least 3 samples and 0.5 m of span.

`dgm_repair_cross_var` wrote only the ray samples outside that first good
pixel. The next pixel inward stayed raw, and the step made the 1.5 m window
rougher than the gap it had closed.

`dgm_repair_cross_cover` is the run that was inspected and then rejected.
See the section above. Every carriageway pixel in the
trapezoid from the edge to the anchor is written, with height interpolated in
station and offset. A seam pass then writes one step further in, at most
0.75 m, and only where the carriageway-only deviation is still at least
1.5 cm. Where both edges claim a pixel, the height is mixed by distance to
the two edges. On this corridor that blend did not fire. Cover result,
carriageway-only: hot pixels 54 965 → 45 266, p99 5.36 cm → 3.63 cm.
Full-window p99 rose to 11.4 cm because the new lip disagrees with the bank
in the window. 12 269 of the remaining hot pixels on the written surface sit
on the inner boundary of the fill. Only 271 sit in a 3×3 that is already
fully written, 61 of them at or above 5 cm. The profile is consistent. The
boundary pixel looks rough while the next raw pixel is still in the window.
Class 8 (the seam) is the roughest written class for that reason. Extending
by one roughness-triggered step only moves the step inward.

## Settle

`tools/repair_dgm_cross_settle.py` reads `dgm_repair_cross_cover` and writes
`dgm_repair_cross_settle`. Profiles are fit again on the raw DGM, the same way
as cover. Pixels cover already wrote (class 3 and above) are not moved.

1. Continue the same line inward from the anchor. Walk pixels in order of
   offset from the edge. Skip pixels already written. Write the line onto an
   unwritten pixel while its raw height differs from the line by more than
   1.5 cm. Stop that station segment at the first unwritten pixel whose raw
   height is already within 1.5 cm of the line, or 1.5 m past the anchor,
   whichever comes first. Do not measure roughness to decide this. Do not
   search for a new profile. Do not step over a pixel that already agrees.
2. After that, pixels of at least 5 cm on the carriageway alone, that are
   still unwritten and do not touch the written surface (8-connected, one
   pixel), are set to the median of nearby carriageway pixels already under
   1.5 cm. The spike itself is left out of that mean. The window grows from
   3×3 up to 2 m and needs at least 3 such pixels. Otherwise the spike stays
   raw. This is not the edge profile, and the terrain is not in the median.

Leave single pixels under 5 cm that are not on that inner step. Another
roughness pass on them moves the local mean and creates the next outlier.

Class 9 is the inward continuation. Class 10 is the interior median. Classes
1–8 stay as cover left them.

The 61 pixels at or above 5 cm that already sit inside the written fill are
left as they are. They are not opened again.

First settle run on this corridor did not lower the tail. Carriageway-only
hot pixels went from 45 266 to 50 014, p99 from 3.63 cm to 4.28 cm.
Full-window p99 went from 11.4 cm to 12.7 cm. The inward line wrote 13 053
pixels; only 2 840 of them were hot before, and 5 006 of them are hot after.
4 896 further pixels became hot beside that band. Median |Δz| on the
extension is 2.5 cm, p90 is 11 cm, p99 is 55 cm, max 1.15 m. The grade that
fits the outer gap keeps going across road that is only a few centimetres off
the raw surface, until some pixel happens to lie on that line. That is the
stop rule as written. It is not evidence that another roughness iteration
would help. The interior median replaced 582 spikes, and 504 of those are
still at or above 1.5 cm against their neighbourhood.

## Transect along the centerline

`tools/repair_dgm_transect.py` writes `dgm_repair_transect`. This is the
successor. The cross-slope grades above stay rejected.

Each half is sampled on the centerline normal, every 0.5 m along the road and
every 0.25 m outward, until the carriageway polygon ends. A sample under
1.5 cm in the carriageway-only 1.5 m window is a support of that half. A rough
sample is not, including an edge pixel that is already the embankment. The
stored shape is the height relative to the centerline. Between two supports of
the same offset and the same half, at most 50 m apart, that shape is blended
along the arc, so the profile is different at every station. Only a rough
pixel is replaced, and only with the blended value at its own offset. Nothing
is fitted across the gap at the broken station. An offset with no support
within 50 m stays raw. Past the first and last support of that offset, nothing
is written. Structure spans are neither supports nor targets. Terrain outside
the carriageway polygon is not written.

On this corridor, carriageway-only: hot pixels 54 544 → 35 520, p99 5.27 cm →
4.71 cm. 50 811 rough pixels were replaced, 3 733 stayed raw. Median move
2.3 cm, p90 7.3 cm, max 3.45 m where a slope pixel inside the polygon is
replaced by the road profile. Full-window p99 rose from 6.2 cm to 7.9 cm
because the new lip still shares its 1.5 m window with the bank.

OID 6975, foot 28 280 / 238 269, station 630.8 m. The left edge at 2.5 m is
rough. The longitudinal trace follows the clean supports of that offset, not a
grade fitted at that station. The cross-section at the same station keeps the
raw center and replaces the rough left edge with the blended half-profile.
Plots: `dgm_repair_transect/oid6975_laengs.png` and
`dgm_repair_transect/oid6975_querprofil.png`.

## In the level

The method below is the road mesh. The level does not currently show it.
The last inject is the rejected bridge fill (`dom_bridge_deck/deck.tif` and
`clip.gpkg`). What differs, and the command that builds the road mesh again,
is in [ROAD_MESH.md](ROAD_MESH.md). Do not extend `tools/apply_bridge_deck.py`
as if it were this method.

The surface the wheel drives is a COLLADA mesh, not the heightmap.
`tools/build_road_grid.py` reads `dgm_repair_transect/02_repaired.tif` and
clips to the `carriageway` layer of `centerline_shift.gpkg`. A 0.5 m cell
whose centre lies in that polygon becomes a quad. A quad that crosses the
outline is cut on the polygon. Every vertex sits 4 cm above the raster
(`CLEARANCE_M` in `tools/build_corridor_mesh.py`).

Before the mesh is built, the raster within 1 m of the outline is set to the
nearest interior road height. The donor lies more than 1 m inside, or on the
centre of a road narrower than 2 m, and at most 4 m away, so a parallel road
is not copied. A 3 m median and a Gaussian of 4 m then run along the outer
two erosion rings only. Left and right stay separate, and a chain stops at a
junction. The same height is copied into the 1 m strip just outside the
polygon, so a cut vertex samples the road and not the slope. Parts are split
only after that raster is finished, when one geometry would pass 65 535
vertices. That limit is the BeamNG import, not the COLLADA file. There is no
height edit per part. The build fails when a shared cut differs by more than
1 cm, or an open edge sits more than 0.75 m inside the polygon.

Each DAE holds both surfaces. `part_XXX_a999` is drawn (UVs, asphalt).
`collision-1/Colmesh-1` is not drawn. The level object uses
`collisionType: Collision Mesh`, so the wheel rests on the Colmesh. Both
surfaces share the same height, including the 4 cm. The collision mesh is the
visual mesh with interior vertices clustered on a 2 m plan grid. Boundary
vertices stay, so the outline and the cuts between parts do not open. This is
not an angle threshold. A collapse at 2° has not been run. Moving the whole
Colmesh down would drop the car through the visible asphalt by the same
amount, because the wheel follows the Colmesh. A downward thickness with the
top face left on the visible surface is not built.

Last mesh of this corridor: 18 parts, 732 113 visual vertices and
1 327 043 triangles, 186 953 collision vertices and 236 801 triangles.
`part_000` in the level has 49 904 visual vertices and 12 542 collision
vertices.

The ground beside and under that mesh is the raw 0.5 m DGM.
`tools/apply_corridor_dgm.py` resamples `corridor50_raw.tif` onto the
heightmap as the replace layer `corridor_dgm`, priority 45. It covers
`road_bed` inside the corridor. The span layer stays at 55, so a bridge or
gallery opening is not filled with the road. The repaired road raster is not
written into the heightmap. The playable grid is 8 192 nodes over 8 192 m.
After a compose, re-import `terrainPreset.json`. Do not save the World Editor
when the objects come from the inject.

Nine cross-sections of that mesh against the raw DGM and the repaired raster
are in `dgm_repair_transect/querprofile_stichprobe.png` (five Bundesstraßen
and five Landesstraßen, seed 20260927, stations at least 30 m from the ends
and 8 m from a structure). One draw, B189 OID 4461, lies outside the corridor
raster and has no samples. On the other nine, the repaired interior stays
within 2.8 cm of the raw DGM (median 0). The mesh interior stays within
2.3 cm (median 0.9 cm). The outer metre, which received the road height,
moves by a median of 2.9 cm and at most 10 cm.

A further sink of the terrain under the mesh, 6 cm times the fraction of the
1 m square around a heightmap node that the mesh covers, is not built. Fully
covered, that would put the terrain about 10 cm under the mesh, because the
mesh is already 4 cm above the raster.

## Run

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\repair_dgm_cross_cover.py
```

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\repair_dgm_cross_settle.py
```

Cover has to exist before settle. The transect does not read either of them.

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\repair_dgm_transect.py
```

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\build_road_grid.py --heights "data\processed\tirol-imst-8192\dgm_repair_transect\02_repaired.tif" --clip "data\processed\tirol-imst-8192\centerline_shift_taper\centerline_shift.gpkg" --clip-layer carriageway
```

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\apply_corridor_dgm.py
```

The mesh command copies the DAE files into the level and drops DAE names that
are no longer in the set. Quit BeamNG and start it again after that. A map
reload keeps the old shape. The corridor command only updates the heightmap
import. Re-import the terrain preset, and do not save the World Editor.

Earlier folders stay. QGIS reads the GeoTIFFs; `qgis_process` crashes in this environment.
