"""Passages through tubes (galleries, tunnels): terrain in the clear space at the portals, snapped clear or holed.

Clear space of a tube, portal to portal (``portal_s``): across to the wall's inner face on a closed side (the deck
edge, or half ``clear_width_m`` where that is wider) and to the deck edge + 15 cm on an open side; in height from
the deck surface (the built deck MeshRoad with its crossfall) to the roof underside (node z_roof_inner). Deck slab,
roof slab and wall are solid, so terrain inside them is hidden.

1. Search band: half the deck width plus ``passage_band_px`` (1.5) terrain pixels. A vertex in the band at a height
   inside the clear space is a passage vertex.
2. Snap: one at most ``passage_snap_down_m`` (1 m) above the deck surface goes just below it; one at most
   ``passage_snap_up_m`` (1 m) under the roof top (z_roof_outer) just above that (not on the open side).
   ``passage_snap_clear_m`` (5 cm) is the clearance.
3. Majority: any other vertex of the band above the deck surface whose 8 neighbours lie at least
   ``passage_majority`` (5) times under it goes down too, however high it is (a spike the carving missed); one
   under the roof underside with 5 neighbours over it goes up (not on the open side). Three passes: a straight or
   diagonal mountain front has at most 3 neighbours on the other side and stays.
4. Open side of a gallery, before 2 and 3: from the open deck edge ``passage_open_in_m`` (1.5 m) inward the ground
   may not stand over the roof top (it goes just under it, into the roof slab: ``open_roof``), and
   ``passage_open_out_m`` (1.5 m) outward not over the deck underside (``open_down``). Squares there that then run
   from under the deck to the roof are holed (``open``), with ``passage_open_ring_px`` (0) rings of neighbours.
5. Hole every terrain square with a triangle (the terrain's own, terrain_holes.square_triangles) still in the clear
   space: ``transit`` where the ground runs from under the deck to over the roof, ``inside`` around a vertex that
   stayed in. Plus ``passage_ring_px`` (1) ring of neighbours and any square the hole encloses (``island``).

The snapped heights go into the heightmap layer ``portal_snap`` (above every other layer). Every changed vertex and
every holed square is written to ``portal_passages.json``, ``portal_passages.csv`` and a map per portal
``portal_passages_<name>_<id>_<n>.png``.

Run alone (after import_magliasina_roads / compose_heightmap; build_galleries runs it too):
  $env:AUTOROAD_SITE = "config/sites/pfunds_spiess.yaml"
  python tools/portal_passages.py
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import terrain_holes as th

LAYER = "portal_snap"
DEFAULTS = {
    "passage_band_px": 1.5,
    "passage_snap_down_m": 1.0,
    "passage_snap_up_m": 1.0,
    "passage_snap_clear_m": 0.05,
    "passage_ring_px": 1,
    "passage_majority": 5,
    "passage_open_in_m": 1.5,
    "passage_open_out_m": 1.5,
    "passage_open_ring_px": 0,
}
SIDE_MARGIN_M = 0.15  # clear space beyond the deck edge on an open side, as terrain_holes.intruding_squares


def settings_for(g: dict, defaults: dict) -> dict:
    out = {}
    for k, v in DEFAULTS.items():
        val = g.get(k) if g.get(k) is not None else defaults.get(k)
        out[k] = type(v)(val) if val is not None else v
    return out


def deck_banks(decks: list[dict] | None) -> np.ndarray | None:
    """Sections of the built deck MeshRoads: rows x, y, z, bank (height change per metre to the LEFT)."""
    rows = []
    for e in decks or []:
        nd = np.array(e.get("nodes") or [], dtype=np.float64)
        if len(nd) < 2 or nd.shape[1] < 8:
            continue
        c = nd[:, :3]
        up = nd[:, 5:8] / np.maximum(np.linalg.norm(nd[:, 5:8], axis=1, keepdims=True), 1e-9)
        fwd = np.gradient(c, axis=0)
        fwd /= np.maximum(np.linalg.norm(fwd, axis=1, keepdims=True), 1e-9)
        right = np.cross(fwd, up)
        right /= np.maximum(np.linalg.norm(right[:, :2], axis=1, keepdims=True), 1e-9)
        rows += [(x, y, z, -rz) for (x, y, z), rz in zip(c.tolist(), right[:, 2].tolist())]
    return np.array(rows, dtype=np.float64) if rows else None


def _axis(g: dict, defaults: dict, banks: np.ndarray | None = None) -> np.ndarray:
    """Rows x, y, s, z_road, lo (deck underside), hi (roof top), half width, roof underside, bank; sorted by s.

    With a built deck (nearest deck section within 2 m and 1 m in height) z_road and lo are its surface height in
    the axis and bank its crossfall; without one, the node z_road and bank 0.
    """
    deck_t = float(g.get("deck_depth_m") if g.get("deck_depth_m") is not None else defaults.get("deck_depth_m") or 0.5)
    clear_h = float(g.get("clear_height_m") or defaults.get("clear_height_m") or 4.0)
    roof_t = float(g.get("roof_thickness_m") or defaults.get("roof_thickness_m") or 0.5)
    rows = []
    for n in g.get("nodes") or []:
        z = float(n["z_road"])
        hi = n.get("z_roof_outer")
        hi = float(hi) if hi is not None else z + clear_h + roof_t
        ri = n.get("z_roof_inner")
        ri = float(ri) if ri is not None else z + clear_h
        x, y = float(n["x"]), float(n["y"])
        bank = 0.0
        if banks is not None:
            d2 = (banks[:, 0] - x) ** 2 + (banks[:, 1] - y) ** 2
            k = int(np.argmin(d2))
            if d2[k] <= 4.0 and abs(banks[k, 2] - z) <= 1.0:
                bank = float(banks[k, 3])
                z = float(banks[k, 2])
        rows.append((x, y, float(n["s"]), z, z - deck_t, hi, 0.5 * float(n.get("width") or 7.5), ri, bank))
    ax = np.array(rows, dtype=np.float64)
    return ax[np.argsort(ax[:, 2])] if len(ax) else ax


def _project(points: np.ndarray, ax: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Nearest point on the axis for every point: s, signed offset (+ left of travel), and columns 3.. of ax."""
    p0 = ax[:-1, :2]
    d = ax[1:, :2] - p0
    seg2 = np.maximum((d * d).sum(1), 1e-12)
    best = np.full(len(points), np.inf)
    s = np.zeros(len(points))
    off = np.zeros(len(points))
    cols = np.zeros((len(points), ax.shape[1] - 3))
    for k in range(len(p0)):
        rel = points - p0[k]
        t = np.clip((rel @ d[k]) / seg2[k], 0.0, 1.0)
        q = rel - t[:, None] * d[k]
        dist = np.hypot(q[:, 0], q[:, 1])
        b = dist < best
        if not b.any():
            continue
        best[b] = dist[b]
        cross = d[k, 0] * rel[:, 1] - d[k, 1] * rel[:, 0]
        off[b] = np.where(cross >= 0, 1.0, -1.0)[b] * dist[b]
        tb = t[b][:, None]
        s[b] = ax[k, 2] + t[b] * (ax[k + 1, 2] - ax[k, 2])
        cols[b] = ax[k, 3:] + tb * (ax[k + 1, 3:] - ax[k, 3:])
    return s, off, cols


def _open_mask(off: np.ndarray, open_side: str) -> np.ndarray:
    side = (open_side or "none").lower()
    if side == "right":
        return off < 0
    if side == "left":
        return off > 0
    if side == "both":
        return np.ones(len(off), bool)
    return np.zeros(len(off), bool)


def tube_passages(
    work: np.ndarray,
    hole: np.ndarray | None,
    g: dict,
    info: dict,
    defaults: dict,
    *,
    size: int,
    pitch: float,
    banks: np.ndarray | None = None,
) -> dict | None:
    """Snap and hole one tube in place (``work`` heights, ``hole`` map). Returns the record or None."""
    portal_s = info.get("portal_s") or g.get("portal_s")
    ax = _axis(g, defaults, banks)
    if portal_s is None or len(portal_s) < 2 or len(ax) < 2:
        return None
    s0, s1 = sorted(float(v) for v in portal_s)
    cfg = settings_for(g, defaults)
    band = cfg["passage_band_px"] * pitch
    eps = cfg["passage_snap_clear_m"]
    open_side = str(info.get("open_side") or g.get("open_side") or "none")
    cw = g.get("clear_width_m") if g.get("clear_width_m") is not None else defaults.get("clear_width_m")
    clear_w = float(cw or 0.0)
    sel = (ax[:, 2] >= s0 - 3.0) & (ax[:, 2] <= s1 + 3.0)
    axs = ax[sel] if sel.sum() >= 2 else ax
    reach = float(axs[:, 6].max()) + band + 2 * pitch
    i0, j0, i1, j1 = th.square_range(
        (axs[:, 0].min() - reach, axs[:, 1].min() - reach, axs[:, 0].max() + reach, axs[:, 1].max() + reach),
        size, pitch,
    )
    j1, i1 = min(j1 + 1, size - 1), min(i1 + 1, size - 1)
    I, J = np.meshgrid(np.arange(i0, i1 + 1), np.arange(j0, j1 + 1))
    shape = I.shape
    I, J = I.ravel(), J.ravel()
    s, off, cols = _project(np.stack([I * pitch, J * pitch], 1), ax)
    z_road, lo, hi, hw, roof_in, bank = cols.T
    surf = z_road + np.clip(off, -hw, hw) * bank  # deck surface with its crossfall, held level beyond the edge
    lo = lo + np.clip(off, -hw, hw) * bank  # deck underside likewise
    opn = _open_mask(off, open_side)
    # a point before the first or past the last node projects onto that end (s exactly there): it lies beyond the
    # axis, in the next structure of a chain, and is not this tube's
    a_lo, a_hi = ax[0, 2] + 1e-3, ax[-1, 2] - 1e-3
    along = (s >= s0) & (s <= s1) & (s > a_lo) & (s < a_hi)
    in_band = along & (np.abs(off) <= hw + band)
    z = work[size - 1 - J, I].copy()  # before
    zc = z.copy()  # current
    W = shape[1]
    # candidate squares: a corner in the band. Their triangles (the terrain's own) as corner indices into the grid,
    # their sample points projected once; only the heights change from round to round
    band2 = in_band.reshape(shape)
    cand = band2[:-1, :-1] | band2[:-1, 1:] | band2[1:, :-1] | band2[1:, 1:]
    jj, ii = np.nonzero(cand)
    SI, SJ = ii + i0, jj + j0
    corners = []
    for i, j, li, lj in zip(SI.tolist(), SJ.tolist(), ii.tolist(), jj.tolist()):
        for tri in th.square_triangles(i, j):
            corners.append([(lj + dj) * W + (li + di) for di, dj in tri])
    C = np.array(corners, dtype=np.int64).reshape(-1, 3)  # (2 per square, 3)
    nb = len(th._BARY)
    sq_corners = C.reshape(-1, 6)  # the 4 corners of a square (with repeats)
    holes: list[dict] = []
    if len(C):
        XY = np.stack([I * pitch, J * pitch], 1)
        P = np.einsum("bk,tkc->tbc", th._BARY, XY[C]).reshape(-1, 2)
        ps, poff, pc = _project(P, ax)
        pz, _plo, _phi, phw, pri, pbk = pc.T
        psurf = pz + np.clip(poff, -phw, phw) * pbk
        # across: to the wall's inner face on a closed side (deck edge, or half clear_width_m if wider), the deck
        # edge + 15 cm on an open side
        popn = _open_mask(poff, open_side)
        plim = np.where(popn, phw + SIDE_MARGIN_M, np.maximum(phw, 0.5 * clear_w))
        pin = (ps >= s0) & (ps <= s1) & (ps > a_lo) & (ps < a_hi) & (np.abs(poff) <= plim)

    def hits(zv: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        Z = np.einsum("bk,tk->tb", th._BARY, zv[C]).reshape(-1)
        h = pin & (Z > psurf + 1e-3) & (Z < pri - 1e-3)
        d = np.where(h, np.minimum(Z - psurf, pri - Z), -1.0)
        return h.reshape(-1, 2 * nb).any(1), d

    action = np.full(len(z), "", dtype=object)
    need = int(cfg["passage_majority"])
    # open side of a gallery: from the open edge passage_open_in_m inward the ground may not stand over the roof top
    # (it goes just under it, into the roof slab); passage_open_out_m outward not over the deck underside. The squares
    # between the two strips then run from under the deck to the roof and are holed (below).
    rel = np.abs(off) - hw
    in_s = along & opn
    strip_in = in_s & (rel <= 0) & (rel >= -cfg["passage_open_in_m"])
    strip_out = in_s & (rel > 0) & (rel <= cfg["passage_open_out_m"])
    open_zone = strip_in | strip_out
    o_roof = strip_in & (z > hi - eps)
    o_dn = strip_out & (z > lo - eps)
    zc[o_roof] = hi[o_roof] - eps
    zc[o_dn] = lo[o_dn] - eps
    action[o_roof] = "open_roof"
    action[o_dn] = "open_down"
    for _ in range(4 if len(C) else 0):
        hit_sq, _d = hits(zc)
        if not hit_sq.any():
            break
        cv = np.zeros(len(z), bool)
        cv[np.unique(sq_corners[hit_sq])] = True
        cv &= in_band & (action == "")
        # threshold: just under the deck surface / just over the roof top
        dn = cv & (zc > surf + 1e-3) & (zc - surf <= cfg["passage_snap_down_m"])
        up = cv & (zc > surf + 1e-3) & (zc < roof_in - 1e-3) & (hi - zc <= cfg["passage_snap_up_m"]) & ~opn
        both = dn & up
        dn[both] = (zc - surf <= hi - zc)[both]
        up &= ~dn
        zc[dn] = surf[dn] - eps
        zc[up] = hi[up] + eps
        action[dn] = "snap_down"
        action[up] = "snap_up"
        cv &= ~(dn | up)
        # majority of the 8 neighbours under the deck / over the roof: a spike or a dent, not the mountain front
        below = np.pad((zc <= surf + 1e-3).reshape(shape).astype(np.int16), 1)
        above = np.pad((zc >= roof_in - 1e-3).reshape(shape).astype(np.int16), 1)
        nb_b = np.zeros(shape, np.int16)
        nb_a = np.zeros(shape, np.int16)
        for dj in (-1, 0, 1):
            for di in (-1, 0, 1):
                if di or dj:
                    nb_b += below[1 + dj:1 + dj + shape[0], 1 + di:1 + di + shape[1]]
                    nb_a += above[1 + dj:1 + dj + shape[0], 1 + di:1 + di + shape[1]]
        nb_b, nb_a = nb_b.ravel(), nb_a.ravel()
        md = cv & (zc > surf + 1e-3) & (nb_b >= need)
        mu = cv & ~md & (zc < roof_in - 1e-3) & (nb_a >= need) & ~opn
        zc[md] = surf[md] - eps
        zc[mu] = hi[mu] + eps
        action[md] = "snap_down_majority"
        action[mu] = "snap_up_majority"
        if not (dn.any() or up.any() or md.any() or mu.any()):
            break
    hit_sq = np.zeros(len(SI), bool)
    if len(C):
        hit_sq, depth = hits(zc)
        kept = np.zeros(len(z), bool)
        kept[np.unique(sq_corners[hit_sq])] = True
        kept &= (action == "") & (zc > surf + 1e-3) & (zc < roof_in - 1e-3)
        action[kept] = "kept_inside"
        dsq = depth.reshape(-1, 2 * nb)
        a_arg = dsq.argmax(1)
        deep = dsq.max(1)
        at_s = ps.reshape(-1, 2 * nb)[np.arange(len(dsq)), a_arg]
    else:
        kept = np.zeros(len(z), bool)
    go_down = np.isin(action, ["snap_down"])
    go_up = np.isin(action, ["snap_up"])
    ch = np.isin(action, ["snap_down", "snap_up", "snap_down_majority", "snap_up_majority", "open_down", "open_roof"])
    work[size - 1 - J[ch], I[ch]] = zc[ch]
    z_new = zc
    inside = in_band & (z > surf + 1e-3) & (z < roof_in - 1e-3)

    def vrec(k: int) -> dict:
        return {
            "px": int(I[k]), "py": int(size - 1 - J[k]), "x": round(float(I[k] * pitch), 2),
            "y": round(float(J[k] * pitch), 2), "s": round(float(s[k]), 2), "off": round(float(off[k]), 2),
            "z_before": round(float(z[k]), 3), "z_after": round(float(z_new[k]), 3),
            "dz": round(float(z_new[k] - z[k]), 3), "action": str(action[k]),
            "deck_surface": round(float(surf[k]), 3), "roof_under": round(float(roof_in[k]), 3),
            "roof_top": round(float(hi[k]), 3), "open_side": bool(opn[k]),
        }

    vertices = [vrec(k) for k in np.flatnonzero(action != "")]
    kept2 = kept.reshape(shape)
    sq_set = {(int(i), int(j)) for i, j, h in zip(SI, SJ, hit_sq) if h}
    # open side: squares that run from under the deck to the roof with a corner in the open strips; labelled open (no
    # ring) even when the clear-space test found them too
    open_sq: set[tuple[int, int]] = set()
    if open_zone.any():
        bel = (zc <= surf + 1e-3).reshape(shape)
        abv = (zc >= roof_in - 1e-3).reshape(shape)
        oz = open_zone.reshape(shape)

        def quad(a: np.ndarray) -> np.ndarray:
            return np.stack([a[:-1, :-1], a[:-1, 1:], a[1:, :-1], a[1:, 1:]])

        span = quad(bel).any(0) & quad(abv).any(0) & quad(oz).any(0)
        for lj, li in zip(*np.nonzero(span)):
            open_sq.add((int(li + i0), int(lj + j0)))
        sq_set |= open_sq
    kept_corner = set()
    for (i, j) in sq_set:
        for di in (0, 1):
            for dj in (0, 1):
                a, b = j + dj - j0, i + di - i0
                if 0 <= a < shape[0] and 0 <= b < shape[1] and kept2[a, b]:
                    kept_corner.add((i, j))
    ring: set[tuple[int, int]] = set()
    for (i, j) in sq_set:
        r = int(cfg["passage_open_ring_px"] if (i, j) in open_sq else cfg["passage_ring_px"])
        for di in range(-r, r + 1):
            for dj in range(-r, r + 1):
                q = (i + di, j + dj)
                if q not in sq_set and 0 <= q[0] < size - 1 and 0 <= q[1] < size - 1:
                    ring.add(q)
    # squares wholly enclosed by the hole (terrain islands) go too
    island: set[tuple[int, int]] = set()
    allq = sq_set | ring
    if allq:
        from scipy.ndimage import binary_fill_holes

        qi = [q[0] for q in allq]
        qj = [q[1] for q in allq]
        a0, b0 = min(qi) - 1, min(qj) - 1
        m = np.zeros((max(qj) - b0 + 2, max(qi) - a0 + 2), bool)
        for i, j in allq:
            m[j - b0, i - a0] = True
        for jj_, ii_ in zip(*np.nonzero(binary_fill_holes(m) & ~m)):
            island.add((int(ii_ + a0), int(jj_ + b0)))
    idx = {(int(i), int(j)): k for k, (i, j) in enumerate(zip(SI, SJ))}
    for (i, j), why in [
        (q, "open" if q in open_sq else "inside" if q in kept_corner else "transit") for q in sorted(sq_set)
    ] + [
        (q, "ring") for q in sorted(ring)
    ] + [(q, "island") for q in sorted(island)]:
        py, px = th.square_hole_pixel(i, j, size)
        k = idx.get((i, j))
        was = hole is not None and hole[py, px] > 0
        rec = {"px": px, "py": py, "i": i, "j": j, "x": round(i * pitch, 2), "y": round(j * pitch, 2),
               "reason": why, "already": bool(was)}
        if k is not None and why in ("transit", "inside"):
            rec["s"] = round(float(at_s[k]), 2)
            rec["into_m"] = round(float(deep[k]), 3)
        holes.append(rec)
        if hole is not None:
            hole[py, px] = 255
    nd, nu, nk = int(go_down.sum()), int(go_up.sum()), int(kept.sum())
    n_md = sum(1 for v in vertices if v["action"] == "snap_down_majority")
    n_mu = sum(1 for v in vertices if v["action"] == "snap_up_majority")
    dz_d = [v["dz"] for v in vertices if v["action"].startswith("snap_down")]
    dz_u = [v["dz"] for v in vertices if v["action"].startswith("snap_up")]
    return {
        "objectid": g.get("objectid"),
        "name": g.get("name"),
        "kind": g.get("kind"),
        "open_side": open_side,
        "portal_s": [round(s0, 2), round(s1, 2)],
        "settings": cfg,
        "region": {"i0": int(i0), "j0": int(j0), "i1": int(i1), "j1": int(j1)},
        "band_vertices": int(in_band.sum()),
        "inside_before": int(inside.sum()),
        "snap_down": nd,
        "snap_up": nu,
        "snap_down_majority": n_md,
        "snap_up_majority": n_mu,
        "kept_inside": nk,
        "max_down_m": round(min(dz_d), 3) if dz_d else 0.0,
        "max_up_m": round(max(dz_u), 3) if dz_u else 0.0,
        "holes_transit": sum(1 for h in holes if h["reason"] == "transit"),
        "holes_inside": sum(1 for h in holes if h["reason"] == "inside"),
        "holes_ring": sum(1 for h in holes if h["reason"] == "ring"),
        "holes_island": sum(1 for h in holes if h["reason"] == "island"),
        "holes_open": sum(1 for h in holes if h["reason"] == "open"),
        "open_down": sum(1 for v in vertices if v["action"] == "open_down"),
        "open_roof": sum(1 for v in vertices if v["action"] == "open_roof"),
        "vertices": vertices,
        "holes": holes,
        "_axis": ax,
        "_band": list(zip(I[in_band].tolist(), J[in_band].tolist())),
    }


def summary_line(r: dict) -> str:
    return (
        f"passages {r['name'] or r['objectid']}: {r['inside_before']} of {r['band_vertices']} band vertices in the "
        f"clear space; snapped {r['snap_down']} down + {r['snap_down_majority']} by majority "
        f"(max {r['max_down_m']:+.2f} m), {r['snap_up']} up + {r['snap_up_majority']} by majority "
        f"(max {r['max_up_m']:+.2f} m), "
        f"{r['kept_inside']} left inside; holed {r['holes_transit']} transit + {r['holes_inside']} inside "
        f"+ {r['holes_ring']} ring + {r['holes_island']} island squares; open side: {r['open_down']} down, "
        f"{r['open_roof']} down to the roof, {r['holes_open']} squares holed"
    )


def _clusters(r: dict, gap_m: float = 15.0) -> list[list[float]]:
    """Station ranges of the changes (snaps, holes), split where they are more than ``gap_m`` apart."""
    ss = sorted(v["s"] for v in r["vertices"]) + sorted(h["s"] for h in r["holes"] if "s" in h)
    ss.sort()
    out: list[list[float]] = []
    for s in ss:
        if out and s - out[-1][1] <= gap_m:
            out[-1][1] = s
        else:
            out.append([s, s])
    return out


def plot(r: dict, path_stem: Path, *, pitch: float, scale: int = 16, margin_px: int = 6) -> list[Path]:
    """One map per group of changes (usually one per portal): axis, test band, snapped vertices, holed squares.

    North up. Grey dots: vertices of the test band; blue / red: snapped down / up; orange: left inside (holed
    around); squares: black transit, purple around a vertex left inside, grey the ring.
    """
    from PIL import Image, ImageDraw

    paths = []
    band = r["_band"]
    for k, (sa, sb) in enumerate(_clusters(r)):
        pts = [(v["x"] / pitch, v["y"] / pitch) for v in r["vertices"] if sa - 1e-6 <= v["s"] <= sb + 1e-6]
        hs = [h for h in r["holes"] if "s" not in h or sa - 2 <= h["s"] <= sb + 2]
        pts += [(h["i"] + 0.5, h["j"] + 0.5) for h in hs if "s" in h]
        if not pts:
            continue
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        i0, i1 = int(min(xs)) - margin_px, int(max(xs)) + margin_px + 1
        j0, j1 = int(min(ys)) - margin_px, int(max(ys)) + margin_px + 1
        hs = [h for h in r["holes"] if i0 <= h["i"] <= i1 and j0 <= h["j"] <= j1]
        W, H = (i1 - i0 + 1) * scale, (j1 - j0 + 1) * scale
        leg = 210
        img = Image.new("RGB", (max(W, 420), H + leg), (250, 250, 250))
        dr = ImageDraw.Draw(img)

        def xy(i: float, j: float, i0: int = i0, j1: int = j1) -> tuple[float, float]:
            return (i - i0 + 0.5) * scale, (j1 - j + 0.5) * scale

        colours = {"transit": (25, 25, 25), "inside": (120, 0, 120), "ring": (165, 165, 165), "island": (90, 160, 200),
                   "open": (200, 110, 0)}
        for h in hs:
            a = xy(h["i"], h["j"] + 1)
            b = xy(h["i"] + 1, h["j"])
            dr.rectangle([a, b], fill=colours[h["reason"]], outline=(255, 255, 255))
        for bi, bj in band:
            if i0 <= bi <= i1 and j0 <= bj <= j1:
                p = xy(bi, bj)
                dr.ellipse([p[0] - 1.5, p[1] - 1.5, p[0] + 1.5, p[1] + 1.5], fill=(150, 150, 150))
        ax = r["_axis"]
        dr.line([xy(x / pitch, y / pitch) for x, y in ax[:, :2]], fill=(0, 150, 0), width=2)
        for sp in r["portal_s"]:
            m = int(np.argmin(np.abs(ax[:, 2] - sp)))
            m2 = min(m + 1, len(ax) - 1) if m + 1 < len(ax) else m - 1
            tx, ty = ax[m2, 0] - ax[m, 0], ax[m2, 1] - ax[m, 1]
            L = float(np.hypot(tx, ty)) or 1.0
            nx, ny = -ty / L, tx / L
            hw = ax[m, 6] + 2.0
            px = np.interp(sp, ax[:, 2], ax[:, 0]) / pitch
            py = np.interp(sp, ax[:, 2], ax[:, 1]) / pitch
            dr.line([xy(px - nx * hw, py - ny * hw), xy(px + nx * hw, py + ny * hw)], fill=(0, 150, 0), width=3)
        for v in r["vertices"]:
            if not (i0 <= v["x"] / pitch <= i1 and j0 <= v["y"] / pitch <= j1):
                continue
            c = {"snap_down": (30, 90, 255), "snap_up": (230, 40, 30), "kept_inside": (255, 160, 0),
                 "snap_down_majority": (0, 200, 220), "snap_up_majority": (230, 0, 200),
                 "open_down": (0, 120, 60), "open_roof": (150, 60, 0)}[v["action"]]
            p = xy(v["x"] / pitch, v["y"] / pitch)
            rr = scale * 0.28
            dr.ellipse([p[0] - rr, p[1] - rr, p[0] + rr, p[1] + rr], fill=c, outline=(0, 0, 0))
        y0 = H + 6
        dr.rectangle([0, H, img.width, img.height], fill=(250, 250, 250))
        dr.text((8, y0), f"{r['name']} ({r['objectid']})  s {sa:.1f} - {sb:.1f}  open side: {r['open_side']}  "
                f"1 square = {pitch:g} m, north up", fill=(0, 0, 0))
        items = [("snapped down", (30, 90, 255)), ("snapped up", (230, 40, 30)),
                 ("down by majority", (0, 200, 220)), ("up by majority", (230, 0, 200)), ("left inside", (255, 160, 0)),
                 ("hole: transit", (25, 25, 25)), ("hole: around left inside", (120, 0, 120)),
                 ("hole: ring", (165, 165, 165)), ("hole: island", (90, 160, 200)),
                 ("open side: under deck", (0, 120, 60)), ("open side: to roof", (150, 60, 0)), ("hole: open side", (200, 110, 0)),
                 ("axis / portal", (0, 150, 0)),
                 ("test band (grey dots)", (150, 150, 150))]
        for n, (txt, c) in enumerate(items):
            cx, cy = 8 + (n % 2) * 210, y0 + 20 + (n // 2) * 20
            dr.rectangle([cx, cy, cx + 12, cy + 12], fill=c)
            dr.text((cx + 18, cy), txt, fill=(0, 0, 0))
        out = Path(f"{path_stem}_{k + 1}.png")
        img.save(out)
        paths.append(out)
    return paths


def run(
    proc: Path,
    centerlines: list[dict],
    span_infos: list[dict],
    *,
    elev: np.ndarray,
    hole: np.ndarray | None,
    size: int,
    extent: float,
    defaults: dict,
    max_h: float,
    level_name: str | None,
    write_layer: bool = True,
    plots: bool = True,
    decks: list[dict] | None = None,
) -> dict:
    """Snap and hole every tube; write the portal_snap layer, compose, and write the report."""
    import heightmap_layers as hml

    pitch = extent / size
    work = elev.copy()
    by_oid = {i.get("objectid"): i for i in span_infos}
    tubes = []
    banks = deck_banks(decks)
    for g in centerlines:
        r = tube_passages(
            work, hole, g, by_oid.get(g.get("objectid")) or {}, defaults, size=size, pitch=pitch, banks=banks
        )
        if r is None:
            continue
        print(summary_line(r))
        tubes.append(r)
    changed = np.abs(work - elev) > 1e-6
    if write_layer:
        if changed.any():
            hml.write_replace_layer(proc, LAYER, work, changed.astype(np.float64), max_h=max_h, size=size)
        else:
            hml.drop_layer(proc, LAYER)
        hml.compose(proc, size=size, max_h=max_h, level_name=level_name)
    rows = []
    for r in tubes:
        for v in r["vertices"]:
            rows.append({"tube": r["name"] or r["objectid"], "kind": "vertex", **v})
        for h in r["holes"]:
            rows.append({"tube": r["name"] or r["objectid"], "kind": "hole", "action": "hole_" + h["reason"], **h})
    cols = ["tube", "kind", "action", "px", "py", "x", "y", "s", "off", "z_before", "z_after", "dz",
            "deck_surface", "roof_under", "roof_top", "open_side", "into_m", "already"]
    with (proc / "portal_passages.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    if plots:
        for old in proc.glob("portal_passages_*.png"):
            old.unlink()
        for r in tubes:
            slug = "".join(c if c.isalnum() else "_" for c in str(r["name"] or r["objectid"]))
            plot(r, proc / f"portal_passages_{slug}_{r['objectid']}", pitch=pitch)
    keys = ("band_vertices", "inside_before", "snap_down", "snap_up", "snap_down_majority", "snap_up_majority",
            "kept_inside",
            "holes_transit", "holes_inside", "holes_ring", "holes_island", "open_down", "open_roof", "holes_open")
    out = {
        "layer": LAYER,
        "changed_vertices": int(changed.sum()),
        "totals": {k: sum(r[k] for r in tubes) for k in keys},
        "tubes": [{k: v for k, v in r.items() if not k.startswith("_")} for r in tubes],
    }
    (proc / "portal_passages.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    t = out["totals"]
    print(
        f"portal passages: {t['snap_down'] + t['snap_down_majority']} vertices snapped down "
        f"({t['snap_down_majority']} by majority), {t['snap_up'] + t['snap_up_majority']} up "
        f"({t['snap_up_majority']} by majority), {t['kept_inside']} left inside; open side {t['open_down']} down, "
        f"{t['open_roof']} to the roof; "
        f"{t['holes_transit'] + t['holes_inside'] + t['holes_ring'] + t['holes_island'] + t['holes_open']} squares holed "
        f"-> {proc / 'portal_passages.json'}, portal_passages.csv, portal_passages_*.png"
    )
    return {k: v for k, v in out.items() if k != "tubes"} | {
        "tubes": [{k: r[k] for k in ("objectid", "name") + keys} for r in tubes]
    }


def main() -> None:
    import heightmap_layers as hml
    from PIL import Image
    from site_coords import SiteCoords, load_site, processed_dir

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="report only: no layer, no hole map written")
    ap.add_argument("--no-plot", action="store_true")
    args = ap.parse_args()

    site = load_site()
    bng = site.get("beamng") or {}
    level_name = str(bng.get("level_name") or "").strip() or None
    size = int(bng.get("mask_size") or 512)
    proc = processed_dir(site)
    extent = float(SiteCoords(site).terrain_extent)
    cl = json.loads((proc / "galleries_centerlines.json").read_text(encoding="utf-8"))
    meta = json.loads((proc / "galleries_meta.json").read_text(encoding="utf-8"))
    defaults = dict(meta.get("defaults") or {})
    _dgm, max_h = hml.load_dgm(proc, size)
    # analyse the terrain without the previous snaps
    has_snap = any(x.get("name") == LAYER for x in hml.load_manifest(proc).get("layers") or [])
    if has_snap and args.dry_run:
        tmp = hml.compose(proc, size=size, max_h=max_h, sync_import=False, skip_layers=[LAYER])
        comp = np.asarray(Image.open(tmp), dtype=np.float64) / 65535.0 * max_h
        tmp.unlink()
    else:
        if has_snap:
            hml.drop_layer(proc, LAYER)
            hml.compose(proc, size=size, max_h=max_h, level_name=None, sync_import=False)
        comp, _mh, _src = hml.load_composed_or_dgm(proc, size)
    # start like build_galleries: from the manual hole map or empty, so no hole of an earlier method stays
    hole = np.zeros((size, size), np.uint8)
    manual = proc / "theTerrain_holemap_manual.png"
    if manual.is_file():
        hole = np.asarray(Image.open(manual), dtype=np.uint8).copy()
        if hole.ndim == 3:
            hole = hole[..., 0].copy()
        print(f"holes start from the manual map ({int((hole > 0).sum())} px)")
    decks = []
    items = proc / "galleries_items.level.json"
    if items.is_file():
        for line in items.read_text(encoding="utf-8").splitlines():
            if line.strip():
                e = json.loads(line)
                if e.get("class") == "MeshRoad" and str(e.get("name", "")).startswith("gallery_deck_"):
                    decks.append(e)
    run(
        proc, cl.get("galleries") or [], meta.get("spans") or [], elev=comp, hole=hole, size=size, extent=extent,
        defaults=defaults, max_h=max_h, level_name=level_name, write_layer=not args.dry_run, plots=not args.no_plot,
        decks=decks,
    )
    if not args.dry_run:
        import build_galleries as bg

        bg.write_hole_assets(proc, level_name, hole)


if __name__ == "__main__":
    main()
