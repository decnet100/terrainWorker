"""The road surface Magliasina built (magliasinaNG roads_only), read straight from its chunk files, in BeamNG metres.

Used where a structure deck takes over from a Magliasina road (build_galleries, the handover line) and where
Magliasina's mesh is cut at that line (import_magliasina_roads). Magliasina leaves galleries and tunnels out, but
the gap in the export does not start where the portal is: at the Klammleck chain (Pfunds-Spiss) its asphalt ran about
8 m into both galleries, banked 7-9 %, under a level gallery deck.

Coordinates as in import_magliasina_roads: local (x, y) -> site CRS (E0 + x K, N0 + y K) -> BeamNG terrain metres;
z -> z - z_min_m of the heightmap.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

ASPHALT = {
    "mp_road_asphalt", "mp_road_asphalt_fresh", "mp_hard_asphalt", "mp_road_sett", "mp_road_cobble",
}


def to_beamng(sc, e0: float, n0: float, k: float, z_min: float, v: np.ndarray) -> np.ndarray:
    X = e0 + v[:, 0] * k
    Y = n0 + v[:, 1] * k
    bx = (X - sc.xmin) / sc.bw * sc.terrain_span
    by = (Y - sc.ymin) / sc.bh * sc.terrain_span
    return np.column_stack([bx, by, v[:, 2] - z_min])


def roads_dir(proc: Path) -> Path:
    return proc / "magliasina" / "magliasina_work" / "work" / "roads_only"


def frame(site, sc, proc: Path) -> tuple[float, float, float, float] | None:
    """(e0, n0, k, z_min) of the Magliasina export of this site, None without one."""
    man = proc / "magliasina" / "manifest.json"
    meta = proc / "heightmap_meta.json"
    if not man.is_file() or not meta.is_file():
        return None
    m = json.loads(man.read_text(encoding="utf-8"))
    h = json.loads(meta.read_text(encoding="utf-8"))
    e0, n0 = (float(v) for v in m["origin"])
    return e0, n0, float(m["scale_factor_at_origin"]), float(h["z_min_m"])


class Surface:
    """Asphalt triangles of Magliasina inside a box (BeamNG metres), with a point query on a 4 m bucket grid."""

    CELL = 4.0

    def __init__(self, tris: np.ndarray):
        self.P = tris  # (n, 3, 3)
        self.a = tris[:, 0]
        self.e1 = tris[:, 1] - self.a
        self.e2 = tris[:, 2] - self.a
        self.det = self.e1[:, 0] * self.e2[:, 1] - self.e1[:, 1] * self.e2[:, 0]
        lo = np.floor(tris[:, :, :2].min(1) / self.CELL).astype(int)
        hi = np.floor(tris[:, :, :2].max(1) / self.CELL).astype(int)
        self.grid: dict[tuple[int, int], list[int]] = {}
        for i in range(len(tris)):
            for cx in range(lo[i, 0], hi[i, 0] + 1):
                for cy in range(lo[i, 1], hi[i, 1] + 1):
                    self.grid.setdefault((cx, cy), []).append(i)

    def __len__(self) -> int:
        return len(self.P)

    def z(self, x: float, y: float, ref: float | None = None) -> float | None:
        """Surface height at (x, y); with several layers the one nearest ``ref`` (else the lowest)."""
        idx = self.grid.get((int(math.floor(x / self.CELL)), int(math.floor(y / self.CELL))))
        if not idx:
            return None
        i = np.array(idx)
        a, e1, e2, det = self.a[i], self.e1[i], self.e2[i], self.det[i]
        rx, ry = x - a[:, 0], y - a[:, 1]
        with np.errstate(all="ignore"):
            u = (rx * e2[:, 1] - ry * e2[:, 0]) / det
            w = (e1[:, 0] * ry - e1[:, 1] * rx) / det
            ok = (np.abs(det) > 1e-9) & (u >= -1e-7) & (w >= -1e-7) & (u + w <= 1 + 1e-7)
            if not ok.any():
                return None
            zs = (a[:, 2] + u * e1[:, 2] + w * e2[:, 2])[ok]
        if ref is None:
            return float(zs.min())
        return float(zs[np.argmin(np.abs(zs - ref))])

    def section(
        self, x: float, y: float, tx: float, ty: float, *, ref: float | None = None, max_half: float = 9.0,
        step: float = 0.05,
    ) -> dict | None:
        """Cross-section through (x, y) across the direction (tx, ty): the asphalt run that contains (x, y).

        Returns the edges (offset left +, z), the middle of the run (offset, z), width and crossfall (dz per m to
        the right, as build_bridges.sample_crossfall); None if (x, y) is not on asphalt.
        """
        h = math.hypot(tx, ty) or 1.0
        lx, ly = -ty / h, tx / h
        z0 = self.z(x, y, ref)
        if z0 is None:
            return None

        def walk(sign: float) -> tuple[float, float]:
            o, z_last = 0.0, z0
            while o + step <= max_half:
                zz = self.z(x + lx * sign * (o + step), y + ly * sign * (o + step), z_last)
                if zz is None or abs(zz - z_last) > 0.25:  # off the asphalt, or onto another layer
                    break
                o += step
                z_last = zz
            # the edge lies between o and o + step: bisect it to the millimetre, so the deck's width matches
            # (a whole step short on one side showed as a 5-10 cm offset at the handover)
            a, b = o, o + step
            for _ in range(8):
                m = 0.5 * (a + b)
                zz = self.z(x + lx * sign * m, y + ly * sign * m, z_last)
                if zz is None or abs(zz - z_last) > 0.25:
                    b = m
                else:
                    a = m
            if a > o:
                zz = self.z(x + lx * sign * a, y + ly * sign * a, z_last)
                if zz is not None:
                    o, z_last = a, zz
            return o, z_last

        ol, zl = walk(1.0)
        orr, zr = walk(-1.0)
        width = ol + orr
        mid = 0.5 * (ol - orr)  # offset of the middle, left positive
        zm = self.z(x + lx * mid, y + ly * mid, z0)
        return {
            "left_m": round(ol, 3), "right_m": round(orr, 3), "z_left": round(zl, 3), "z_right": round(zr, 3),
            "width": round(width, 3), "mid_offset": round(mid, 3), "z_mid": round(zm if zm is not None else z0, 3),
            "crossfall": round((zr - zl) / max(width, 0.1), 5),
        }


def load_surface(site, sc, proc: Path, box: tuple[float, float, float, float]) -> Surface | None:
    """Asphalt of Magliasina's roads_only output inside ``box`` (BeamNG metres x0, y0, x1, y1); None without it."""
    fr = frame(site, sc, proc)
    rd = roads_dir(proc)
    if fr is None or not rd.is_dir():
        return None
    e0, n0, k, z_min = fr
    out = []
    for f in sorted(rd.glob("road_*.npz")):
        d = np.load(f)
        for i, mat in enumerate(d["materials"]):
            if str(mat) not in ASPHALT:
                continue
            v = to_beamng(sc, e0, n0, k, z_min, d[f"v{i}"])
            t = d[f"t{i}"]
            if not len(t):
                continue
            c = v[t].mean(1)
            keep = (c[:, 0] >= box[0]) & (c[:, 0] <= box[2]) & (c[:, 1] >= box[1]) & (c[:, 1] <= box[3])
            if keep.any():
                out.append(v[t[keep]])
    if not out:
        return None
    return Surface(np.concatenate(out))


# ---------------------------------------------------------------------------------------------------------------
# cutting the mesh at a handover line


def clip_halfplane(tri: np.ndarray, c: np.ndarray, n: np.ndarray) -> list[np.ndarray]:
    """Part of a triangle (3, 3) on the side (p - c) . n <= 0 (2D test), as triangles."""
    d = (tri[:, :2] - c) @ n
    if (d <= 1e-9).all():
        return [tri]
    if (d >= -1e-9).all():
        return []
    poly = []
    for i in range(3):
        p, q = tri[i], tri[(i + 1) % 3]
        dp, dq = d[i], d[(i + 1) % 3]
        if dp <= 0:
            poly.append(p)
        if (dp < 0 < dq) or (dq < 0 < dp):
            t = dp / (dp - dq)
            poly.append(p + t * (q - p))
    return [np.array([poly[0], poly[k], poly[k + 1]]) for k in range(1, len(poly) - 1)]


def cut_at_lines(pos: np.ndarray, faces: np.ndarray, lines: list[dict]) -> tuple[np.ndarray, np.ndarray, int]:
    """Cut a mesh (BeamNG metres) at handover lines; the part beyond a line (into the structure) is dropped.

    A line: centre (x, y), direction ``nx, ny`` pointing into the structure, ``half_m`` (lateral extent of the cut),
    ``depth_m`` (how far into the structure triangles are dropped). Only triangles whose centre lies within that
    band are touched, so a road crossing nearby keeps its mesh. Returns (pos, faces, triangles changed).
    """
    if not lines or not len(faces):
        return pos, faces, 0
    tris = pos[faces]
    cen = tris.mean(1)[:, :2]
    hit = np.zeros(len(faces), bool)
    for ln in lines:
        c = np.array([ln["x"], ln["y"]])
        n = np.array([ln["nx"], ln["ny"]])
        lat = np.array([-n[1], n[0]])
        along = (cen - c) @ n
        side = (cen - c) @ lat
        hit |= (np.abs(side) <= ln["half_m"]) & (along >= -4.0) & (along <= ln["depth_m"])
    if not hit.any():
        return pos, faces, 0
    keep = [tris[i] for i in np.where(~hit)[0]]
    changed = 0
    for i in np.where(hit)[0]:
        parts = [tris[i]]
        for ln in lines:
            c = np.array([ln["x"], ln["y"]])
            n = np.array([ln["nx"], ln["ny"]])
            lat = np.array([-n[1], n[0]])
            cc = tris[i].mean(0)[:2]
            if abs((cc - c) @ lat) > ln["half_m"] or not (-4.0 <= (cc - c) @ n <= ln["depth_m"]):
                continue
            parts = [p for t in parts for p in clip_halfplane(t, c, n)]
        if len(parts) != 1 or parts[0] is not tris[i]:
            changed += 1
        keep.extend(parts)
    T = np.array(keep).reshape(-1, 3)
    return T, np.arange(len(T)).reshape(-1, 3), changed
