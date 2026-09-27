# DGM edge repair

Carriageway-edge fill for the Imst corridor. Heights come from
`data/processed/tirol-imst-8192/corridor50_raw/corridor50_raw.tif`.
The carriageway polygon and centerline come from
`centerline_shift_taper/centerline_shift.gpkg`. Nothing in this pipeline is
injected into BeamNG. Terrain outside the carriageway polygon is never written.

Roughness is the absolute deviation from the mean in a 1.5 m window (3×3
pixels at 0.5 m). The drop threshold is 1.5 cm. A uniform cross-slope lies on
that mean and is not an error. At the edge the same window also contains the
embankment, so a correct road lip next to a bank still counts as rough. Compare
methods on the carriageway-only window (`_road_only` in
`tools/repair_dgm_cross_var.py`) as well as on the full window.

Raw carriageway, full window, 596 290 pixels: 62 842 at or above 1.5 cm,
median 0.48 cm, p99 6.8 cm. Carriageway-only p99 was 5.36 cm.

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

`dgm_repair_cross_cover` is the fill to keep. Every carriageway pixel in the
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

## Run

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\repair_dgm_cross_cover.py
```

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\repair_dgm_cross_settle.py
```

Cover has to exist before settle. Earlier folders stay. QGIS reads the
GeoTIFFs; `qgis_process` crashes in this environment.
