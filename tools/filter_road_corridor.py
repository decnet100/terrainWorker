"""Filter the 0.5 m road corridor and leave one georeferenced height raster.

The WCS download stays in tiles. This masks 15 m either side of the Landes- and
Bundesstraßen, runs the exclusive road-bed profile, and writes a single
GeoTIFF of the whole corridor. Collada parts are a later mesh step.

The driving surface is not sunk. The 8 cm terrain sink belongs to the later
terrain write. Nothing here is copied into the level.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\filter_road_corridor.py

The 15 m strip is the finite pixels of that raster. Write it as its own mask,
255 inside the strip:

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\filter_road_corridor.py --masks-only

One mosaic per stage (selection, raw fill, after the blurs, after crossfall):

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\filter_road_corridor.py --stages
"""
from __future__ import annotations

import csv
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import tifffile as tiff
from PIL import Image, ImageDraw
from scipy.ndimage import distance_transform_edt

import build_bridges as bb
import build_decal_roads as bdr
from build_asphalt_meshroads import _is_main_road
from diag_road_bed_modes import _gip_polylines
from fetch_road_corridor_dgm import RES_M, TILE_M
from raster_tile_fetch import elevation_nodata_mask
from site_coords import SiteCoords, load_site, processed_dir, site_slug

ROOT = Path(__file__).resolve().parents[1]
MASK_M = 15.0
HALO_M = 24.0
# Sum of the three truncated kernels (2 * 1.5 + 2 * 2.5 + 2 * 4).
SINK_M = 0.0


def _load_tiles(src: Path) -> dict[tuple[int, int], dict]:
    index = json.loads((src / "corridor_index.json").read_text(encoding="utf-8"))
    tiles: dict[tuple[int, int], dict] = {}
    for spec in index["tiles"]:
        arr = np.asarray(tiff.imread(src / spec["name"]), dtype=np.float32)
        if arr.ndim == 3:
            arr = arr[..., 0]
        bad = elevation_nodata_mask(arr)
        if np.any(bad):
            arr = arr.copy()
            arr[bad] = np.nan
        spec = dict(spec)
        spec["arr"] = arr
        tiles[(int(spec["ix"]), int(spec["iy"]))] = spec
    return tiles


def _sample_crs(tiles: dict[tuple[int, int], dict], x: float, y: float) -> float:
    for spec in tiles.values():
        if x < spec["xmin"] or x > spec["xmax"] or y < spec["ymin"] or y > spec["ymax"]:
            continue
        width = int(spec["width"])
        height = int(spec["height"])
        fx = (x - spec["xmin"]) / (spec["xmax"] - spec["xmin"]) * width - 0.5
        fy = (spec["ymax"] - y) / (spec["ymax"] - spec["ymin"]) * height - 0.5
        if fx < -0.5 or fy < -0.5 or fx > width - 0.5 or fy > height - 0.5:
            continue
        x0 = int(math.floor(fx))
        y0 = int(math.floor(fy))
        x1 = min(max(x0 + 1, 0), width - 1)
        y1 = min(max(y0 + 1, 0), height - 1)
        x0 = min(max(x0, 0), width - 1)
        y0 = min(max(y0, 0), height - 1)
        tx, ty = fx - x0, fy - y0
        arr = spec["arr"]
        v = (
            float(arr[y0, x0]) * (1 - tx) * (1 - ty)
            + float(arr[y0, x1]) * tx * (1 - ty)
            + float(arr[y1, x0]) * (1 - tx) * ty
            + float(arr[y1, x1]) * tx * ty
        )
        if math.isfinite(v):
            return v
    return float("nan")


def _prepare(
    roads: list[dict], cfg: dict, z_at, meshroads: list[dict], *, z_min_m: float
) -> list[dict]:
    densify = float(cfg.get("densify_max_step_m") or 0.0)
    stamp_step = min(densify if densify > 0 else 8.0, 4.0)
    w_scale = max(0.1, float(cfg["width_scale"]))
    prepared: list[dict] = []
    for road in roads:
        raw = []
        for p in road.get("pts") or []:
            if len(p) < 4:
                continue
            raw.append([float(p[0]), float(p[1]), float(p[2]), float(p[3]) * w_scale])
        if len(raw) < 2 or bdr._polyline_length(raw) < float(cfg["min_length_m"]):
            continue
        bp = bdr._road_bed_params_for_road(road, cfg, gip_polys={})
        densified = bdr._densify_nodes(raw, stamp_step)
        nodes = []
        for n in densified:
            z = float(z_at(n[0], n[1]))
            nodes.append(
                {"x": n[0], "y": n[1], "z": z, "z_dgm": z, "width": n[3]}
            )
        if meshroads:
            nodes = bdr._hold_span_deck_z(nodes, meshroads)
            # MeshRoad node Z is BeamNG height above z_min. The corridor is absolute.
            for n in nodes:
                if n.get("hold_z"):
                    n["z"] = float(n["z"]) + z_min_m
        nodes = bdr._smooth_polyline_z(
            nodes, float(bp["smooth_m"]), freeze_holds=True
        )
        cf = bdr._crossfall_pieces(nodes, z_at)
        prepared.append(
            {
                "id": str(road.get("id") or road.get("objectid") or ""),
                "str_code": str(road.get("str_code") or ""),
                "nodes": nodes,
                "pad": float(bp["pad_m"]),
                "falloff": float(bp["falloff_m"]),
                "sink": SINK_M,
                "max_raise": float(bp["max_raise_m"]),
                "max_cut": float(bp["max_cut_m"]),
                "width": max(float(n["width"]) for n in nodes),
                "rank": bdr._bed_class_rank(road),
                "length": bdr._polyline_length(raw),
                "crossfall": cf,
                "smooth_m": float(bp["smooth_m"]),
            }
        )
    return prepared


def _window_frame(spec: dict, halo_px: int) -> dict:
    xmin = float(spec["xmin"])
    ymax = float(spec["ymax"])
    width = int(spec["width"])
    height = int(spec["height"])
    side = max(width, height) + 2 * halo_px
    extent = (side - 1) * RES_M
    x0c = xmin - (halo_px - 0.5) * RES_M
    y0c = ymax + (halo_px - 0.5) * RES_M
    return {
        "side": side,
        "extent": extent,
        "x0c": x0c,
        "y0c": y0c,
        "width": width,
        "height": height,
        "halo": halo_px,
    }


def _shift_item(item: dict, sc: SiteCoords, frame: dict, scale: float) -> dict:
    x0c = frame["x0c"]
    y_add = frame["extent"] - frame["y0c"]
    nodes = []
    for n in item["nodes"]:
        x, y = sc.beamng_to_crs(float(n["x"]), float(n["y"]))
        nodes.append(
            {
                **n,
                "x": x - x0c,
                "y": y + y_add,
                "width": float(n["width"]) * scale,
            }
        )
    pieces = [
        (s0 * scale, s1 * scale, cf / scale)
        for s0, s1, cf in item["crossfall"]
    ]
    return {
        **item,
        "nodes": nodes,
        "pad": float(item["pad"]) * scale,
        "falloff": float(item["falloff"]) * scale,
        "crossfall": pieces,
    }


def _paste_base(tiles: dict[tuple[int, int], dict], frame: dict) -> np.ndarray:
    side = frame["side"]
    elev = np.full((side, side), np.nan, dtype=np.float32)
    x0 = frame["x0c"]
    x1 = frame["x0c"] + (side - 1) * RES_M
    y_north = frame["y0c"]
    y_south = frame["y0c"] - (side - 1) * RES_M
    for spec in tiles.values():
        if spec["xmax"] < x0 or spec["xmin"] > x1:
            continue
        if spec["ymax"] < y_south or spec["ymin"] > y_north:
            continue
        c0 = int(round((float(spec["xmin"]) + 0.5 * RES_M - frame["x0c"]) / RES_M))
        r0 = int(round((frame["y0c"] - (float(spec["ymax"]) - 0.5 * RES_M)) / RES_M))
        src = spec["arr"]
        sh, sw = src.shape
        rs = max(0, -r0)
        cs = max(0, -c0)
        rd = max(0, r0)
        cd = max(0, c0)
        rh = min(sh - rs, side - rd)
        rw = min(sw - cs, side - cd)
        if rh <= 0 or rw <= 0:
            continue
        elev[rd : rd + rh, cd : cd + rw] = src[rs : rs + rh, cs : cs + rw]
    bad = ~np.isfinite(elev)
    if np.any(bad) and not np.all(bad):
        _dist, (iy, ix) = distance_transform_edt(bad, return_indices=True)
        elev = elev.copy()
        elev[bad] = elev[iy[bad], ix[bad]]
    return elev


def _paint_spans(
    frame: dict,
    bands: list[tuple[list[tuple[float, float]], float]],
    sc: SiteCoords,
    scale: float,
) -> np.ndarray:
    side = frame["side"]
    extent = frame["extent"]
    img = Image.new("L", (side, side), 0)
    if not bands:
        return np.zeros((side, side), dtype=bool)
    draw = ImageDraw.Draw(img)
    y_add = extent - frame["y0c"]
    for poly, half in bands:
        if len(poly) < 2:
            continue
        pts = []
        for x, y in poly:
            cx, cy = sc.beamng_to_crs(float(x), float(y))
            pts.append(
                bb._to_px_beamng(cx - frame["x0c"], cy + y_add, side, extent)
            )
        wpx = max(2, int(round((2.0 * float(half) * scale) / RES_M)))
        draw.line(pts, fill=255, width=wpx, joint="curve")
    return np.asarray(img, dtype=np.uint8) > 0


def _mask_strip(items: list[dict], frame: dict) -> np.ndarray:
    side = frame["side"]
    extent = frame["extent"]
    mpp = extent / max(side - 1, 1)
    rad = MASK_M / mpp
    mask = np.zeros((side, side), dtype=bool)
    for item in items:
        pts = item["nodes"]
        for a, b in zip(pts, pts[1:]):
            ax, ay = float(a["x"]), float(a["y"])
            bx, by = float(b["x"]), float(b["y"])
            dx, dy = bx - ax, by - ay
            len2 = dx * dx + dy * dy
            if len2 < 1e-8:
                continue
            c0 = max(0, int(math.floor(min(ax, bx) / mpp - rad)))
            c1 = min(side - 1, int(math.ceil(max(ax, bx) / mpp + rad)))
            # Row 0 is north, so a larger local y is a smaller row.
            py0 = (extent - max(ay, by)) / mpp
            py1 = (extent - min(ay, by)) / mpp
            r0 = max(0, int(math.floor(min(py0, py1) - rad)))
            r1 = min(side - 1, int(math.ceil(max(py0, py1) + rad)))
            if c1 < c0 or r1 < r0:
                continue
            yy, xx = np.ogrid[r0 : r1 + 1, c0 : c1 + 1]
            px = xx * mpp
            py = extent - yy * mpp
            t = ((px - ax) * dx + (py - ay) * dy) / len2
            np.clip(t, 0.0, 1.0, out=t)
            dist = np.hypot(px - (ax + t * dx), py - (ay + t * dy))
            mask[r0 : r1 + 1, c0 : c1 + 1] |= dist <= MASK_M
    return mask


def _filter_window(
    items: list[dict],
    base: np.ndarray,
    *,
    protected: np.ndarray,
    max_raise0: float,
    max_cut0: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    side = int(base.shape[0])
    extent = (side - 1) * RES_M
    mpp = RES_M
    value_acc = np.zeros((side, side), dtype=np.float64)
    weight_acc = np.zeros((side, side), dtype=np.float64)
    opacity = np.zeros((side, side), dtype=np.float64)
    raise_lim = np.zeros((side, side), dtype=np.float64)
    cut_lim = np.zeros((side, side), dtype=np.float64)
    tilt = bdr._stamp_exclusive_beds(
        items,
        value_acc=value_acc,
        weight_acc=weight_acc,
        opacity_acc=opacity,
        raise_lim=raise_lim,
        cut_lim=cut_lim,
        size=side,
        extent=extent,
        mpp=mpp,
    )
    opacity[protected] = 0.0
    has_w = weight_acc > 1e-9
    value = np.array(base, dtype=np.float64, copy=True)
    value[has_w] = value_acc[has_w] / weight_acc[has_w]
    d_raise = value - base
    d_cut = base - value
    lim_raise = np.where(raise_lim > 1e-9, raise_lim, max_raise0)
    lim_cut = np.where(cut_lim > 1e-9, cut_lim, max_cut0)
    bad = ((d_raise > 1e-6) & (d_raise > lim_raise)) | (
        (d_cut > 1e-6) & (d_cut > lim_cut)
    )
    opacity[bad] = 0.0
    protected = protected | bad
    out = base * (1.0 - opacity) + value * opacity
    filled = np.array(out, dtype=np.float32, copy=True)
    if tilt is not None:
        tilt[opacity <= 1.0e-6] = 0.0
        bdr._smooth_exclusive_grade(out, value, opacity, mpp=mpp, protected=protected)
        filtered = np.array(out, dtype=np.float32, copy=True)
        out = out + tilt
    else:
        bdr._smooth_exclusive_grade(out, value, opacity, mpp=mpp, protected=protected)
        filtered = np.array(out, dtype=np.float32, copy=True)
    return out, opacity, bad, filled, filtered


def _stamp_centerline(
    prepared: list[dict], sc: SiteCoords, spec: dict, surf: np.ndarray
) -> None:
    """Write the filtered raster Z back onto nodes that fall in this tile."""
    height, width = surf.shape
    xmin, xmax = float(spec["xmin"]), float(spec["xmax"])
    ymin, ymax = float(spec["ymin"]), float(spec["ymax"])
    for item in prepared:
        for n in item["nodes"]:
            if n.get("hold_z"):
                continue
            x, y = sc.beamng_to_crs(float(n["x"]), float(n["y"]))
            if x < xmin or x > xmax or y < ymin or y > ymax:
                continue
            fx = (x - xmin) / (xmax - xmin) * width - 0.5
            fy = (ymax - y) / (ymax - ymin) * height - 0.5
            if fx < -0.5 or fy < -0.5 or fx > width - 0.5 or fy > height - 0.5:
                continue
            x0 = int(math.floor(fx))
            y0 = int(math.floor(fy))
            x1 = min(max(x0 + 1, 0), width - 1)
            y1 = min(max(y0 + 1, 0), height - 1)
            x0 = min(max(x0, 0), width - 1)
            y0 = min(max(y0, 0), height - 1)
            tx, ty = fx - x0, fy - y0
            v = (
                float(surf[y0, x0]) * (1 - tx) * (1 - ty)
                + float(surf[y0, x1]) * tx * (1 - ty)
                + float(surf[y1, x0]) * (1 - tx) * ty
                + float(surf[y1, x1]) * tx * ty
            )
            if math.isfinite(v):
                n["z_surface"] = v


def _write_profile(path: Path, prepared: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "road_id",
                "str_code",
                "s_m",
                "x",
                "y",
                "z_dgm",
                "z_smooth",
                "z_surface",
                "crossfall",
            ]
        )
        for item in prepared:
            cum = 0.0
            prev = None
            for n in item["nodes"]:
                if prev is not None:
                    cum += math.hypot(float(n["x"]) - prev[0], float(n["y"]) - prev[1])
                if n.get("hold_z"):
                    prev = (float(n["x"]), float(n["y"]))
                    continue
                w.writerow(
                    [
                        item["id"],
                        item["str_code"],
                        f"{cum:.2f}",
                        f"{float(n['x']):.2f}",
                        f"{float(n['y']):.2f}",
                        f"{float(n['z_dgm']):.3f}",
                        f"{float(n['z']):.3f}",
                        f"{float(n.get('z_surface', float('nan'))):.3f}",
                        f"{bdr._crossfall_lookup(item['crossfall'], cum):.5f}",
                    ]
                )
                prev = (float(n["x"]), float(n["y"]))


def main() -> None:
    t0 = time.perf_counter()
    site = load_site()
    sc = SiteCoords(site)
    proc = processed_dir(site)
    cfg = bdr._cfg(site.get("beamng") or {})
    scale = sc.bw / sc.terrain_span
    src = ROOT / "data" / "raw" / f"dgm_{site_slug(site)}_corridor50"
    if not (src / "corridor_index.json").is_file():
        raise SystemExit(f"missing {src / 'corridor_index.json'}")

    roads = [
        r
        for r in bdr.stitch_abutting_roads(_gip_polylines(site, proc), cfg)
        if _is_main_road(r, site)
    ]
    print(f"corridor filter: loading {src.name}", flush=True)
    tiles = _load_tiles(src)
    print(f"corridor filter: {len(tiles)} tiles, sampling centerlines", flush=True)

    def z_at(bx: float, by: float) -> float:
        x, y = sc.beamng_to_crs(bx, by)
        return _sample_crs(tiles, x, y)

    meta = json.loads((proc / "heightmap_meta.json").read_text(encoding="utf-8"))
    z_min_m = float(meta["z_min_m"])
    meshroads = bdr._load_meshroads(proc, str((site.get("beamng") or {}).get("level_name") or ""))
    prepared = _prepare(roads, cfg, z_at, meshroads, z_min_m=z_min_m)
    print(
        f"corridor filter: {len(prepared)} of {len(roads)} carriageways",
        flush=True,
    )
    if not prepared:
        raise SystemExit("no carriageways to filter")
    n_nan = sum(1 for item in prepared for n in item["nodes"] if not math.isfinite(n["z"]))
    if n_nan:
        raise SystemExit(f"{n_nan} centerline samples have no 0.5 m elevation")

    out_dir = proc / "corridor50_road"
    out_dir.mkdir(parents=True, exist_ok=True)

    bpad = float(
        cfg.get("road_bed_bridge_pad_m")
        if cfg.get("road_bed_bridge_pad_m") is not None
        else cfg.get("bridge_clip_pad_m") or 2.0
    )
    bridges = bdr._load_bridge_bands(proc, pad_m=bpad, extend_m=0.0, prefer_under=False)
    galleries = bdr._load_gallery_corridors(proc, cfg)
    max_raise0 = float(cfg.get("road_bed_max_raise_m") or cfg.get("road_bed_max_delta_m") or 2.0)
    max_cut0 = float(cfg.get("road_bed_max_cut_m") or cfg.get("road_bed_max_delta_m") or 2.0)
    halo_px = int(round(HALO_M / RES_M))

    specs = sorted(tiles.values(), key=lambda s: (int(s["iy"]), int(s["ix"])))
    stages = "--stages" in sys.argv
    mosaic = _stage_mosaic(specs, proc / "corridor50_stages") if stages else None
    full = raster_frame(specs)
    surface = None
    surface_path = out_dir / "_mosaic.dat"
    if not stages:
        if surface_path.exists():
            surface_path.unlink()
        surface = np.memmap(
            surface_path,
            dtype=np.float32,
            mode="w+",
            shape=(full["height"], full["width"]),
        )
        surface[:] = np.nan
    n_mask = 0
    n_shelf = 0
    n_clamp = 0
    max_delta = 0.0
    for i, spec in enumerate(specs, start=1):
        frame = _window_frame(spec, halo_px)
        x = float(spec["xmin"]) + 0.5 * RES_M
        y = float(spec["ymax"]) - 0.5 * RES_M
        px, py = bb._to_px_beamng(
            x - frame["x0c"],
            y + (frame["extent"] - frame["y0c"]),
            frame["side"],
            frame["extent"],
        )
        if abs(px - halo_px) > 1e-4 or abs(py - halo_px) > 1e-4:
            raise SystemExit(f"pixel frame off on {spec['name']}: {px:.4f},{py:.4f}")
        shifted = [_shift_item(item, sc, frame, scale) for item in prepared]
        base = _paste_base(tiles, frame)
        if not np.isfinite(base).any():
            print(f"  {i}/{len(specs)} {spec['name']} no elevation", flush=True)
            continue
        protected = _paint_spans(frame, bridges, sc, scale)
        protected |= _paint_spans(frame, galleries, sc, scale)
        out, opacity, bad, _filled, filtered = _filter_window(
            shifted,
            base.astype(np.float64),
            protected=protected,
            max_raise0=max_raise0,
            max_cut0=max_cut0,
        )
        strip = _mask_strip(shifted, frame)
        h = frame["height"]
        w = frame["width"]
        sl = (slice(halo_px, halo_px + h), slice(halo_px, halo_px + w))
        surf = np.array(out[sl], dtype=np.float32)
        raw = base[sl]
        keep = strip[sl]
        shelf = opacity[sl] >= 0.999
        delta = np.abs(surf - raw)
        hit = keep & np.isfinite(delta)
        if np.any(hit):
            max_delta = max(max_delta, float(np.max(delta[hit])))
        n_mask += int(np.count_nonzero(keep))
        n_shelf += int(np.count_nonzero(shelf & keep))
        n_clamp += int(np.count_nonzero(bad[sl] & keep))
        if mosaic is not None:
            _paste_stage(mosaic, spec, keep, raw, filtered[sl], surf)
            print(
                f"  {i}/{len(specs)} {spec['name']} mask={int(keep.sum())}",
                flush=True,
            )
            continue
        _stamp_centerline(prepared, sc, spec, surf)
        surf[~keep] = np.nan
        r0 = int(round((full["ymax"] - float(spec["ymax"])) / RES_M))
        c0 = int(round((float(spec["xmin"]) - full["xmin"]) / RES_M))
        surface[r0 : r0 + h, c0 : c0 + w] = surf
        n_here = int(np.count_nonzero(keep))
        print(
            f"  {i}/{len(specs)} {spec['name']} mask={n_here} "
            f"shelf={int(np.count_nonzero(shelf & keep))}",
            flush=True,
        )

    if mosaic is not None:
        _write_stage_rasters(mosaic)
        return

    _write_profile(out_dir / "profile.csv", prepared)
    raster_name = "corridor50_road.tif"
    write_referenced_tif(
        out_dir / raster_name,
        np.asarray(surface),
        xmin=full["xmin"],
        ymax=full["ymax"],
        res=RES_M,
        epsg=int(str(sc.crs).split(":")[-1]),
    )
    surface.flush()
    del surface
    surface_path.unlink(missing_ok=True)
    for old in out_dir.glob("r*.tif"):
        try:
            old.unlink()
        except OSError:
            print(f"  could not remove {old.name}", flush=True)
    index = {
        "raster": raster_name,
        "resolution_m": RES_M,
        "mask_m": MASK_M,
        "halo_m": HALO_M,
        "sink_m": SINK_M,
        "blur_sigma_m": [1.5, 2.5, 4.0],
        "crs": sc.crs,
        "roads": len(prepared),
        "mask_px": n_mask,
        "shelf_px": n_shelf,
        "clamp_reject_px": n_clamp,
        "max_delta_m": round(max_delta, 3),
        "elapsed_s": round(time.perf_counter() - t0, 1),
        "xmin": full["xmin"],
        "ymin": full["ymin"],
        "xmax": full["xmax"],
        "ymax": full["ymax"],
        "width": full["width"],
        "height": full["height"],
        "row0": "north",
    }
    (out_dir / "corridor50_road_index.json").write_text(
        json.dumps(index, indent=2), encoding="utf-8"
    )
    print(
        f"Wrote {out_dir / raster_name} "
        f"{full['width']}x{full['height']} "
        f"roads={len(prepared)} mask_px={n_mask} shelf_px={n_shelf} "
        f"clamp={n_clamp} max_delta={max_delta:.3f}m elapsed={index['elapsed_s']}s",
        flush=True,
    )


def raster_frame(specs: list[dict]) -> dict:
    """Outer edges of every tile. Row 0 of the mosaic is north."""
    xmin = min(float(s["xmin"]) for s in specs)
    ymax = max(float(s["ymax"]) for s in specs)
    xmax = max(float(s["xmax"]) for s in specs)
    ymin = min(float(s["ymin"]) for s in specs)
    return {
        "xmin": xmin,
        "ymin": ymin,
        "xmax": xmax,
        "ymax": ymax,
        "width": int(round((xmax - xmin) / RES_M)),
        "height": int(round((ymax - ymin) / RES_M)),
    }


def write_referenced_tif(
    path: Path,
    arr: np.ndarray,
    *,
    xmin: float,
    ymax: float,
    res: float,
    epsg: int = 31254,
) -> None:
    """GeoTIFF plus world file. Pixel (0, 0) is the north-west cell.

    The tiepoint is the outer north-west corner. The world file names the
    centre of that cell, which is the same grid.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    scale = (float(res), float(res), 0.0)
    tie = (0.0, 0.0, 0.0, float(xmin), float(ymax), 0.0)
    keys = [
        1, 1, 0, 4,
        1024, 0, 1, 1,
        1025, 0, 1, 1,
        3072, 0, 1, int(epsg),
        3076, 0, 1, 9001,
    ]
    photo = {}
    if arr.ndim == 3 and arr.shape[-1] in (3, 4):
        photo["photometric"] = "rgb"
    tiff.imwrite(
        path,
        arr,
        compression="deflate",
        extratags=[
            (33550, "d", 3, scale, True),
            (33922, "d", 6, tie, True),
            (34735, "H", len(keys), keys, True),
        ],
        **photo,
    )
    x_center = float(xmin) + 0.5 * float(res)
    y_center = float(ymax) - 0.5 * float(res)
    path.with_suffix(".tfw").write_text(
        f"{res}\n0\n0\n{-res}\n{x_center}\n{y_center}\n",
        encoding="utf-8",
    )


def _stage_mosaic(specs: list[dict], out_dir: Path) -> dict:
    """One raster for the whole corridor. Row 0 is north."""
    xmin = min(float(s["xmin"]) for s in specs)
    ymax = max(float(s["ymax"]) for s in specs)
    xmax = max(float(s["xmax"]) for s in specs)
    ymin = min(float(s["ymin"]) for s in specs)
    width = int(round((xmax - xmin) / RES_M))
    height = int(round((ymax - ymin) / RES_M))
    out_dir.mkdir(parents=True, exist_ok=True)
    layers = {
        "auswahl": np.memmap(out_dir / "_auswahl.dat", dtype=np.uint8, mode="w+", shape=(height, width)),
        "fuellung": np.memmap(out_dir / "_fuellung.dat", dtype=np.float32, mode="w+", shape=(height, width)),
        "filterung": np.memmap(out_dir / "_filterung.dat", dtype=np.float32, mode="w+", shape=(height, width)),
        "endzustand": np.memmap(out_dir / "_endzustand.dat", dtype=np.float32, mode="w+", shape=(height, width)),
    }
    layers["auswahl"][:] = 0
    for name in ("fuellung", "filterung", "endzustand"):
        layers[name][:] = np.nan
    return {
        "xmin": xmin,
        "ymin": ymin,
        "xmax": xmax,
        "ymax": ymax,
        "width": width,
        "height": height,
        "dir": out_dir,
        "layers": layers,
    }


def _paste_stage(mosaic: dict, spec: dict, keep: np.ndarray, raw, filtered, final) -> None:
    c0 = int(round((float(spec["xmin"]) - mosaic["xmin"]) / RES_M))
    r0 = int(round((mosaic["ymax"] - float(spec["ymax"])) / RES_M))
    h, w = keep.shape
    sel = mosaic["layers"]["auswahl"]
    view = sel[r0 : r0 + h, c0 : c0 + w]
    view[keep] = 255
    sel[r0 : r0 + h, c0 : c0 + w] = view
    for key, src in (
        ("fuellung", raw),
        ("filterung", filtered),
        ("endzustand", final),
    ):
        band = np.array(src, dtype=np.float32, copy=True)
        band[~keep] = np.nan
        dst = mosaic["layers"][key]
        block = np.array(dst[r0 : r0 + h, c0 : c0 + w], copy=True)
        block[keep] = band[keep]
        dst[r0 : r0 + h, c0 : c0 + w] = block


def _hillshade(z: np.ndarray, pixel_m: float) -> np.ndarray:
    finite = np.isfinite(z)
    src = np.array(z, dtype=np.float64, copy=True)
    if not finite.all():
        src[~finite] = np.nanmean(src) if finite.any() else 0.0
    gy, gx = np.gradient(src, pixel_m)
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    zenith = math.radians(45.0)
    azimuth = math.radians(315.0)
    shade = np.cos(zenith) * np.cos(slope) + np.sin(zenith) * np.sin(slope) * np.cos(azimuth - aspect)
    shade = np.clip(shade, 0.0, 1.0)
    out = np.zeros(shade.shape, dtype=np.uint8)
    out[finite] = (shade[finite] * 255.0).astype(np.uint8)
    return out


def _write_stage_preview(mosaic: dict) -> None:
    factor = 8
    pixel_m = RES_M * factor
    height, width = mosaic["height"], mosaic["width"]
    h2, w2 = height // factor * factor, width // factor * factor
    sel = np.array(mosaic["layers"]["auswahl"][:h2, :w2])
    sel_small = sel.reshape(h2 // factor, factor, w2 // factor, factor).max(axis=(1, 3))
    Image.fromarray(sel_small).save(mosaic["dir"] / "01_auswahl.png")
    for name, png in (
        ("fuellung", "02_fuellung.png"),
        ("filterung", "03_filterung.png"),
        ("endzustand", "04_endzustand.png"),
    ):
        z = np.array(mosaic["layers"][name][:h2, :w2], dtype=np.float32)
        block = z.reshape(h2 // factor, factor, w2 // factor, factor)
        with np.errstate(all="ignore"):
            small = np.nanmean(block, axis=(1, 3)).astype(np.float32)
        Image.fromarray(_hillshade(small, pixel_m)).save(mosaic["dir"] / png)


def _write_stage_rasters(mosaic: dict) -> None:
    out_dir = mosaic["dir"]
    names = {
        "auswahl": "01_auswahl.tif",
        "fuellung": "02_fuellung.tif",
        "filterung": "03_filterung.tif",
        "endzustand": "04_endzustand.tif",
    }
    for key, name in names.items():
        arr = np.asarray(mosaic["layers"][key])
        tiff.imwrite(out_dir / name, arr, compression="deflate")
        print(f"  wrote {name}", flush=True)
        mosaic["layers"][key].flush()
    _write_stage_preview(mosaic)
    paths = []
    for key in list(mosaic["layers"]):
        layer = mosaic["layers"].pop(key)
        paths.append(Path(layer.filename))
        del layer
    for path in paths:
        path.unlink(missing_ok=True)
    x_center = mosaic["xmin"] + 0.5 * RES_M
    y_center = mosaic["ymax"] - 0.5 * RES_M
    world = f"{RES_M}\n0\n0\n{-RES_M}\n{x_center}\n{y_center}\n"
    for name in names.values():
        (out_dir / f"{Path(name).stem}.tfw").write_text(world, encoding="utf-8")
    report = {
        "what": "Ein Raster für den ganzen Korridor, Zeile 0 im Norden.",
        "auswahl": "255 im 15-m-Streifen um die Mittelachse, 0 außerhalb",
        "fuellung": "rohes 0,5-m-DGM im Streifen, NaN außerhalb",
        "filterung": "nach den drei Glättungen, vor der Querneigung, NaN außerhalb",
        "endzustand": "nach der Querneigung, NaN außerhalb",
        "resolution_m": RES_M,
        "crs": "EPSG:31254",
        "xmin": mosaic["xmin"],
        "ymin": mosaic["ymin"],
        "xmax": mosaic["xmax"],
        "ymax": mosaic["ymax"],
        "width": mosaic["width"],
        "height": mosaic["height"],
    }
    (out_dir / "stages.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out_dir}", flush=True)


def _write_strip_masks(proc: Path) -> None:
    """uint8 copy of the 15 m strip already stored as finite pixels."""
    src = proc / "corridor50_road"
    index = json.loads((src / "corridor50_road_index.json").read_text(encoding="utf-8"))
    z = np.asarray(tiff.imread(src / index["raster"]), dtype=np.float32)
    if z.ndim == 3:
        z = z[..., 0]
    keep = np.isfinite(z)
    n = int(keep.sum())
    expect = int(index["mask_px"])
    if n != expect:
        raise SystemExit(f"strip mask {n} px, height index says {expect}")
    out = proc / "corridor50_mask.tif"
    write_referenced_tif(
        out,
        (keep.astype(np.uint8) * 255),
        xmin=float(index["xmin"]),
        ymax=float(index["ymax"]),
        res=float(index["resolution_m"]),
        epsg=int(str(index.get("crs") or "EPSG:31254").split(":")[-1]),
    )
    old_dir = proc / "corridor50_mask"
    if old_dir.is_dir():
        for stale in old_dir.glob("*"):
            try:
                stale.unlink()
            except OSError:
                print(f"  could not remove {stale.name}", flush=True)
        try:
            old_dir.rmdir()
        except OSError:
            pass
    print(f"Wrote {out} mask_px={n}", flush=True)


if __name__ == "__main__":
    if "--masks-only" in sys.argv:
        _write_strip_masks(processed_dir(load_site()))
    else:
        main()
