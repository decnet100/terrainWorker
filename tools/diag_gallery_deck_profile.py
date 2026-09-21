"""Longitudinal profile: gallery/tunnel deck vs DGM vs composed heightmap.

Samples along the daylight plate (portal → abutment) plus a short run into
the bore and past the pad. Writes CSV + a PNG under processed/portal_peek/.

```powershell
cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/reschen.yaml"; python tools\\diag_gallery_deck_profile.py --name landecker --inject-debug
```
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from site_coords import SiteCoords, load_site, processed_dir  # noqa: E402
import build_bridges as bb  # noqa: E402
from build_galleries import _daylight_deck_nodes  # noqa: E402


def _load_hm(path: Path, max_h: float) -> np.ndarray:
    arr = np.asarray(Image.open(path), dtype=np.float64)
    if arr.ndim == 3:
        arr = arr[..., 0]
    return arr / 65535.0 * float(max_h)


def _z_at(elev: np.ndarray, bx: float, by: float, extent: float) -> float:
    n = int(elev.shape[0])
    px = bx / extent * (n - 1)
    py = (1.0 - by / extent) * (n - 1)
    px = max(0.0, min(n - 1.001, px))
    py = max(0.0, min(n - 1.001, py))
    c0 = int(math.floor(px))
    r0 = int(math.floor(py))
    c1 = min(n - 1, c0 + 1)
    r1 = min(n - 1, r0 + 1)
    tx = px - c0
    ty = py - r0
    z00 = float(elev[r0, c0])
    z10 = float(elev[r0, c1])
    z01 = float(elev[r1, c0])
    z11 = float(elev[r1, c1])
    return (1 - ty) * ((1 - tx) * z00 + tx * z10) + ty * ((1 - tx) * z01 + tx * z11)


def _cum_xy(nodes: list[dict]) -> list[float]:
    cum = [0.0]
    for a, b in zip(nodes, nodes[1:]):
        cum.append(
            cum[-1]
            + math.hypot(float(b["x"]) - float(a["x"]), float(b["y"]) - float(a["y"]))
        )
    return cum


def _sample_nodes(nodes: list[dict], step_m: float) -> list[dict]:
    if len(nodes) < 2:
        return list(nodes)
    cum = _cum_xy(nodes)
    total = cum[-1]
    out: list[dict] = []
    s = 0.0
    i = 0
    while s <= total + 1e-9:
        while i + 1 < len(cum) and cum[i + 1] < s - 1e-12:
            i += 1
        if i + 1 >= len(nodes):
            i = len(nodes) - 2
        span = max(cum[i + 1] - cum[i], 1e-9)
        t = (s - cum[i]) / span
        a, b = nodes[i], nodes[i + 1]
        out.append(
            {
                "s_node": float(a["s"]) + t * (float(b["s"]) - float(a["s"])),
                "x": float(a["x"]) + t * (float(b["x"]) - float(a["x"])),
                "y": float(a["y"]) + t * (float(b["y"]) - float(a["y"])),
                "z_road": float(a["z_road"]) + t * (float(b["z_road"]) - float(a["z_road"])),
                "tx": float(b["x"]) - float(a["x"]),
                "ty": float(b["y"]) - float(a["y"]),
                "along": s,
            }
        )
        s += step_m
    last = nodes[-1]
    if out and math.hypot(out[-1]["x"] - float(last["x"]), out[-1]["y"] - float(last["y"])) > 0.05:
        out.append(
            {
                "s_node": float(last["s"]),
                "x": float(last["x"]),
                "y": float(last["y"]),
                "z_road": float(last["z_road"]),
                "tx": float(last["x"]) - float(nodes[-2]["x"]),
                "ty": float(last["y"]) - float(nodes[-2]["y"]),
                "along": total,
            }
        )
    return out


def _window_nodes(nodes: list[dict], s_lo: float, s_hi: float) -> list[dict]:
    if s_hi < s_lo:
        s_lo, s_hi = s_hi, s_lo
    out = [n for n in nodes if s_lo - 1e-6 <= float(n["s"]) <= s_hi + 1e-6]
    return out if len(out) >= 2 else []


def _nearest_on_poly(
    px: float, py: float, xy: list[tuple[float, float]]
) -> tuple[float, float, float]:
    best_d2 = 1e18
    qx, qy = px, py
    for (ax, ay), (bx, by) in zip(xy, xy[1:]):
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        if seg2 < 1e-18:
            t = 0.0
        else:
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
        sx, sy_ = ax + t * dx, ay + t * dy
        d2 = (px - sx) ** 2 + (py - sy_) ** 2
        if d2 < best_d2:
            best_d2 = d2
            qx, qy = sx, sy_
    return qx, qy, math.sqrt(best_d2)


def _collect_gip_tracks(site: dict, g: dict, roads: dict) -> list[dict]:
    """GIP polylines that this gallery's assumed axis is derived from."""
    from gip_road_segments import load_unify_specs

    specs = {s["id"]: s for s in load_unify_specs(site)}
    bore_oids: list[int] = []
    for x in g.get("merged_from") or []:
        try:
            bore_oids.append(int(x))
        except (TypeError, ValueError):
            pass
    if g.get("objectid") is not None:
        try:
            bore_oids.append(int(g["objectid"]))
        except (TypeError, ValueError):
            pass
    uid = str(g.get("unify_id") or "")
    if uid in specs:
        bore_oids.extend(int(x) for x in specs[uid]["objectids"])
    app_oids: list[int] = []
    app_uid = str(g.get("append_unify") or "")
    if app_uid in specs:
        app_oids.extend(int(x) for x in specs[app_uid]["objectids"])
    tracks: list[dict] = []
    seen: set[int] = set()

    def _add(oids: list[int], group: str) -> None:
        for oid in oids:
            if oid in seen:
                continue
            road = roads.get(str(oid))
            if not road:
                continue
            xy = [(float(n[0]), float(n[1])) for n in (road.get("nodes") or [])]
            if len(xy) < 2:
                continue
            seen.add(oid)
            tracks.append(
                {
                    "oid": oid,
                    "group": group,
                    "xy": xy,
                    "label": str(road.get("name") or oid),
                }
            )

    _add(bore_oids, "bore")
    _add(app_oids, "approach")
    return tracks


_GIP_RGB = [
    (220, 90, 20),
    (20, 150, 70),
    (40, 90, 210),
    (190, 40, 40),
    (160, 140, 20),
    (120, 60, 180),
]


def _gip_color(i: int) -> tuple[int, int, int]:
    return _GIP_RGB[i % len(_GIP_RGB)]


def _gip_rgb01(i: int) -> tuple[float, float, float]:
    r, g, b = _gip_color(i)
    return (r / 255.0, g / 255.0, b / 255.0)


def _fonts():
    try:
        return (
            ImageFont.truetype("arial.ttf", 14),
            ImageFont.truetype("arial.ttf", 12),
        )
    except OSError:
        f = ImageFont.load_default()
        return f, f


def _panel_xy(box: tuple[int, int, int, int], xmin: float, xmax: float, ymin: float, ymax: float):
    l, t, r, b = box

    def sx(x: float) -> float:
        return l + (x - xmin) / (xmax - xmin) * (r - l)

    def sy(y: float) -> float:
        return t + (1.0 - (y - ymin) / (ymax - ymin)) * (b - t)

    return sx, sy


def _draw_profile(
    path: Path,
    rows: list[dict],
    *,
    title: str,
    d_portal: float,
    d_pad0: float | None,
    d_pad1: float | None,
    lat_series: list[tuple[str, str, tuple[int, int, int]]],
) -> None:
    """Top: Z vs d. Bottom: GIP lateral offset vs assumed centerline (0 = on axis, + = left)."""
    w, h = 1100, 820
    gap = 18
    pad_l, pad_r, pad_t, pad_b = 70, 24, 32, 40
    mid_h = 48
    top_b = 390
    bot_t = top_b + gap + mid_h
    img = Image.new("RGB", (w, h), (250, 250, 248))
    dr = ImageDraw.Draw(img)
    font, font_s = _fonts()

    xs = [float(r["d_portal"]) for r in rows]
    xmin, xmax = min(xs), max(xs)
    if abs(xmax - xmin) < 1e-6:
        xmax = xmin + 1.0

    z_series = {
        "DGM": ("dgm", (140, 140, 140)),
        "composed": ("composed", (30, 90, 180)),
        "deck": ("deck", (20, 20, 20)),
    }
    if any(r.get("imported") is not None for r in rows):
        z_series["import"] = ("imported", (0, 140, 90))
    ys: list[float] = []
    for r in rows:
        for key, _c in z_series.values():
            v = r.get(key)
            if v is not None:
                ys.append(float(v))
    ymin, ymax = min(ys), max(ys)
    ypad = max(0.4, 0.08 * (ymax - ymin or 1.0))
    ymin -= ypad
    ymax += ypad

    box_z = (pad_l, pad_t + 8, w - pad_r, top_b)
    sx, sy = _panel_xy(box_z, xmin, xmax, ymin, ymax)
    dr.rectangle(list(box_z), outline=(180, 180, 180), fill=(255, 255, 255))
    for gx in range(int(math.floor(xmin / 10) * 10), int(math.ceil(xmax / 10) * 10) + 1, 10):
        x = sx(float(gx))
        dr.line([(x, box_z[1]), (x, box_z[3])], fill=(230, 230, 230), width=1)
    z0 = math.floor(ymin)
    z1 = math.ceil(ymax)
    step_z = 1 if (z1 - z0) <= 12 else 2
    z = z0
    while z <= z1:
        y = sy(float(z))
        dr.line([(box_z[0], y), (box_z[2], y)], fill=(230, 230, 230), width=1)
        dr.text((8, y - 7), f"{z:.0f}", fill=(80, 80, 80), font=font_s)
        z += step_z
    for xmark, _label, col in (
        (d_portal, "portal", (180, 40, 40)),
        (d_pad0, "pad", (180, 120, 20)),
        (d_pad1, "end", (180, 120, 20)),
    ):
        if xmark is None:
            continue
        x = sx(float(xmark))
        dr.line([(x, box_z[1]), (x, box_z[3])], fill=col, width=1)
    for name, (key, col) in z_series.items():
        pts = [
            (sx(float(r["d_portal"])), sy(float(r[key])))
            for r in rows
            if r.get(key) is not None
        ]
        if len(pts) >= 2:
            dr.line(pts, fill=col, width=2)
    dr.text((pad_l, 6), title, fill=(20, 20, 20), font=font)
    lx = pad_l
    for name, (_key, col) in z_series.items():
        dr.rectangle([lx, top_b + 8, lx + 14, top_b + 20], fill=col)
        dr.text((lx + 18, top_b + 6), name, fill=(40, 40, 40), font=font_s)
        lx += 90 + 8 * len(name)

    lats: list[float] = []
    for r in rows:
        for key, _leg, _c in lat_series:
            v = r.get(key)
            if v is not None:
                lats.append(float(v))
    if lats:
        lmin, lmax = min(lats), max(lats)
    else:
        lmin, lmax = -2.0, 2.0
    lpad = max(0.4, 0.15 * (lmax - lmin or 1.0))
    lmin = min(lmin - lpad, -0.5)
    lmax = max(lmax + lpad, 0.5)
    box_l = (pad_l, bot_t, w - pad_r, h - pad_b)
    sx2, sy2 = _panel_xy(box_l, xmin, xmax, lmin, lmax)
    dr.rectangle(list(box_l), outline=(180, 180, 180), fill=(255, 255, 255))
    for gx in range(int(math.floor(xmin / 10) * 10), int(math.ceil(xmax / 10) * 10) + 1, 10):
        x = sx2(float(gx))
        dr.line([(x, box_l[1]), (x, box_l[3])], fill=(230, 230, 230), width=1)
        dr.text((x - 8, box_l[3] + 6), str(gx), fill=(80, 80, 80), font=font_s)
    for gv in (-4, -2, -1, 0, 1, 2, 4):
        if gv < lmin or gv > lmax:
            continue
        y = sy2(float(gv))
        dr.line(
            [(box_l[0], y), (box_l[2], y)],
            fill=(40, 40, 40) if gv == 0 else (230, 230, 230),
            width=1,
        )
        dr.text((8, y - 7), f"{gv:+.0f}", fill=(80, 80, 80), font=font_s)
    for xmark, _label, col in (
        (d_portal, "portal", (180, 40, 40)),
        (d_pad0, "pad", (180, 120, 20)),
        (d_pad1, "end", (180, 120, 20)),
    ):
        if xmark is None:
            continue
        x = sx2(float(xmark))
        dr.line([(x, box_l[1]), (x, box_l[3])], fill=col, width=1)
    for key, _leg, col in lat_series:
        pts = [
            (sx2(float(r["d_portal"])), sy2(float(r[key])))
            for r in rows
            if r.get(key) is not None
        ]
        if len(pts) >= 2:
            dr.line(pts, fill=col, width=2)
    dr.text(
        (pad_l, bot_t - 22),
        "GIP lateral vs assumed centerline  (m;  0 = on axis,  + = left looking along travel)",
        fill=(20, 20, 20),
        font=font,
    )
    lx = pad_l
    for _key, leg, col in lat_series:
        dr.rectangle([lx, h - 18, lx + 14, h - 6], fill=col)
        dr.text((lx + 18, h - 20), leg, fill=(40, 40, 40), font=font_s)
        lx += 70 + 7 * len(leg)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def _draw_plan(
    path: Path,
    *,
    assumed: list[tuple[float, float]],
    tracks: list[dict],
    portal: tuple[float, float],
    left: tuple[float, float],
    half_w: float,
    title: str,
) -> None:
    """North-up plan: assumed axis vs GIP polylines around the portal."""
    w, h = 900, 720
    pad = 48
    img = Image.new("RGB", (w, h), (250, 250, 248))
    dr = ImageDraw.Draw(img)
    font, font_s = _fonts()
    ox, oy = portal
    radius = 45.0
    pts: list[tuple[float, float]] = list(assumed)
    for tr in tracks:
        for x, y in tr["xy"]:
            if math.hypot(x - ox, y - oy) <= radius + 8.0:
                pts.append((x, y))
    if not pts:
        pts = [(ox, oy)]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    xmin, xmax = min(xs) - 4.0, max(xs) + 4.0
    ymin, ymax = min(ys) - 4.0, max(ys) + 4.0
    span = max(xmax - xmin, ymax - ymin, 10.0)
    cx, cy = 0.5 * (xmin + xmax), 0.5 * (ymin + ymax)
    xmin, xmax = cx - 0.5 * span, cx + 0.5 * span
    ymin, ymax = cy - 0.5 * span, cy + 0.5 * span
    box = (pad, pad, w - pad, h - pad - 28)
    sx, sy = _panel_xy(box, xmin, xmax, ymin, ymax)
    dr.rectangle(list(box), outline=(180, 180, 180), fill=(255, 255, 255))
    for k in range(int(math.floor(xmin)), int(math.ceil(xmax)) + 1):
        if k % 10:
            continue
        x = sx(float(k))
        dr.line([(x, box[1]), (x, box[3])], fill=(230, 230, 230), width=1)
    for k in range(int(math.floor(ymin)), int(math.ceil(ymax)) + 1):
        if k % 10:
            continue
        y = sy(float(k))
        dr.line([(box[0], y), (box[2], y)], fill=(230, 230, 230), width=1)

    lx, ly = left
    e0 = (ox + half_w * lx, oy + half_w * ly)
    e1 = (ox - half_w * lx, oy - half_w * ly)
    dr.line([(sx(e0[0]), sy(e0[1])), (sx(e1[0]), sy(e1[1]))], fill=(200, 180, 20), width=3)

    for i, tr in enumerate(tracks):
        col = _gip_color(i)
        for piece in _clip_xy_near(tr["xy"], ox, oy, radius + 12.0):
            dr.line([(sx(x), sy(y)) for x, y in piece], fill=col, width=3)

    if len(assumed) >= 2:
        dr.line([(sx(x), sy(y)) for x, y in assumed], fill=(200, 20, 170), width=3)

    pr = 5
    dr.ellipse(
        [sx(ox) - pr, sy(oy) - pr, sx(ox) + pr, sy(oy) + pr],
        outline=(180, 40, 40),
        width=2,
    )
    dr.text((pad, 8), title, fill=(20, 20, 20), font=font)
    dr.text(
        (pad, h - 24),
        "magenta=assumed CL  yellow=7.5 m at portal  other=GIP OBJECTID  (Y up, metres)",
        fill=(40, 40, 40),
        font=font_s,
    )
    lxg = pad
    for i, tr in enumerate(tracks):
        col = _gip_color(i)
        dr.rectangle([lxg, box[3] + 6, lxg + 12, box[3] + 18], fill=col)
        dr.text(
            (lxg + 16, box[3] + 4),
            f"{tr['oid']} {tr['group']}",
            fill=(40, 40, 40),
            font=font_s,
        )
        lxg += 130
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def _clip_xy_near(
    xy: list[tuple[float, float]], ox: float, oy: float, radius: float
) -> list[list[tuple[float, float]]]:
    pieces: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    for x, y in xy:
        if math.hypot(x - ox, y - oy) <= radius:
            cur.append((x, y))
        elif cur:
            if len(cur) >= 2:
                pieces.append(cur)
            cur = []
    if len(cur) >= 2:
        pieces.append(cur)
    return pieces


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--name", default="landecker", help="gallery name / unify id")
    ap.add_argument("--step", type=float, default=1.0)
    ap.add_argument("--before", type=float, default=20.0, help="metres into the bore")
    ap.add_argument("--after", type=float, default=15.0, help="metres past the plate")
    ap.add_argument(
        "--inject-debug",
        action="store_true",
        help="Inject assumed axes as collision-free TSStatics (gallery_debug)",
    )
    args = ap.parse_args()

    site = load_site()
    sc = SiteCoords(site)
    proc = processed_dir(site)
    bng = site.get("beamng") or {}
    size = int(bng.get("mask_size") or 512)
    extent = float(sc.terrain_extent)
    cl_path = proc / "galleries_centerlines.json"
    if not cl_path.is_file():
        raise SystemExit(f"Missing {cl_path} — run build_galleries first")
    galleries = json.loads(cl_path.read_text(encoding="utf-8")).get("galleries") or []
    g = next(
        (
            x
            for x in galleries
            if str(x.get("name") or "").lower() == str(args.name).lower()
            or str(x.get("unify_id") or "").lower() == str(args.name).lower()
        ),
        None,
    )
    if g is None:
        names = [str(x.get("name")) for x in galleries]
        raise SystemExit(f"No gallery {args.name!r}. Have: {names}")

    nodes = g.get("nodes") or []
    plate = _daylight_deck_nodes(g, g)
    if len(plate) < 2:
        raise SystemExit(f"{args.name}: no daylight deck nodes")
    s_p = float((g.get("portal_s") or [None, None])[1 if g.get("fake_end") == "s0" else 0])
    fake = g.get("fake_end")
    out_s = 1.0 if fake == "s0" else -1.0
    abut_end = g.get("abutment_end_s")
    plate_len = (
        abs(float(abut_end) - s_p)
        if abut_end is not None
        else float(g.get("abutment_len_m") or 0.0)
    )
    s_lo = s_p - args.before * out_s
    s_hi = s_p + (plate_len + args.after) * out_s
    win = _window_nodes(nodes, s_lo, s_hi)
    if len(win) < 2:
        win = plate
    samples = _sample_nodes(win, max(0.25, float(args.step)))

    meta = json.loads((proc / "heightmap_meta.json").read_text(encoding="utf-8"))
    max_h = float(meta["max_height_m"])
    dgm = _load_hm(proc / f"heightmap_{size}.png", max_h)
    composed_p = proc / f"heightmap_{size}_composed.png"
    composed = _load_hm(composed_p, max_h) if composed_p.is_file() else dgm
    level_name = str(bng.get("level_name") or "")
    import_p = bb.USER_LEVELS / level_name / "import" / f"heightmap_{size}.png"
    imported = _load_hm(import_p, max_h) if import_p.is_file() else None

    from gip_road_segments import load_gip_road_segments

    roads = load_gip_road_segments(site)
    tracks = _collect_gip_tracks(site, g, roads)

    pad = g.get("abutment_s") or []
    s_pad0 = float(pad[0]) if len(pad) >= 2 else None
    s_pad1 = float(pad[1]) if len(pad) >= 2 else None

    rows: list[dict] = []
    for sm in samples:
        d = (float(sm["s_node"]) - s_p) * out_s
        x, y = float(sm["x"]), float(sm["y"])
        deck = float(sm["z_road"])
        z_dgm = _z_at(dgm, x, y, extent)
        z_cmp = _z_at(composed, x, y, extent)
        z_imp = _z_at(imported, x, y, extent) if imported is not None else None
        left, _rgt = bb.left_right_unit(float(sm.get("tx") or 1.0), float(sm.get("ty") or 0.0))
        if d < -0.5:
            region = "bore"
        elif s_pad0 is not None and s_pad1 is not None:
            slo, shi = (s_pad0, s_pad1) if s_pad0 <= s_pad1 else (s_pad1, s_pad0)
            if slo - 1e-6 <= sm["s_node"] <= shi + 1e-6:
                region = "pad"
            elif 0.0 <= d <= plate_len + 0.25:
                region = "plate"
            else:
                region = "beyond"
        else:
            region = "plate" if d >= -0.5 else "beyond"
        row: dict = {
            "d_portal": round(d, 3),
            "s": round(float(sm["s_node"]), 3),
            "x": round(x, 3),
            "y": round(y, 3),
            "deck": round(deck, 3),
            "dgm": round(z_dgm, 3),
            "composed": round(z_cmp, 3),
            "imported": None if z_imp is None else round(z_imp, 3),
            "composed_minus_deck": round(z_cmp - deck, 3),
            "region": region,
        }
        for tr in tracks:
            gx, gy, dist = _nearest_on_poly(x, y, tr["xy"])
            oid = tr["oid"]
            if dist > 30.0:
                row[f"gip_{oid}_lat"] = None
                row[f"gip_{oid}_dist"] = None
                row[f"gip_{oid}_x"] = None
                row[f"gip_{oid}_y"] = None
                continue
            lat = (gx - x) * left[0] + (gy - y) * left[1]
            row[f"gip_{oid}_lat"] = round(lat, 3)
            row[f"gip_{oid}_dist"] = round(dist, 3)
            row[f"gip_{oid}_x"] = round(gx, 3)
            row[f"gip_{oid}_y"] = round(gy, 3)
        rows.append(row)

    out_dir = proc / "portal_peek"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = str(g.get("name") or args.name)
    csv_path = out_dir / f"{stem}_deck_profile.csv"
    png_path = out_dir / f"{stem}_deck_profile.png"
    plan_path = out_dir / f"{stem}_deck_xy.png"
    fieldnames: list[str] = []
    for r in rows:
        for k in r.keys():
            if k not in fieldnames:
                fieldnames.append(k)
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        wcsv = csv.DictWriter(f, fieldnames=fieldnames)
        wcsv.writeheader()
        wcsv.writerows(rows)

    d_pad0 = None if s_pad0 is None else (s_pad0 - s_p) * out_s
    d_pad1 = None if s_pad1 is None else (s_pad1 - s_p) * out_s
    lat_series = [
        (f"gip_{tr['oid']}_lat", f"{tr['oid']} {tr['group']}", _gip_color(i))
        for i, tr in enumerate(tracks)
    ]
    _draw_profile(
        png_path,
        rows,
        title=f"{stem} deck profile  (Z vs metres from portal)",
        d_portal=0.0,
        d_pad0=d_pad0,
        d_pad1=d_pad1,
        lat_series=lat_series,
    )
    portal_sm = min(samples, key=lambda n: abs((float(n["s_node"]) - s_p) * out_s))
    pn = min(nodes, key=lambda n: abs(float(n["s"]) - s_p)) if nodes else portal_sm
    half_w = 0.5 * float(pn.get("width") or 7.5)
    p_left, _pr = bb.left_right_unit(float(pn.get("tx") or 1.0), float(pn.get("ty") or 0.0))
    _draw_plan(
        plan_path,
        assumed=[(float(n["x"]), float(n["y"])) for n in win],
        tracks=tracks,
        portal=(float(portal_sm["x"]), float(portal_sm["y"])),
        left=p_left,
        half_w=half_w,
        title=f"{stem} plan  assumed centerline vs GIP",
    )

    plate_rows = [r for r in rows if r["region"] in ("plate", "pad")]
    deltas = [float(r["composed_minus_deck"]) for r in plate_rows]
    dgm_d = [float(r["dgm"]) - float(r["deck"]) for r in plate_rows]
    print(f"{stem}: samples={len(rows)} plate={len(plate_rows)}")
    if plate_rows:
        print(
            f"  composed - deck: min={min(deltas):+.3f} m  max={max(deltas):+.3f} m  "
            f"mean={sum(deltas)/len(deltas):+.3f} m"
        )
        print(
            f"  DGM      - deck: min={min(dgm_d):+.3f} m  max={max(dgm_d):+.3f} m  "
            f"mean={sum(dgm_d)/len(dgm_d):+.3f} m"
        )
        n_high = sum(1 for d in deltas if d > 0.05)
        print(f"  composed > deck+5cm: {n_high}/{len(deltas)} samples")
    portal_row = min(rows, key=lambda r: abs(float(r["d_portal"])))
    print(
        f"  assumed CL at portal: xy=({portal_row['x']:.2f}, {portal_row['y']:.2f}) "
        f"d={float(portal_row['d_portal']):+.2f} m"
    )
    print("  GIP vs assumed CL (lat + = left of assumed, looking along travel):")
    for i, tr in enumerate(tracks):
        oid = tr["oid"]
        key = f"gip_{oid}_lat"
        at_p = portal_row.get(key)
        plate_vals = [
            float(r[key])
            for r in plate_rows
            if r.get(key) is not None
        ]
        mean_s = f" plate mean {sum(plate_vals)/len(plate_vals):+.3f} m" if plate_vals else ""
        if at_p is None:
            print(f"    {oid} {tr['group']}: (no GIP within 30 m at portal){mean_s}")
        else:
            print(
                f"    {oid} {tr['group']}: portal lat={float(at_p):+.3f} m  "
                f"xy=({portal_row.get(f'gip_{oid}_x')}, {portal_row.get(f'gip_{oid}_y')})"
                f"{mean_s}"
            )
    print(f"  CSV {csv_path}")
    print(f"  PNG {png_path}")
    print(f"  plan {plan_path}")
    if imported is None:
        print("  (no level import heightmap)")
    elif import_p.is_file():
        print(f"  import {import_p}")
    if args.inject_debug:
        inject_assumed_axes(
            site,
            sc,
            g,
            win,
            plate,
            composed=composed,
            extent=extent,
            tracks=tracks,
        )


def _emissive_mat(name: str, rgb: tuple[float, float, float], pid: str) -> dict:
    r, g, b = rgb
    return {
        "name": name,
        "mapTo": name,
        "class": "Material",
        "persistentId": pid,
        "Stages": [
            {
                "baseColorFactor": [r, g, b, 1.0],
                "roughnessFactor": 0.4,
                "emissiveFactor": [r * 0.8, g * 0.8, b * 0.8],
            },
            {},
            {},
            {},
        ],
        "doubleSided": True,
        "materialTag0": "RoadAndPath",
        "materialTag1": "beamng",
        "version": 1.5,
    }


def _ensure_debug_mats(user_level: Path, extra: list[tuple[str, tuple[float, float, float], str]] | None = None) -> None:
    # DAE mapTo lives next to gallery shapes, not under art/road.
    mats_path = user_level / "art" / "shapes" / "galleries" / "main.materials.json"
    mats_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if mats_path.is_file() and mats_path.stat().st_size:
        try:
            data = json.loads(mats_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    data["GalleryDebugDeck"] = _emissive_mat(
        "GalleryDebugDeck", (1.0, 0.1, 0.85), "deb06c11-0001-4e1e-9c11-0000deb06c11"
    )
    data["GalleryDebugApproach"] = _emissive_mat(
        "GalleryDebugApproach", (0.05, 0.95, 1.0), "deb06c11-0002-4e1e-9c11-0000deb06c11"
    )
    data["GalleryDebugWidth"] = _emissive_mat(
        "GalleryDebugWidth", (1.0, 0.85, 0.05), "deb06c11-0003-4e1e-9c11-0000deb06c11"
    )
    for name, rgb, pid in extra or []:
        data[name] = _emissive_mat(name, rgb, pid)
    mats_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _loft_debug_strip(
    nodes: list[dict],
    *,
    width_m: float,
    lift_m: float,
    z_of,
) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]], tuple[float, float, float]]:
    from build_galleries import _add_vert, _quad_along

    origin = (
        float(nodes[0]["x"]),
        float(nodes[0]["y"]),
        float(z_of(nodes[0])) + lift_m,
    )
    ox, oy, oz = origin
    verts: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    half = 0.5 * width_m
    thick = 0.04
    rings: list[tuple[int, int, int, int]] = []
    lefts: list[tuple[float, float]] = []
    for i, n in enumerate(nodes):
        tx = float(n.get("tx") or 0.0)
        ty = float(n.get("ty") or 0.0)
        if abs(tx) + abs(ty) < 1e-9 and i + 1 < len(nodes):
            tx = float(nodes[i + 1]["x"]) - float(n["x"])
            ty = float(nodes[i + 1]["y"]) - float(n["y"])
        elif abs(tx) + abs(ty) < 1e-9 and i > 0:
            tx = float(n["x"]) - float(nodes[i - 1]["x"])
            ty = float(n["y"]) - float(nodes[i - 1]["y"])
        left, _right = bb.left_right_unit(tx, ty)
        z = float(z_of(n)) + lift_m
        x, y = float(n["x"]), float(n["y"])
        lx, ly = x + half * left[0], y + half * left[1]
        rx, ry = x - half * left[0], y - half * left[1]
        i0 = _add_vert(verts, (lx - ox, ly - oy, z - oz))
        i1 = _add_vert(verts, (rx - ox, ry - oy, z - oz))
        i2 = _add_vert(verts, (rx - ox, ry - oy, z - oz - thick))
        i3 = _add_vert(verts, (lx - ox, ly - oy, z - oz - thick))
        rings.append((i0, i1, i2, i3))
        lefts.append(left)
    for a, b, left in zip(rings, rings[1:], lefts):
        _quad_along(faces, verts, a[0], a[1], b[1], b[0], (0.0, 0.0, 1.0))
        _quad_along(faces, verts, a[1], a[2], b[2], b[1], (-left[0], -left[1], 0.0))
        _quad_along(faces, verts, a[2], a[3], b[3], b[2], (0.0, 0.0, -1.0))
        _quad_along(faces, verts, a[3], a[0], b[0], b[3], (left[0], left[1], 0.0))
    return verts, faces, origin


def _register_debug_group(user_level: Path) -> Path:
    group_dir = user_level / "main" / "MissionGroup" / "level_objects" / "gallery_debug"
    group_dir.mkdir(parents=True, exist_ok=True)
    lo_items = user_level / "main" / "MissionGroup" / "level_objects" / "items.level.json"
    lines = []
    if lo_items.is_file() and lo_items.stat().st_size:
        lines = [ln for ln in lo_items.read_text(encoding="utf-8").splitlines() if ln.strip()]
    names = set()
    for ln in lines:
        try:
            names.add(json.loads(ln).get("name"))
        except json.JSONDecodeError:
            pass
    if "gallery_debug" not in names:
        lines.append(
            json.dumps(
                {
                    "name": "gallery_debug",
                    "class": "SimGroup",
                    "__parent": "level_objects",
                    "enabled": "1",
                },
                separators=(",", ":"),
            )
        )
        lo_items.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return group_dir / "items.level.json"


def inject_assumed_axes(
    site: dict,
    sc,
    g: dict,
    win: list[dict],
    plate: list[dict],
    *,
    composed,
    extent: float,
    tracks: list[dict] | None = None,
) -> None:
    """Magenta = assumed deck axis; cyan = unify mean; coloured = GIP OBJECTIDs; yellow = 7.5 m."""
    from build_galleries import write_collada
    from gip_road_segments import load_unify_axis_xy

    _ = sc
    bng = site.get("beamng") or {}
    level_name = str(bng.get("level_name") or "")
    user_level = bb.USER_LEVELS / level_name
    if not user_level.is_dir():
        print("No user level folder — skipped debug inject")
        return
    extra_mats: list[tuple[str, tuple[float, float, float], str]] = []
    for i, tr in enumerate(tracks or []):
        name = f"GalleryDebugGip{tr['oid']}"
        pid = f"deb06c11-{1000 + int(tr['oid']) % 8000:04d}-4e1e-9c11-0000deb06c11"
        extra_mats.append((name, _gip_rgb01(i), pid))
    _ensure_debug_mats(user_level, extra_mats)
    shapes = user_level / "art" / "shapes" / "galleries"
    shapes.mkdir(parents=True, exist_ok=True)
    items_path = _register_debug_group(user_level)

    def z_deck(n: dict) -> float:
        return float(n["z_road"])

    def z_hm(n: dict) -> float:
        return _z_at(composed, float(n["x"]), float(n["y"]), extent)

    entries: list[dict] = []
    src = win if len(win) >= 2 else plate

    def _add_strip(name: str, nodes: list[dict], mat: str, width: float, z_of, lift: float) -> None:
        if len(nodes) < 2:
            return
        verts, faces, origin = _loft_debug_strip(nodes, width_m=width, lift_m=lift, z_of=z_of)
        dae = f"{name}.dae"
        write_collada(
            shapes / dae,
            verts,
            faces,
            material_name=mat,
            mesh_stem=name,
        )
        entries.append(
            {
                "name": name,
                "class": "TSStatic",
                "__parent": "gallery_debug",
                "position": [round(origin[0], 3), round(origin[1], 3), round(origin[2], 3)],
                "scale": [1.0, 1.0, 1.0],
                "shapeName": f"/levels/{level_name}/art/shapes/galleries/{dae}",
                "collisionType": "None",
                "decalType": "None",
                "useInstanceRenderData": True,
                "isRenderEnabled": True,
            }
        )

    _add_strip("debug_axis_deck", src, "GalleryDebugDeck", 0.35, z_deck, 0.12)

    portal_s = g.get("portal_s") or [0.0, 0.0]
    s_p = float(portal_s[1] if g.get("fake_end") == "s0" else portal_s[0])
    portal = min(src, key=lambda n: abs(float(n["s"]) - s_p))
    tx, ty = float(portal.get("tx") or 1.0), float(portal.get("ty") or 0.0)
    left, _r = bb.left_right_unit(tx, ty)
    half = 0.5 * float(portal.get("width") or 7.5)
    z_p = float(portal["z_road"])
    bar = [
        {
            "x": float(portal["x"]) + half * left[0],
            "y": float(portal["y"]) + half * left[1],
            "z_road": z_p,
            "tx": -left[1],
            "ty": left[0],
            "s": 0.0,
        },
        {
            "x": float(portal["x"]) - half * left[0],
            "y": float(portal["y"]) - half * left[1],
            "z_road": z_p,
            "tx": -left[1],
            "ty": left[0],
            "s": 7.5,
        },
    ]
    _add_strip("debug_axis_width75", bar, "GalleryDebugWidth", 0.2, z_deck, 0.18)

    def _offset_nodes(nodes: list[dict], lat_m: float) -> list[dict]:
        out: list[dict] = []
        for i, n in enumerate(nodes):
            tx = float(n.get("tx") or 0.0)
            ty = float(n.get("ty") or 0.0)
            if abs(tx) + abs(ty) < 1e-9 and i + 1 < len(nodes):
                tx = float(nodes[i + 1]["x"]) - float(n["x"])
                ty = float(nodes[i + 1]["y"]) - float(n["y"])
            elif abs(tx) + abs(ty) < 1e-9 and i > 0:
                tx = float(n["x"]) - float(nodes[i - 1]["x"])
                ty = float(n["y"]) - float(nodes[i - 1]["y"])
            left_u, _r = bb.left_right_unit(tx, ty)
            nn = dict(n)
            nn["x"] = float(n["x"]) + lat_m * left_u[0]
            nn["y"] = float(n["y"]) + lat_m * left_u[1]
            nn["tx"] = tx
            nn["ty"] = ty
            out.append(nn)
        return out

    _add_strip("debug_axis_left75", _offset_nodes(src, half), "GalleryDebugWidth", 0.12, z_deck, 0.16)
    _add_strip("debug_axis_right75", _offset_nodes(src, -half), "GalleryDebugWidth", 0.12, z_deck, 0.16)
    print(
        f"  portal xy=({float(portal['x']):.2f}, {float(portal['y']):.2f}) "
        f"z_road={z_p:.3f} assumed_width={2.0 * half:.1f} m"
    )

    uid = str(g.get("append_unify") or "landecker_approach")
    try:
        xy, _spec = load_unify_axis_xy(site, uid)
    except SystemExit as exc:
        print(f"debug approach skipped: {exc}")
        xy = []
    if len(xy) >= 2:
        ox, oy = float(portal["x"]), float(portal["y"])
        d0 = math.hypot(xy[0][0] - ox, xy[0][1] - oy)
        d1 = math.hypot(xy[-1][0] - ox, xy[-1][1] - oy)
        if d1 < d0:
            xy = list(reversed(xy))
        app_nodes = []
        acc = 0.0
        prev = None
        for x, y in xy:
            if prev is not None:
                acc += math.hypot(x - prev[0], y - prev[1])
            if acc > 55.0:
                break
            app_nodes.append(
                {
                    "x": x,
                    "y": y,
                    "z_road": _z_at(composed, x, y, extent),
                    "tx": 1.0,
                    "ty": 0.0,
                    "s": acc,
                }
            )
            prev = (x, y)
        if len(app_nodes) >= 2:
            _add_strip("debug_axis_approach", app_nodes, "GalleryDebugApproach", 0.35, z_hm, 0.22)

    ox, oy = float(portal["x"]), float(portal["y"])
    for i, tr in enumerate(tracks or []):
        pieces = _clip_xy_near(tr["xy"], ox, oy, 55.0)
        mat = f"GalleryDebugGip{tr['oid']}"
        for pi, piece in enumerate(pieces):
            gnodes = []
            acc = 0.0
            prev = None
            for x, y in piece:
                if prev is not None:
                    acc += math.hypot(x - prev[0], y - prev[1])
                gnodes.append(
                    {
                        "x": x,
                        "y": y,
                        "z_road": _z_at(composed, x, y, extent),
                        "tx": 1.0,
                        "ty": 0.0,
                        "s": acc,
                    }
                )
                prev = (x, y)
            _add_strip(f"debug_gip_{tr['oid']}_{pi}", gnodes, mat, 0.28, z_hm, 0.28)

    with items_path.open("w", encoding="utf-8", newline="\n") as f:
        for e in entries:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")
    print(
        f"Debug axes: magenta=assumed CL, yellow=7.5 m, cyan=append_unify {uid}, "
        f"other=GIP OBJECTID -> {items_path} ({len(entries)} meshes, no collision)"
    )


if __name__ == "__main__":
    main()
