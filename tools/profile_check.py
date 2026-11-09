"""Longitudinal profile of what a car drives on through galleries and tunnels and over their approaches.

Samples the surfaces as they end up in the level along wheel tracks, every 0.1 m:

- the deck MeshRoads of build_galleries (``galleries_items.level.json``): the top surface rebuilt from the nodes,
  edges at +- width/2 along the banked cross direction. BeamNG splines between the nodes; this takes them straight,
  which is what the nodes (1-2 m apart) say;
- Magliasina's road mesh (roads_only), cut at the handover lines as import_magliasina_roads cuts it;
- the terrain (composed heightmap, the terrain's own triangles), except where the hole map removes it.

Per track it finds the driven surface (the highest road surface near the last height, the terrain where there is
none) and reports:

- steps: height jumps from one 0.1 m sample to the next, less the local grade;
- grade breaks over 2, 5 and 10 m: grade over the next window minus grade over the last;
- overlaps: deck and Magliasina both there at heights more than 1 cm apart (two surfaces cutting through);
- terrain above the road: terrain more than 2 cm over the driven surface (and less than 1.5 m, so not a roof);
- gaps: no surface at all.

The tracks follow the deck axis and, past each deck end, the middle of the Magliasina road (``--outside`` m).
Default offsets from the axis: the wheel tracks of two 3.5 m lanes (+-0.9 and +-2.6 m) and the axis itself.

Limits as the transition check of build_galleries (``galleries.transition_*``): a step over 2 cm is a warning, over
``transition_step_max_m`` a failure; a grade break over ``transition_kink_warn`` a warning, over
``transition_kink_max`` a failure.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/pfunds_spiess.yaml"; python tools\\profile_check.py --only 3125,3127,3128

Writes profile_check.json and one profile_check_<deck>.png per deck into the processed folder. Run it after
build_galleries, import_magliasina_roads and compose_heightmap (it reads their output).
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from site_coords import SiteCoords, load_site, processed_dir  # noqa: E402
import heightmap_layers as hml  # noqa: E402
import magliasina_surface as mgs  # noqa: E402
import terrain_holes as th  # noqa: E402

STEP = 0.1
WINDOWS = (2.0, 5.0, 10.0)
LIMITS = {"step_warn": 0.02, "step_max": 0.10, "kink_warn": 0.03, "kink_max": 0.08}
NEAR_M = 1.5  # surfaces farther than this from the last height are another level (a roof, a road below)


# ---------------------------------------------------------------------------------------------------------------
# surfaces


def deck_triangles(nodes: np.ndarray) -> np.ndarray:
    """Top surface of a MeshRoad from its nodes [x, y, z, width, depth, nx, ny, nz] as triangles (n, 3, 3)."""
    c = nodes[:, :3]
    w = nodes[:, 3]
    up = nodes[:, 5:8] / np.linalg.norm(nodes[:, 5:8], axis=1, keepdims=True)
    fwd = np.gradient(c, axis=0)
    fwd /= np.maximum(np.linalg.norm(fwd, axis=1, keepdims=True), 1e-9)
    right = np.cross(fwd, up)  # build_bridges.normal_from_pitch_crossfall: up = fwd x right
    right /= np.maximum(np.linalg.norm(right, axis=1, keepdims=True), 1e-9)
    left_e = c - right * (0.5 * w)[:, None]
    right_e = c + right * (0.5 * w)[:, None]
    tris = []
    for i in range(len(c) - 1):
        a, b, cc, d = left_e[i], right_e[i], right_e[i + 1], left_e[i + 1]
        tris.append([a, b, cc])
        tris.append([a, cc, d])
    return np.array(tris, dtype=np.float64)


class Terrain:
    """Heights of the terrain as BeamNG triangulates it (terrain_holes.square_triangles), holes left out."""

    def __init__(self, elev: np.ndarray, hole: np.ndarray | None, pitch: float):
        self.elev = elev
        self.hole = hole
        self.size = elev.shape[0]
        self.pitch = pitch

    def z(self, x: float, y: float) -> float | None:
        fx, fy = x / self.pitch, y / self.pitch
        i, j = int(math.floor(fx)), int(math.floor(fy))
        if not (0 <= i < self.size - 1 and 0 <= j < self.size - 1):
            return None
        if self.hole is not None and self.hole[self.size - 1 - j, i] > 0:
            return None
        u, v = fx - i, fy - j

        def h(di, dj):
            return float(self.elev[self.size - 1 - (j + dj), i + di])

        for tri in th.square_triangles(i, j):
            (a0, b0), (a1, b1), (a2, b2) = tri
            det = (a1 - a0) * (b2 - b0) - (a2 - a0) * (b1 - b0)
            l1 = ((u - a0) * (b2 - b0) - (a2 - a0) * (v - b0)) / det
            l2 = ((a1 - a0) * (v - b0) - (u - a0) * (b1 - b0)) / det
            if l1 >= -1e-9 and l2 >= -1e-9 and l1 + l2 <= 1 + 1e-9:
                z0 = h(a0, b0)
                return z0 + l1 * (h(a1, b1) - z0) + l2 * (h(a2, b2) - z0)
        return None


def surface_of(tris: np.ndarray) -> mgs.Surface | None:
    return mgs.Surface(tris) if len(tris) else None


# ---------------------------------------------------------------------------------------------------------------
# path


def deck_axis(nodes: np.ndarray) -> np.ndarray:
    """(x, y) of the deck axis, resampled every STEP."""
    xy = nodes[:, :2]
    d = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    s = np.arange(0.0, d[-1] + 1e-9, STEP)
    return np.column_stack([np.interp(s, d, xy[:, 0]), np.interp(s, d, xy[:, 1])])


def follow_road(mgl: mgs.Surface | None, start: np.ndarray, t: np.ndarray, z: float, length: float) -> np.ndarray:
    """(x, y) along the middle of the Magliasina road from ``start`` in direction ``t``, every STEP; stops where the
    asphalt ends."""
    pts = []
    if mgl is None:
        return np.zeros((0, 2))
    p = start.astype(float).copy()
    t = t / np.linalg.norm(t)
    mids = []
    n = int(round(length / STEP))
    for _ in range(n):
        q = p + t * STEP
        sec = mgl.section(float(q[0]), float(q[1]), float(t[0]), float(t[1]), ref=z, step=0.1)
        if sec is None or sec["width"] < 3.0:
            break
        left = np.array([-t[1], t[0]])
        m = q + left * float(sec["mid_offset"])
        z = float(sec["z_mid"])
        mids.append(m)
        # head along the road: direction over the last 3 m of middles, and keep the sample on the axis of
        # travel (the middle is only used to steer, so a ragged edge does not shake the track)
        if len(mids) >= 30:
            dv = mids[-1] - mids[-30]
            if np.linalg.norm(dv) > 1e-6:
                t = 0.8 * t + 0.2 * dv / np.linalg.norm(dv)
                t /= np.linalg.norm(t)
        p = q + 0.05 * (m - q)  # drift toward the middle slowly
        pts.append(p.copy())
    return np.array(pts).reshape(-1, 2)


def tracks(axis: np.ndarray, offsets: list[float]) -> dict[float, np.ndarray]:
    t = np.gradient(axis, axis=0)
    t /= np.maximum(np.linalg.norm(t, axis=1, keepdims=True), 1e-9)
    left = np.column_stack([-t[:, 1], t[:, 0]])
    return {o: axis + left * o for o in offsets}


# ---------------------------------------------------------------------------------------------------------------
# sampling and checks


def sample_track(xy: np.ndarray, deck, mgl, terr: Terrain, z_start: float) -> dict:
    n = len(xy)
    z = np.full(n, np.nan)
    src = np.zeros(n, dtype=np.int8)  # 0 none, 1 deck, 2 magliasina, 3 terrain
    overlap = np.zeros(n)
    terr_above = np.zeros(n)
    ref = z_start
    for i, (x, y) in enumerate(xy):
        zd = deck.z(x, y, ref) if deck is not None else None
        zm = mgl.z(x, y, ref) if mgl is not None else None
        zt = terr.z(x, y)
        zd = zd if zd is not None and abs(zd - ref) <= NEAR_M else None
        zm = zm if zm is not None and abs(zm - ref) <= NEAR_M else None
        road = [v for v in (zd, zm) if v is not None]
        if road:
            zz = max(road)
            src[i] = 1 if zd is not None and zz == zd else 2
            if zd is not None and zm is not None:
                overlap[i] = abs(zd - zm)
            if zt is not None and zz < zt < zz + NEAR_M:
                terr_above[i] = zt - zz
        elif zt is not None and abs(zt - ref) <= NEAR_M:
            zz = zt
            src[i] = 3
        else:
            continue
        z[i] = zz
        ref = zz
    return {"z": z, "src": src, "overlap": overlap, "terr_above": terr_above}


def steps(z: np.ndarray) -> np.ndarray:
    """Height jump per sample less the local grade (median of the jumps over +-1 m)."""
    dz = np.diff(z)
    k = int(round(1.0 / STEP))
    out = np.zeros_like(dz)
    for i in range(len(dz)):
        win = dz[max(0, i - k): i + k + 1]
        win = win[np.isfinite(win)]
        if len(win) and np.isfinite(dz[i]):
            out[i] = dz[i] - np.median(win)
    return out


def grade_breaks(z: np.ndarray, w: float) -> np.ndarray:
    k = int(round(w / STEP))
    out = np.full(len(z), np.nan)
    for i in range(k, len(z) - k):
        a, b, c = z[i - k], z[i], z[i + k]
        if np.isfinite(a) and np.isfinite(b) and np.isfinite(c):
            out[i] = (c - b) / w - (b - a) / w
    return out


def judge(value: float, warn: float, fail: float) -> str:
    return "fail" if abs(value) > fail else ("warn" if abs(value) > warn else "ok")


# ---------------------------------------------------------------------------------------------------------------
# plot


def unevenness(z: np.ndarray, window_m: float = 10.0) -> np.ndarray:
    """Height minus its running mean over ``window_m``: the bumps, without the road's own rise and fall."""
    k = int(round(window_m / STEP / 2))
    out = np.full(len(z), np.nan)
    for i in range(len(z)):
        w = z[max(0, i - k): i + k + 1]
        w = w[np.isfinite(w)]
        if np.isfinite(z[i]) and i >= k and i + k < len(z) and len(w) == 2 * k + 1:  # full window only
            out[i] = z[i] - w.mean()
    return out


def zones(u: np.ndarray, marks: list[tuple[float, str]], half_m: float = 30.0) -> list[tuple[float, float]]:
    """Stretches of +-``half_m`` around the marks, merged where they overlap; the first and last reach the ends."""
    iv = sorted((max(float(u[0]), m - half_m), min(float(u[-1]), m + half_m)) for m, _ in marks)
    out: list[list[float]] = []
    for a, b in iv:
        if out and a <= out[-1][1] + 5.0:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    if out:
        out[0][0] = float(u[0])
        out[-1][1] = float(u[-1])
    return [(a, b) for a, b in out]


COLORS = [(200, 30, 30), (230, 130, 0), (0, 0, 0), (0, 120, 200), (120, 0, 160)]
SRC_COL = {1: (225, 235, 250), 2: (232, 245, 222), 3: (245, 232, 215)}


def plot(path: Path, title: str, u: np.ndarray, res: dict, marks: list[tuple[float, str]], lim: dict) -> None:
    zs = zones(u, marks)
    col_w, H = 520, 860
    W = 70 + col_w * len(zs) + 20
    img = Image.new("RGB", (W, H), "white")
    dr = ImageDraw.Draw(img)
    rows = [(50, 300, "u", "unevenness: height minus 10 m running mean [cm]"),
            (340, 570, "k", "grade break over 5 m [%]"),
            (610, 820, "s", "step per 0.1 m [cm]")]
    series = {}
    for o, r in res.items():
        series[o] = {"u": unevenness(r["z"]) * 100, "k": r["kink5"] * 100, "s": np.r_[r["steps"], 0.0] * 100}
    axis_track = res[min(res, key=abs)]
    dr.text((10, 8), title, fill="black")
    legend = "   ".join(f"{o:+.1f} m" for o in res)
    dr.text((10, 24), f"tracks (left +): {legend}  -  background: deck blue, Magliasina green, terrain brown;"
                      f" orange/red lines: warn/fail limits", fill=(60, 60, 60))
    for k, (o, _) in enumerate(res.items()):
        dr.line([(10 + 60 * k + 600, 40), (10 + 60 * k + 620, 40)], fill=COLORS[k % 5], width=3)
    for top, bot, key, label in rows:
        dr.text((10, top - 14), label, fill=(60, 60, 60))
        vals = np.concatenate([v[key][np.isfinite(v[key])] for v in series.values()] or [np.zeros(1)])
        m = float(np.abs(vals).max()) if len(vals) else 1.0
        if key == "k":
            m = max(m, lim["kink_max"] * 100 * 1.15)
        elif key == "s":
            m = max(m, lim["step_warn"] * 100 * 1.5)
        else:
            m = max(m, 1.0)
        lo, hi = -m, m
        dr.text((10, top), f"{hi:+.1f}", fill="black")
        dr.text((10, bot - 12), f"{lo:+.1f}", fill="black")
        for zi, (a, b) in enumerate(zs):
            L = 70 + zi * col_w
            R = L + col_w - 15

            def X(v, L=L, R=R, a=a, b=b):
                return L + (v - a) / max(b - a, 1e-9) * (R - L)

            def Y(v, top=top, bot=bot):
                return bot - (v - lo) / (hi - lo) * (bot - top)

            sel = np.where((u >= a) & (u <= b))[0]
            for i in sel:
                c = SRC_COL.get(int(axis_track["src"][i]))
                if c:
                    dr.line([(X(u[i]), top), (X(u[i]), bot)], fill=c, width=2)
            dr.rectangle([L, top, R, bot], outline=(150, 150, 150))
            dr.line([(L, Y(0)), (R, Y(0))], fill=(170, 170, 170))
            if key in ("k", "s"):
                for lv, cc in ((lim["kink_warn"] if key == "k" else lim["step_warn"], (230, 160, 0)),
                               (lim["kink_max"] if key == "k" else lim["step_max"], (220, 0, 0))):
                    for sg in (1, -1):
                        v = sg * lv * 100
                        if lo <= v <= hi:
                            dr.line([(L, Y(v)), (R, Y(v))], fill=cc)
            for k, (o, sv) in enumerate(series.items()):
                y = sv[key]
                pts = []
                for i in sel:
                    if np.isfinite(y[i]):
                        pts.append((X(u[i]), Y(float(np.clip(y[i], lo, hi)))))
                        continue
                    if len(pts) > 1:
                        dr.line(pts, fill=COLORS[k % 5], width=1)
                    pts = []
                if len(pts) > 1:
                    dr.line(pts, fill=COLORS[k % 5], width=1)
            for v, lab in marks:
                if a <= v <= b:
                    dr.line([(X(v), top), (X(v), bot)], fill=(90, 90, 90))
                    if key == "s":
                        dr.text((X(v) + 2, bot + 4), lab, fill="black")
            if key == "s":
                step_t = 10 if b - a <= 120 else 50
                for v in np.arange(math.ceil(a / step_t) * step_t, b + 1e-9, step_t):
                    dr.text((X(v) - 8, bot + 22), f"{v:.0f}", fill=(90, 90, 90))
    img.save(path)


# ---------------------------------------------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="OBJECTIDs (comma separated): decks that carry one of them")
    ap.add_argument("--outside", type=float, default=50.0, help="metres along the road past each deck end")
    ap.add_argument("--offsets", default="-2.6,-0.9,0,0.9,2.6", help="track offsets from the axis, left +")
    ap.add_argument("--no-plot", action="store_true")
    args = ap.parse_args()

    site = load_site()
    sc = SiteCoords(site)
    proc = processed_dir(site)
    size = int((site.get("beamng") or {}).get("mask_size") or 512)
    gal = site.get("galleries") or {}
    lim = dict(LIMITS)
    lim["step_max"] = float(gal.get("transition_step_max_m", lim["step_max"]))
    lim["kink_warn"] = float(gal.get("transition_kink_warn", lim["kink_warn"]))
    lim["kink_max"] = float(gal.get("transition_kink_max", lim["kink_max"]))
    offsets = [float(v) for v in args.offsets.split(",") if v.strip()]
    only = {int(v) for v in args.only.split(",")} if args.only else None

    items_path = proc / "galleries_items.level.json"
    if not items_path.is_file():
        raise SystemExit(f"no {items_path} - run build_galleries first")
    decks = []
    for line in items_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        o = json.loads(line)
        if o.get("class") != "MeshRoad" or not str(o.get("name", "")).startswith("gallery_deck_"):
            continue
        oids = {int(v) for v in re.findall(r"_(\d+)(?=_|$)", o["name"])}
        if only and not (oids & only):
            continue
        decks.append((o["name"], oids, np.array(o["nodes"], dtype=np.float64)))
    if not decks:
        raise SystemExit("no deck MeshRoad matches --only")

    elev, _max_h, elev_src = hml.load_composed_or_dgm(proc, size)
    hole = None
    hp = proc / "theTerrain_holemap.png"
    if hp.is_file():
        hole = np.asarray(Image.open(hp))
        hole = hole[..., 0] if hole.ndim == 3 else hole
        if hole.shape != elev.shape:
            hole = None
    terr = Terrain(elev, hole, float(sc.square_m))
    lines = []
    hp_lines = proc / "magliasina_handover.json"
    if hp_lines.is_file():
        lines = json.loads(hp_lines.read_text(encoding="utf-8")).get("lines") or []
    print(f"terrain: {elev_src} heightmap, hole map {'yes' if hole is not None else 'no'}; "
          f"{len(lines)} handover line(s)")

    portals: dict[int, list[tuple[float, float, str]]] = {}
    cl_path = proc / "galleries_centerlines.json"
    if cl_path.is_file():
        for g in json.loads(cl_path.read_text(encoding="utf-8")).get("galleries") or []:
            ns = sorted(g.get("nodes") or [], key=lambda n: float(n["s"]))
            if len(ns) < 2 or not g.get("portal_s"):
                continue
            ss = np.array([float(n["s"]) for n in ns])
            short = str(g.get("name") or g.get("objectid"))[:14]
            portals[int(g["objectid"])] = [
                (float(np.interp(sp, ss, [float(n["x"]) for n in ns])),
                 float(np.interp(sp, ss, [float(n["y"]) for n in ns])), short)
                for sp in g["portal_s"]
            ]
    names = {0: "none", 1: "deck", 2: "magliasina", 3: "terrain"}

    report = {"limits": lim, "offsets": offsets, "decks": []}
    for name, oids, nodes in decks:
        pad = args.outside + 20
        box = (nodes[:, 0].min() - pad, nodes[:, 1].min() - pad, nodes[:, 0].max() + pad, nodes[:, 1].max() + pad)
        mgl = mgs.load_surface(site, sc, proc, box)
        if mgl is not None and lines:
            pos = mgl.P.reshape(-1, 3)
            faces = np.arange(len(pos)).reshape(-1, 3)
            pos, faces, _n = mgs.cut_at_lines(pos, faces, lines)
            mgl = surface_of(pos[faces])
        deck = surface_of(deck_triangles(nodes))
        ax_in = deck_axis(nodes)
        t0 = ax_in[0] - ax_in[min(10, len(ax_in) - 1)]
        t1 = ax_in[-1] - ax_in[max(-11, -len(ax_in))]
        before = follow_road(mgl, ax_in[0], t0, float(nodes[0, 2]), args.outside)[::-1]
        after = follow_road(mgl, ax_in[-1], t1, float(nodes[-1, 2]), args.outside)
        axis = np.vstack([before, ax_in, after])
        u = (np.arange(len(axis)) - len(before)) * STEP  # 0 at the deck's first node
        z_start = float(nodes[0, 2])
        res = {}
        for o, xy in tracks(axis, offsets).items():
            # start at the deck's first node and sample both ways, so the reference height starts right
            k0 = len(before)
            fw = sample_track(xy[k0:], deck, mgl, terr, z_start)
            bw = sample_track(xy[:k0][::-1], deck, mgl, terr, z_start) if k0 else None
            r = {}
            for key in fw:
                r[key] = np.r_[bw[key][::-1], fw[key]] if bw is not None else fw[key]
            r["steps"] = steps(r["z"])
            for w in WINDOWS:
                r[f"kink{int(w)}"] = grade_breaks(r["z"], w)
            res[o] = r

        out = {"deck": name, "objectids": sorted(oids), "length_m": round(float(u[-1] - u[0]), 1),
               "outside_m": [round(float(len(before) * STEP), 1), round(float(len(after) * STEP), 1)], "tracks": []}
        worst = "ok"
        rank = {"ok": 0, "warn": 1, "fail": 2}
        for o, r in res.items():
            tr = {"offset_m": o}
            st = r["steps"]
            i = int(np.nanargmax(np.abs(st))) if np.isfinite(st).any() else 0
            tr["step"] = {"max_m": round(float(st[i]), 3), "at_m": round(float(u[i]), 1),
                          "on": f"{names[int(r['src'][i])]}/{names[int(r['src'][min(i + 1, len(u) - 1)])]}",
                          "n_over_warn": int((np.abs(st) > lim["step_warn"]).sum()),
                          "judge": judge(float(st[i]), lim["step_warn"], lim["step_max"])}
            for w in WINDOWS:
                kk = r[f"kink{int(w)}"]
                if np.isfinite(kk).any():
                    j = int(np.nanargmax(np.abs(kk)))
                    tr[f"kink_{int(w)}m"] = {"max": round(float(kk[j]), 4), "at_m": round(float(u[j]), 1),
                                             "on": names[int(r["src"][j])],
                                             "judge": judge(float(kk[j]), lim["kink_warn"], lim["kink_max"])}
            ov = r["overlap"]
            tr["overlap"] = {"max_m": round(float(ov.max()), 3), "m": round(float((ov > 0.01).sum() * STEP), 1),
                             "at_m": round(float(u[int(np.argmax(ov))]), 1)}
            ta = r["terr_above"]
            tr["terrain_above"] = {"max_m": round(float(ta.max()), 3), "m": round(float((ta > 0.02).sum() * STEP), 1),
                                   "at_m": round(float(u[int(np.argmax(ta))]), 1)}
            tr["gap_m"] = round(float((r["src"] == 0).sum() * STEP), 1)
            tr["surface_m"] = {k: round(float((r["src"] == v).sum() * STEP), 1)
                               for k, v in (("deck", 1), ("magliasina", 2), ("terrain", 3))}
            levels = [tr["step"]["judge"]] + [tr[k]["judge"] for k in tr if k.startswith("kink_")]
            if tr["overlap"]["m"] > 0 or tr["terrain_above"]["m"] > 0:
                levels.append("warn")
            tr["judge"] = max(levels, key=rank.get)
            worst = max(worst, tr["judge"], key=rank.get)
            out["tracks"].append(tr)
        out["judge"] = worst
        report["decks"].append(out)

        marks = [(0.0, "deck start"), (float(u[len(before) + len(ax_in) - 1]), "deck end")]
        seen: list[float] = []
        for oid in sorted(oids):
            for px, py, short in portals.get(oid, []):
                k = int(np.argmin(np.hypot(axis[:, 0] - px, axis[:, 1] - py)))
                if np.hypot(axis[k, 0] - px, axis[k, 1] - py) > 10.0:
                    continue
                uk = float(u[k])
                if any(abs(uk - v) < 2.0 for v in seen):
                    marks = [(v, "joint") if abs(v - uk) < 2.0 and lab != "deck start" and lab != "deck end"
                             else (v, lab) for v, lab in marks]
                    continue
                seen.append(uk)
                marks.append((uk, short))
        marks.sort()
        out["marks"] = [{"u_m": round(v, 1), "label": lab} for v, lab in marks]
        print(f"\n{name}: {out['length_m']:.0f} m (Magliasina {out['outside_m'][0]:.0f} m before, "
              f"{out['outside_m'][1]:.0f} m after the deck) - {worst}")
        print("  marks (u, m from the deck start): " + ", ".join(f"{m['label']} {m['u_m']:.1f}" for m in out["marks"]))
        print("  track  step max (on)                    break 2 m             break 5 m             break 10 m"
              "            overlap  terrain>road  gap")
        for tr in out["tracks"]:
            def kb(k):
                v = tr.get(k)
                return f"{100 * v['max']:+5.1f}% @{v['at_m']:6.1f} {v['on'][:4]:4s}" if v else " " * 20
            print(f"  {tr['offset_m']:+5.1f}  {100 * tr['step']['max_m']:+5.1f}cm @{tr['step']['at_m']:6.1f}"
                  f" {tr['step']['on']:18s}"
                  f"  {kb('kink_2m')}  {kb('kink_5m')}  {kb('kink_10m')}"
                  f"  {tr['overlap']['m']:5.1f} m  {tr['terrain_above']['m']:5.1f} m"
                  f"{'' if not tr['terrain_above']['m'] else ' @%.0f' % tr['terrain_above']['at_m']:>6s}  {tr['gap_m']:4.1f} m"
                  f"  {tr['judge']}")
        if not args.no_plot:
            png = proc / f"profile_check_{name}.png"
            plot(png, f"{name}: u = m from the deck start (-u: Magliasina before it)", u, res, marks, lim)
            out["plot"] = png.name
            print(f"  plot {png}")

    path = proc / "profile_check.json"
    path.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"\nwrote {path}")
    print(f"limits: step warn {100 * lim['step_warn']:.0f} cm / fail {100 * lim['step_max']:.0f} cm; grade break "
          f"warn {100 * lim['kink_warn']:.0f} % / fail {100 * lim['kink_max']:.0f} %")


if __name__ == "__main__":
    main()
