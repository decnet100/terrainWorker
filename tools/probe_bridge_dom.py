"""One bridge: 0.5 m surface model beside the corridor ground model.

Samples the centerline and three cross-sections. Writes a plot. Does not
touch the repaired raster, the heightmap, or a BeamNG level.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\probe_bridge_dom.py --oid 3952
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import tifffile as tiff

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from site_coords import SiteCoords, load_site, processed_dir, site_slug  # noqa: E402
from wcs_terrain import fetch_terrain_coverage  # noqa: E402

RES_M = 0.5
EXTEND_M = 20.0
HALF_M = 12.0
STEP_M = 0.5


def _road(proc: Path, oid: int) -> dict:
    roads = json.loads((proc / "gip_roads_beamng.json").read_text(encoding="utf-8"))
    road = roads.get(str(oid))
    if road is None:
        raise SystemExit(f"object {oid} is not in gip_roads_beamng.json")
    nodes = road.get("nodes") or []
    if len(nodes) < 2:
        raise SystemExit(f"object {oid} has no centerline")
    return road


def _crs_line(road: dict, sc: SiteCoords, z_min: float) -> tuple[np.ndarray, np.ndarray]:
    xy = []
    z = []
    for node in road["nodes"]:
        x, y = sc.beamng_to_crs(float(node[0]), float(node[1]))
        xy.append((x, y))
        z.append(float(node[2]) + z_min)
    return np.asarray(xy, dtype=np.float64), np.asarray(z, dtype=np.float64)


def _extend(xy: np.ndarray, dist: float) -> np.ndarray:
    def _end(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        d = b - a
        n = float(np.hypot(d[0], d[1]))
        if n < 1e-6:
            return a.copy()
        return a - d / n * dist

    pre = _end(xy[0], xy[1])
    post = _end(xy[-1], xy[-2])
    return np.vstack([pre, xy, post])


def _chain(xy: np.ndarray) -> np.ndarray:
    step = np.hypot(np.diff(xy[:, 0]), np.diff(xy[:, 1]))
    return np.concatenate([[0.0], np.cumsum(step)])


def _densify(xy: np.ndarray, step: float) -> tuple[np.ndarray, np.ndarray]:
    cum = _chain(xy)
    total = float(cum[-1])
    if total < step:
        return xy.copy(), cum
    stations = np.arange(0.0, total + step * 0.5, step)
    if stations[-1] < total - 1e-6:
        stations = np.concatenate([stations, [total]])
    out = np.column_stack(
        [
            np.interp(stations, cum, xy[:, 0]),
            np.interp(stations, cum, xy[:, 1]),
        ]
    )
    return out, stations


def _normals(xy: np.ndarray) -> np.ndarray:
    d = np.gradient(xy, axis=0)
    n = np.hypot(d[:, 0], d[:, 1])
    n = np.maximum(n, 1e-9)
    # Left of the direction of travel.
    return np.column_stack([-d[:, 1] / n, d[:, 0] / n])


def _load_dgm(path: Path) -> tuple[np.ndarray, dict]:
    with tiff.TiffFile(path) as src:
        page = src.pages[0]
        z = np.asarray(page.asarray(), dtype=np.float32)
        scale = tuple(page.tags["ModelPixelScaleTag"].value)
        tie = tuple(page.tags["ModelTiepointTag"].value)
    if z.ndim == 3:
        z = z[..., 0]
    bad = (~np.isfinite(z)) | (z < 50.0) | (z > 4500.0)
    z = z.copy()
    z[bad] = np.nan
    res = float(scale[0])
    height, width = z.shape
    xmin = float(tie[3])
    ymax = float(tie[4])
    return z, {
        "xmin": xmin,
        "ymin": ymax - height * res,
        "xmax": xmin + width * res,
        "ymax": ymax,
        "res": res,
    }


def _snap_bbox(xmin, ymin, xmax, ymax, origin_x, origin_y, res) -> tuple[float, float, float, float]:
    xmin = origin_x + math.floor((xmin - origin_x) / res) * res
    ymin = origin_y + math.floor((ymin - origin_y) / res) * res
    xmax = origin_x + math.ceil((xmax - origin_x) / res) * res
    ymax = origin_y + math.ceil((ymax - origin_y) / res) * res
    return xmin, ymin, xmax, ymax


def _bilinear(arr: np.ndarray, frame: dict, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    res = float(frame["res"])
    col = (x - frame["xmin"]) / res - 0.5
    row = (frame["ymax"] - y) / res - 0.5
    c0 = np.floor(col).astype(np.int32)
    r0 = np.floor(row).astype(np.int32)
    c1 = c0 + 1
    r1 = r0 + 1
    h, w = arr.shape
    ok = (c0 >= 0) & (r0 >= 0) & (c1 < w) & (r1 < h)
    out = np.full(x.shape, np.nan, dtype=np.float64)
    if not np.any(ok):
        return out
    tc = col[ok] - c0[ok]
    tr = row[ok] - r0[ok]
    v00 = arr[r0[ok], c0[ok]]
    v10 = arr[r0[ok], c1[ok]]
    v01 = arr[r1[ok], c0[ok]]
    v11 = arr[r1[ok], c1[ok]]
    out[ok] = (
        v00 * (1.0 - tc) * (1.0 - tr)
        + v10 * tc * (1.0 - tr)
        + v01 * (1.0 - tc) * tr
        + v11 * tc * tr
    )
    return out


def _quantile(v: np.ndarray, q: float) -> float | None:
    finite = v[np.isfinite(v)]
    if finite.size == 0:
        return None
    return float(np.quantile(finite, q))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--oid", type=int, default=3952)
    parser.add_argument("--extend", type=float, default=EXTEND_M)
    args = parser.parse_args()
    extend = max(0.0, float(args.extend))

    site = load_site()
    sc = SiteCoords(site)
    proc = processed_dir(site)
    meta = json.loads((proc / "heightmap_meta.json").read_text(encoding="utf-8"))
    z_min = float(meta["z_min_m"])
    road = _road(proc, args.oid)
    name = str(road.get("kunstbauten") or road.get("name") or args.oid)
    xy, z_node = _crs_line(road, sc, z_min)
    cum_node = _chain(xy)
    extended = _extend(xy, extend)
    line, station = _densify(extended, STEP_M)
    # Station 0 is the first GIP node. The extension lies before it.
    station = station - extend
    normal = _normals(line)
    span = float(cum_node[-1])

    pad = HALF_M + 4.0
    xmin, ymin, xmax, ymax = _snap_bbox(
        float(line[:, 0].min()) - pad,
        float(line[:, 1].min()) - pad,
        float(line[:, 0].max()) + pad,
        float(line[:, 1].max()) + pad,
        sc.xmin,
        sc.ymin,
        RES_M,
    )
    dom_path = ROOT / "data" / "raw" / f"dom_{site_slug(site)}_oid{args.oid}.tif"
    fetch_terrain_coverage(
        site,
        "dom",
        label=f"DOM {args.oid}",
        bbox=(xmin, ymin, xmax, ymax),
        resolution_m=RES_M,
        out_path=dom_path,
        allow_incomplete=True,
    )
    dom = np.asarray(tiff.imread(dom_path), dtype=np.float32)
    if dom.ndim == 3:
        dom = dom[..., 0]
    bad = (~np.isfinite(dom)) | (dom < 50.0) | (dom > 4500.0)
    dom = dom.copy()
    dom[bad] = np.nan
    dom_frame = {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax, "res": RES_M}
    dgm, dgm_frame = _load_dgm(proc / "corridor50_raw" / "corridor50_raw.tif")

    z_dom = _bilinear(dom, dom_frame, line[:, 0], line[:, 1])
    z_dgm = _bilinear(dgm, dgm_frame, line[:, 0], line[:, 1])
    z_gip = np.interp(station, cum_node, z_node, left=np.nan, right=np.nan)
    chord = np.interp(station, [0.0, span], [z_node[0], z_node[-1]], left=np.nan, right=np.nan)

    on_span = (station >= 0.0) & (station <= span)
    diff = z_dom - z_dgm
    summary = {
        "oid": args.oid,
        "name": name,
        "str_code": road.get("str_code"),
        "span_m": round(span, 2),
        "bbox": [xmin, ymin, xmax, ymax],
        "dom": str(dom_path),
        "centerline_on_span": {
            "n": int(np.isfinite(diff[on_span]).sum()),
            "dom_minus_dgm_p10_m": _quantile(diff[on_span], 0.10),
            "dom_minus_dgm_p50_m": _quantile(diff[on_span], 0.50),
            "dom_minus_dgm_p90_m": _quantile(diff[on_span], 0.90),
            "dom_minus_dgm_max_m": _quantile(diff[on_span], 1.0),
        },
        "end_node_minus_dgm_m": [
            None if not np.isfinite(z_dgm[np.argmin(np.abs(station))]) else round(
                float(z_node[0] - z_dgm[np.argmin(np.abs(station))]), 3
            ),
            None if not np.isfinite(z_dgm[np.argmin(np.abs(station - span))]) else round(
                float(z_node[-1] - z_dgm[np.argmin(np.abs(station - span))]), 3
            ),
        ],
    }

    cuts = [0.0, 0.5 * span, span]
    offsets = np.arange(-HALF_M, HALF_M + STEP_M * 0.5, STEP_M)
    cross = []
    for s_cut in cuts:
        i = int(np.argmin(np.abs(station - s_cut)))
        origin = line[i]
        nrm = normal[i]
        pts = origin + offsets[:, None] * nrm
        cross.append(
            {
                "station_m": float(station[i]),
                "offset_m": offsets,
                "dom": _bilinear(dom, dom_frame, pts[:, 0], pts[:, 1]),
                "dgm": _bilinear(dgm, dgm_frame, pts[:, 0], pts[:, 1]),
            }
        )

    out_dir = proc / f"dom_bridge_{args.oid}"
    out_dir.mkdir(parents=True, exist_ok=True)
    fig = plt.figure(figsize=(11.5, 8.2))
    ax_h = fig.add_axes((0.07, 0.58, 0.90, 0.36))
    ax_d = fig.add_axes((0.07, 0.38, 0.90, 0.16))
    ax_h.plot(station, z_dgm, color="#8a8f98", linewidth=1.2, label="Gelände")
    ax_h.plot(station, z_dom, color="#1f4e79", linewidth=1.3, label="Oberfläche")
    ax_h.plot(cum_node, z_node, color="#111111", marker="o", linestyle="none", markersize=4, label="Höhenkarte an den Knoten")
    ax_h.plot(station, chord, color="#c2410c", linewidth=1.0, linestyle="--", label="Verbindung der Endknoten")
    for s_cut in cuts:
        ax_h.axvline(s_cut, color="#c5c9d0", linewidth=0.6)
    ax_h.set_ylabel("Höhe m")
    ax_h.set_title(f"{name}, OID {args.oid}, {road.get('str_code') or ''}".strip())
    ax_h.legend(frameon=False, fontsize=8, ncol=2)
    ax_d.plot(station, diff, color="#1f4e79", linewidth=1.0)
    ax_d.axhline(0.0, color="#c5c9d0", linewidth=0.6)
    ax_d.set_ylabel("Oberfläche minus Gelände m")
    ax_d.set_xlabel("Station m, erster Knoten ist 0")

    for k, item in enumerate(cross):
        ax = fig.add_axes((0.07 + k * 0.31, 0.06, 0.28, 0.24))
        ax.plot(item["offset_m"], item["dgm"], color="#8a8f98", linewidth=1.1, label="Gelände")
        ax.plot(item["offset_m"], item["dom"], color="#1f4e79", linewidth=1.2, label="Oberfläche")
        ax.axvline(0.0, color="#c5c9d0", linewidth=0.6)
        ax.set_xlabel("Abstand m, links positiv")
        if k == 0:
            ax.set_ylabel("Höhe m")
            ax.legend(frameon=False, fontsize=7)
        ax.set_title(f"Station {item['station_m']:.1f} m", fontsize=9)

    png = out_dir / "profil.png"
    fig.savefig(png, dpi=120)
    plt.close(fig)
    summary["png"] = str(png)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
