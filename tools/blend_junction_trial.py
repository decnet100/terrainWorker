"""Trial blend at one at-grade junction.

The through pair keeps its surface. The departing arm ramps onto that edge.
Two pairs both near 180° and the same class meet in the middle.
Profiles are written next to the site data. ``--apply`` writes the same
pixels into ``04_smooth.tif`` and ``04_layer0.tif``. The first apply keeps
``04_smooth.pre_junction.tif`` and ``04_layer0.pre_junction.tif``.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\blend_junction_trial.py
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import tifffile as tiff

sys.path.insert(0, str(Path(__file__).resolve().parent))
from site_coords import load_site, processed_dir

OIDS = (36280, 36279, 96185)
# Along-arm length of the patch, on the scale of the half-widths.
R_ALONG_M = 16.0
# S-G 15 %, S-L 5°, S-B / S-A 3°. The ramp limit is the longitudinal grade.
GRADE_LIMIT = {"S-G": 0.15, "S-L": math.tan(math.radians(5.0)), "S-B": math.tan(math.radians(3.0)), "S-A": math.tan(math.radians(3.0))}
FIT_FROM_M = 14.0
FIT_TO_M = 40.0


def _tfw(path: Path):
    vals = [float(x) for x in path.with_suffix(".tfw").read_text().split()]
    return tuple(vals[:6])


def _sample(z, tf, x, y) -> float:
    a, _d, _b, e, c, f = tf
    col = (x - c) / a
    row = (y - f) / e
    h, w = z.shape[:2]
    ic, ir = int(round(col)), int(round(row))
    if ir < 0 or ic < 0 or ir >= h or ic >= w:
        return float("nan")
    v = float(z[ir, ic])
    return v if np.isfinite(v) else float("nan")


def _xy(tf, col: int, row: int) -> tuple[float, float]:
    a, _d, _b, e, c, f = tf
    return c + col * a, f + row * e


def _smoothstep(u: float) -> float:
    u = min(1.0, max(0.0, u))
    return u * u * (3.0 - 2.0 * u)


def _orient(line, origin):
    coords = [(float(x), float(y)) for x, y, *_ in line.coords]
    d0 = (coords[0][0] - origin[0]) ** 2 + (coords[0][1] - origin[1]) ** 2
    d1 = (coords[-1][0] - origin[0]) ** 2 + (coords[-1][1] - origin[1]) ** 2
    if d1 < d0:
        coords.reverse()
    return coords


def _at(coords, dist: float) -> tuple[float, float, float, float]:
    seg = 0.0
    for i in range(1, len(coords)):
        dx = coords[i][0] - coords[i - 1][0]
        dy = coords[i][1] - coords[i - 1][1]
        step = math.hypot(dx, dy)
        if seg + step >= dist or i == len(coords) - 1:
            t = 0.0 if step == 0 else min(1.0, (dist - seg) / step)
            x = coords[i - 1][0] + t * dx
            y = coords[i - 1][1] + t * dy
            n = step or 1.0
            return x, y, dx / n, dy / n
        seg += step
    x, y = coords[-1]
    return x, y, 1.0, 0.0


def _project(coords, x: float, y: float) -> tuple[float, float, float, float]:
    """Station from the node, left-positive offset, foot."""
    best = None
    seg = 0.0
    for i in range(1, len(coords)):
        x0, y0 = coords[i - 1]
        x1, y1 = coords[i]
        dx, dy = x1 - x0, y1 - y0
        step = math.hypot(dx, dy) or 1.0
        t = ((x - x0) * dx + (y - y0) * dy) / (step * step)
        t = min(1.0, max(0.0, t))
        px, py = x0 + t * dx, y0 + t * dy
        ox, oy = x - px, y - py
        d2 = ox * ox + oy * oy
        if best is None or d2 < best[0]:
            # left of the travel direction is positive
            cross = dx * oy - dy * ox
            best = (d2, seg + t * step, cross / step, px, py)
        seg += step
    _d2, s, t, px, py = best
    return s, t, px, py


def _bearing_chord(coords, origin, dist: float) -> float:
    x, y, *_ = _at(coords, dist)
    return math.degrees(math.atan2(y - origin[1], x - origin[0]))


def _angle(a: float, b: float) -> float:
    turn = abs(a - b) % 360.0
    return min(turn, 360.0 - turn)


def _write_applied(proc: Path, before: np.ndarray, after: np.ndarray, tf) -> int:
    """Copy changed pixels into the rasters the road mesh is cut from."""
    from filter_road_corridor import write_referenced_tif

    changed = np.isfinite(after) & (after != before)
    n = int(np.count_nonzero(changed))
    if n == 0:
        print("apply: no pixels changed")
        return 0
    a, _d, _b, _e, c, f = tf
    res = abs(float(a))
    xmin = float(c) - 0.5 * res
    ymax = float(f) + 0.5 * res
    smooth_dir = proc / "road_surface_smooth"
    for name in ("04_smooth.tif", "04_layer0.tif"):
        path = smooth_dir / name
        backup = smooth_dir / name.replace(".tif", ".pre_junction.tif")
        if not backup.is_file():
            shutil.copy2(path, backup)
            tfw = path.with_suffix(".tfw")
            if tfw.is_file():
                shutil.copy2(tfw, backup.with_suffix(".tfw"))
            print(f"backup {backup.name}")
        arr = np.asarray(tiff.imread(path), dtype=np.float32)
        if arr.ndim == 3:
            arr = arr[..., 0]
        if arr.shape != after.shape:
            raise SystemExit(f"{name} shape {arr.shape} != blend {after.shape}")
        missing = int(np.count_nonzero(changed & ~np.isfinite(arr)))
        arr = arr.copy()
        arr[changed] = after[changed]
        write_referenced_tif(path, arr, xmin=xmin, ymax=ymax, res=res)
        print(f"apply: {n} px -> {path.name}" + (f", {missing} were empty" if missing else ""))
    return n


def main() -> None:
    import geopandas as gpd
    from shapely.geometry import Point

    ap = argparse.ArgumentParser()
    ap.add_argument("--oids", type=int, nargs="+", default=list(OIDS))
    ap.add_argument("--out", default="")
    ap.add_argument("--apply", action="store_true", help="write the blend into 04_smooth.tif and 04_layer0.tif")
    args = ap.parse_args()
    oids = tuple(args.oids)

    site = load_site()
    proc = processed_dir(site)
    out_dir = proc / (args.out or "blend_trial_am_pirchet")
    out_dir.mkdir(parents=True, exist_ok=True)

    routing = json.loads((proc / "gip_routing" / "gip_routing.json").read_text(encoding="utf-8"))
    roads = routing["roads"]
    nodes = routing["nodes"]
    sets = {oid: set(roads[str(oid)]["nodes"]) for oid in oids}
    shared = set.intersection(*(sets[oid] for oid in oids))
    if not shared:
        raise SystemExit(f"no node shared by {oids}")
    nid = sorted(shared)[0]
    node = nodes[nid]
    origin = (float(node["x"]), float(node["y"]))
    classes = {oid: str(roads[str(oid)].get("objekt") or "S-G") for oid in oids}

    gdf = gpd.read_file(proc / "road_surface_smooth" / "carriageway_smooth.gpkg", layer="carriageway")
    cl = gpd.read_file(proc / "centerline_shift_taper" / "centerline_shift.gpkg", layer="centerline")
    path = proc / "road_surface_smooth" / "04_smooth.tif"
    before = np.asarray(tiff.imread(path), dtype=np.float32)
    if before.ndim == 3:
        before = before[..., 0]
    tf = _tfw(path)
    after = before.copy()

    arms = {}
    for oid in oids:
        line = cl[cl["objectid"].astype(int) == oid].iloc[0].geometry
        if line.geom_type == "MultiLineString":
            line = max(line.geoms, key=lambda g: g.length)
        poly = gdf[gdf["objectid"].astype(int) == oid].iloc[0].geometry
        coords = _orient(line, origin)
        arms[oid] = {"coords": coords, "poly": poly, "cls": classes[oid]}

    bearings = {}
    print("chords from the node")
    for dist in (5.0, 10.0, 15.0, 20.0, 30.0):
        angs = {}
        for oid in oids:
            x, y, *_ = _at(arms[oid]["coords"], dist)
            angs[oid] = math.degrees(math.atan2(y - origin[1], x - origin[0]))
        ids = list(oids)
        bits = []
        for i, a in enumerate(ids):
            for b in ids[i + 1 :]:
                bits.append(f"{a}/{b} {_angle(angs[a], angs[b]):.0f}°")
        print(f"  {dist:.0f} m  " + "  ".join(f"{o} {angs[o]:+.0f}°" for o in oids) + "  | " + " ".join(bits))
    bearings = {oid: _bearing_chord(arms[oid]["coords"], origin, 10.0) for oid in oids}
    pairs = []
    ids = list(oids)
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            pairs.append((_angle(bearings[a], bearings[b]), a, b))
    pairs.sort()
    through_angle, ta, tb = pairs[-1]
    rest = [p for p in pairs if p[1] not in (ta, tb) and p[2] not in (ta, tb)]
    if rest:
        other_angle, oa, ob = rest[-1]
    else:
        other_angle, oa, ob = pairs[-2][0], pairs[-2][1], pairs[-2][2]
    branch_ids = [oid for oid in oids if oid not in (ta, tb)]
    branch = branch_ids[0]
    clear = (180.0 - through_angle) + 8.0 < (180.0 - other_angle)
    same_class = len({classes[oid] for oid in oids}) == 1
    # Two disjoint pairs both near 180°, same class: the patch sits in the middle.
    middle = same_class and through_angle >= 160.0 and other_angle >= 160.0 and not clear
    print(
        f"node {origin[0]:.1f},{origin[1]:.1f} pairs {ta}/{tb} {through_angle:.0f}° "
        f"and {oa}/{ob} {other_angle:.0f}° middle={middle} clear={clear}"
    )
    for oid, ang in bearings.items():
        print(f"  {oid} {classes[oid]} leaves at {ang:+.0f}°")

    def fit(oid: int) -> None:
        coords = arms[oid]["coords"]
        length = 0.0
        for i in range(1, len(coords)):
            length += math.hypot(coords[i][0] - coords[i - 1][0], coords[i][1] - coords[i - 1][1])
        fit_to = min(FIT_TO_M, max(8.0, length - 1.5))
        fit_from = min(FIT_FROM_M, max(3.0, fit_to - 8.0))
        ss, zz, slopes = [], [], []
        for s in np.arange(fit_from, fit_to + 0.1, 2.0):
            x, y, dx, dy = _at(coords, float(s))
            z = _sample(before, tf, x, y)
            if not np.isfinite(z):
                continue
            ss.append(s)
            zz.append(z)
            n = math.hypot(dx, dy) or 1.0
            lx, ly = -dy / n, dx / n
            zl = _sample(before, tf, x + 2.0 * lx, y + 2.0 * ly)
            zr = _sample(before, tf, x - 2.0 * lx, y - 2.0 * ly)
            if np.isfinite(zl) and np.isfinite(zr):
                slopes.append((zl - zr) / 4.0)
        slope, intercept = np.polyfit(ss, zz, 1)
        arms[oid]["a"] = float(intercept)
        arms[oid]["b"] = float(slope)
        arms[oid]["c"] = float(np.median(slopes)) if slopes else 0.0
        # Half-width from the polygon, per side, outside the mouth.
        left, right = [], []
        poly = arms[oid]["poly"]
        for s in (10.0, 14.0, 18.0, 22.0, 26.0):
            x, y, dx, dy = _at(coords, s)
            n = math.hypot(dx, dy) or 1.0
            lx, ly = -dy / n, dx / n
            for sign, bucket in ((1.0, left), (-1.0, right)):
                last = 0.0
                for step in np.arange(0.5, 12.1, 0.5):
                    if not poly.covers(Point(x + sign * lx * step, y + sign * ly * step)):
                        break
                    last = float(step)
                if last > 0:
                    bucket.append(last)
        arms[oid]["half_left"] = float(np.median(left)) if left else 3.0
        arms[oid]["half_right"] = float(np.median(right)) if right else 3.0
        print(
            f"  fit {oid}: z0={arms[oid]['a']:.2f} grade={100 * arms[oid]['b']:.1f}% "
            f"crossfall={100 * arms[oid]['c']:.1f}% "
            f"half L/R {arms[oid]['half_left']:.1f}/{arms[oid]['half_right']:.1f}"
        )

    for oid in oids:
        fit(oid)

    def model(oid: int, s: float, t: float) -> float:
        arm = arms[oid]
        return arm["a"] + arm["b"] * s + arm["c"] * t

    z_node = _sample(before, tf, origin[0], origin[1])
    n_write = 0
    max_lat = 0.0
    if middle:
        z_mid = sum(arms[oid]["a"] for oid in oids) / len(oids)
        targets = {}
        print(f"middle {z_mid:.2f} m, raster at the node {z_node:.2f}")
        for oid in oids:
            cap = GRADE_LIMIT.get(classes[oid], 0.15) * R_ALONG_M
            delta = z_mid - arms[oid]["a"]
            if abs(delta) > cap:
                print(f"  {oid} limited to {cap:.2f} m of {delta:.2f} m")
                delta = math.copysign(cap, delta)
            targets[oid] = arms[oid]["a"] + delta
            print(
                f"  {oid} {classes[oid]} {arms[oid]['a']:.2f} -> {targets[oid]:.2f} "
                f"({100 * abs(delta) / R_ALONG_M:.1f}%)"
            )

        def on_arm(oid: int, s: float, t: float) -> float:
            w = _smoothstep(min(1.0, max(0.0, s) / R_ALONG_M))
            arm = arms[oid]
            z_axis = arm["a"] + arm["b"] * s + (1.0 - w) * (targets[oid] - arm["a"])
            return z_axis + (w * arm["c"]) * t

        a_tf, _d, _b, e, c0, f0 = tf
        reach = R_ALONG_M + 8.0
        c_lo = max(0, int((origin[0] - reach - c0) / a_tf) - 1)
        c_hi = min(before.shape[1] - 1, int((origin[0] + reach - c0) / a_tf) + 1)
        r_lo, r_hi = sorted(
            (int((origin[1] + reach - f0) / e), int((origin[1] - reach - f0) / e))
        )
        r_lo = max(0, r_lo - 1)
        r_hi = min(before.shape[0] - 1, r_hi + 1)
        for row in range(r_lo, r_hi + 1):
            for col in range(c_lo, c_hi + 1):
                if not np.isfinite(before[row, col]):
                    continue
                x, y = _xy(tf, col, row)
                if not any(arms[oid]["poly"].covers(Point(x, y)) for oid in oids):
                    continue
                hits = []
                for oid in oids:
                    s, t, _px, _py = _project(arms[oid]["coords"], x, y)
                    hits.append((oid, s, t))
                oid_n, s_n, t_n = min(hits, key=lambda h: abs(h[2]))
                half_n = max(arms[oid_n]["half_left"], arms[oid_n]["half_right"], 2.0)
                if s_n < -0.5 or s_n > R_ALONG_M or abs(t_n) > half_n + 0.5:
                    continue
                num = den = 0.0
                for oid, s, t in hits:
                    half = max(arms[oid]["half_left"], arms[oid]["half_right"], 2.0)
                    if s < -0.5 or s > R_ALONG_M or abs(t) > half:
                        continue
                    inf = math.exp(-0.5 * (t / half) ** 2)
                    num += inf * on_arm(oid, max(s, 0.0), t)
                    den += inf
                if den <= 0.0:
                    continue
                after[row, col] = np.float32(num / den)
                n_write += 1
        print(f"wrote {n_write} px")
    else:
        bx, by, *_ = _at(arms[branch]["coords"], 8.0)
        for oid in (ta, tb):
            _s, t, _px, _py = _project(arms[oid]["coords"], bx, by)
            arms[oid]["branch_sign"] = 1.0 if t >= 0 else -1.0
        limit = GRADE_LIMIT.get(classes[branch], 0.15)
        z_far = model(branch, R_ALONG_M, 0.0)
        grade_along = abs(z_far - z_node) / R_ALONG_M
        print(
            f"branch {branch} limit {100 * limit:.1f}%  node {z_node:.2f}  "
            f"at {R_ALONG_M:.0f} m {z_far:.2f}  longitudinal {100 * grade_along:.1f}%"
        )
        if grade_along > limit:
            print("ramp steeper than the class limit, surface unchanged")
        else:
            def nearest_through(x: float, y: float):
                best = None
                for oid in (ta, tb):
                    s, t, px, py = _project(arms[oid]["coords"], x, y)
                    if best is None or abs(t) < abs(best[2]):
                        best = (oid, s, t, px, py)
                return best

            a_tf, _d, _b, e, c0, f0 = tf
            minx, miny = origin[0] - 40.0, origin[1] - 40.0
            maxx, maxy = origin[0] + 40.0, origin[1] + 40.0
            c_lo = max(0, int((minx - c0) / a_tf) - 1)
            c_hi = min(before.shape[1] - 1, int((maxx - c0) / a_tf) + 1)
            r_lo, r_hi = sorted((int((maxy - f0) / e), int((miny - f0) / e)))
            r_lo = max(0, r_lo - 1)
            r_hi = min(before.shape[0] - 1, r_hi + 1)
            poly_b = arms[branch]["poly"]
            for row in range(r_lo, r_hi + 1):
                for col in range(c_lo, c_hi + 1):
                    if not np.isfinite(before[row, col]):
                        continue
                    x, y = _xy(tf, col, row)
                    if not poly_b.covers(Point(x, y)):
                        continue
                    s_b, t_b, _px, _py = _project(arms[branch]["coords"], x, y)
                    if s_b > R_ALONG_M or s_b < -1.0:
                        continue
                    oid_t, s_t, t_t, _fx, _fy = nearest_through(x, y)
                    sign = arms[oid_t]["branch_sign"]
                    if t_t * sign < -0.5:
                        continue
                    half = arms[oid_t]["half_left"] if sign > 0 else arms[oid_t]["half_right"]
                    d_edge = abs(t_t) - half
                    if d_edge <= 0.0:
                        continue
                    axis_x, axis_y, *_rest = _at(arms[branch]["coords"], max(s_b, 0.0))
                    _sb, t_axis, _qx, _qy = _project(arms[oid_t]["coords"], axis_x, axis_y)
                    gap = max(0.5, abs(t_axis) - half)
                    z_edge = model(oid_t, s_t, sign * half)
                    z_br = model(branch, s_b, t_b)
                    u = min(1.0, d_edge / gap)
                    wgt = _smoothstep(u)
                    after[row, col] = np.float32((1.0 - wgt) * z_edge + wgt * z_br)
                    n_write += 1
                    max_lat = max(max_lat, abs(z_edge - z_br) / gap)
            print(f"wrote {n_write} px, steepest lateral grade {100 * max_lat:.0f}%")

    if args.apply:
        _write_applied(proc, before, after, tf)

    def point(oid: int, s: float, t: float) -> tuple[float, float]:
        x, y, dx, dy = _at(arms[oid]["coords"], s)
        n = math.hypot(dx, dy) or 1.0
        return x + t * (-dy / n), y + t * (dx / n)

    stations = np.arange(0.0, 25.0, 1.0)
    offsets = (0.0, 2.0, -2.0, 5.0, -5.0)
    rows_txt = ["arm,offset_m,s_m,before_m,after_m,delta_m"]

    def series(oid: int, t: float):
        zb, za, ss = [], [], []
        for s in stations:
            x, y = point(oid, float(s), t)
            if not any(arms[i]["poly"].covers(Point(x, y)) for i in oids):
                zb.append(np.nan)
                za.append(np.nan)
            else:
                hb, ha = _sample(before, tf, x, y), _sample(after, tf, x, y)
                zb.append(hb)
                za.append(ha)
                if float(s) in (0.0, 2.0, 5.0, 10.0, 15.0, 20.0):
                    rows_txt.append(
                        f"{oid},{t:.0f},{s:.0f},{hb:.3f},{ha:.3f},{ha - hb:.3f}"
                    )
            ss.append(s)
        return np.asarray(ss), np.asarray(zb), np.asarray(za)

    face = {}
    for oid in oids:
        best_sign, best_d = 1.0, 1.0e9
        for sign in (1.0, -1.0):
            x, y = point(oid, 6.0, sign * 2.0)
            dist = min(
                abs(_project(arms[i]["coords"], x, y)[1]) for i in oids if i != oid
            )
            if dist < best_d:
                best_d, best_sign = dist, sign
        face[oid] = best_sign

    n_arm = len(oids)
    fig, axes = plt.subplots(n_arm, 5, figsize=(16, 2.6 * n_arm), sharex=True, sharey=True, squeeze=False)
    col_spec = (
        (0.0, "Achse"),
        (None, "2 m zur anderen"),
        (None, "2 m außen"),
        (None, "5 m zur anderen"),
        (None, "5 m außen"),
    )
    for r, oid in enumerate(oids):
        sign = face[oid]
        cols = (
            (0.0, "Achse"),
            (sign * 2.0, "2 m zur anderen"),
            (-sign * 2.0, "2 m außen"),
            (sign * 5.0, "5 m zur anderen"),
            (-sign * 5.0, "5 m außen"),
        )
        for c, (off, title) in enumerate(cols):
            ax = axes[r, c]
            ss, zb, za = series(oid, off)
            ax.plot(ss, zb, color="#9ca3af", linewidth=1.4, label="vorher")
            ax.plot(ss, za, color="#1d4ed8", linewidth=1.4, label="nachher")
            ax.set_title(f"{oid}  {title}", fontsize=9)
            ax.grid(True, linewidth=0.3)
            if r == n_arm - 1:
                ax.set_xlabel("Station ab Knoten (m)")
            if c == 0:
                ax.set_ylabel("Höhe (m)")
            if r == 0 and c == 0:
                ax.legend(fontsize=8)
    if middle:
        fig.suptitle(
            f"Längs, Vorher/Nachher. Mitte aus {ta}/{tb} ({through_angle:.0f}°) "
            f"und {oa}/{ob} ({other_angle:.0f}°), R = {R_ALONG_M:.0f} m"
        )
    else:
        fig.suptitle(
            f"Längs, Vorher/Nachher. Durchgehend {ta}/{tb} ({through_angle:.0f}°), "
            f"Rampe auf {branch}, R = {R_ALONG_M:.0f} m"
        )
    fig.tight_layout()
    fig.savefig(out_dir / "laengs.png", dpi=120)
    plt.close(fig)

    fig, axes = plt.subplots(n_arm, 3, figsize=(12, 2.6 * n_arm), sharey=True, squeeze=False)
    for r, oid in enumerate(oids):
        for c, s_cut in enumerate((0.0, 2.0, 5.0)):
            ax = axes[r, c]
            offs = np.arange(-8.0, 8.01, 0.5)
            zb, za = [], []
            for t in offs:
                x, y = point(oid, s_cut, float(t))
                zb.append(_sample(before, tf, x, y))
                za.append(_sample(after, tf, x, y))
            ax.plot(offs, zb, color="#9ca3af", linewidth=1.4, label="vorher")
            ax.plot(offs, za, color="#1d4ed8", linewidth=1.4, label="nachher")
            ax.set_title(f"{oid}  quer bei {s_cut:.0f} m", fontsize=9)
            ax.set_xlabel("Abstand zur Achse (m), links positiv")
            ax.grid(True, linewidth=0.3)
            if c == 0:
                ax.set_ylabel("Höhe (m)")
            if r == 0 and c == 0:
                ax.legend(fontsize=8)
    fig.suptitle("Querprofile am Knoten und bei 2 m und 5 m")
    fig.tight_layout()
    fig.savefig(out_dir / "quer.png", dpi=120)
    plt.close(fig)

    report = out_dir / "profile.csv"
    report.write_text("\n".join(rows_txt) + "\n", encoding="utf-8")
    print(f"wrote {out_dir / 'laengs.png'}")
    print(f"wrote {out_dir / 'quer.png'}")
    print(f"wrote {report}")


if __name__ == "__main__":
    main()
