"""Lateral correction of the GIP routing links, one road rank after the other (trial).

Ranks: B (and Swiss H) first, then L, then Gemeindeverbindung / Sammelstraße, then internal access roads.
Ways (functional class 10 and above) are not moved here; their ends only follow the nodes they share
with a corrected road.

Each rank is cut into chains. A chain runs through a node when only two links of the rank meet there,
or, at a junction, along the straightest pair with the same street code. Chains of a rank are corrected
longest first. A chain end on a node that a chain of the same or a higher rank has already moved is
held there: the end offset must land within ANCHOR_TOL_M of that node, and the end vertex is set onto it.

Per chain the stations are 2 m apart. At every station every lateral offset within the class search
range (SEARCH_M) is scored on the 0.5 m mixed DGM:
* the central 80 % of a band of the link width, at that offset, should be a plane across
  (RMS of the line fit, capped at RMS_CAP_M) - the eroded edges do not count;
* just outside each band edge the cross slope should break away from the band's (up into a cut or down
  an embankment), averaged over EDGE_M, capped at EDGE_CAP;
* a weak pull towards the GIP line keeps flat valley floors where they are. It is four times as strong
  on B: in the Swiss part of Pfunds-Spiss the GIP B axis already sits within 0.35 m (median) of the cadastral
  carriageway, and a weaker pull moved it away (LATFIX_PRIOR_B overrides the factor).
Structures score nothing; the smoothness carries the line over them. They are set per station
(classify_structures): a hand file beside the site yaml (<site>_structures.yaml, example in config/sites) wins;
else, between ground stations of check_grade on both sides, the DGM against a cubic through those stations:
more than 1 m below is a bridge, more than 2 m above a tunnel or gallery; else the GIP label (bridge_ref).
Galleries open to the valley side can show the road in the DGM and are then missed: set them by hand.
A dynamic programme (Viterbi) picks the offset path with the least data cost plus a jump cost between
stations, then a Whittaker smoother of the class wavelength (SMOOTH_LC_M) removes the 0.1 m steps.
Confidence per station is how much worse the best offset more than 0.6 m away scores.

With the centre fixed, each side's half width is searched the same way (DP over the width, capped jump
price, robust smoother weighted by how clearly the edge shows): the half band should stay on its own cross line and break away at the edge. Where the
found edge shows no knee in the terrain (KNEE_MIN) - asphalt beside a flat verge - that side keeps half the
GIP width; edge_left_seen / edge_right_seen tell which. The carriageway layer is the ribbon of both edges.

Writes data/processed/<site>/lateral_fix/lateral_fix.gpkg (LATFIX_OUT renames it): link (moved geometry),
station, node, chain, carriageway, structures (runs with type, source and DGM minus line). Nothing else
reads it yet.

Pfunds-Spiss, Swiss part, distance to the middle of the cadastral carriageway (AV LCSF Strasse_Weg),
6194 stations: L median 0.70 -> 0.45 m, p90 1.75 -> 1.15 m, within 1.5 m 86 -> 94 %; B unchanged (0.35-0.40 m).
Width, stations more than 25 m from a swissTLM3D structure: B |error| median 1.00 m (GIP) -> 0.89 m, within 1 m
46 -> 55 %; L 0.80 -> 0.75 m, within 1 m 62 -> 63 %. Where the cadastral edge has no terrain knee the DGM cannot
see the edge. Bridges (GIP and unlabelled score_gap_chord gaps) take the approach width: no bridge station is now
narrower than 85 % of the GIP width (12 % were before). Rank Gemeinde and intern have no Swiss stations; in Austria they were only looked at on the
orthophoto.
The confidence is not calibrated yet.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/pfunds_spiess.yaml"; python tools\\fix_lateral_rank.py
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from collections import defaultdict
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
import scipy.sparse as sp
import scipy.sparse.linalg as spla
import shapely
from scipy.ndimage import map_coordinates
from shapely.geometry import LineString, Point

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from gip_routing_load import gpkg_path  # noqa: E402
from site_coords import load_site, processed_dir, site_slug  # noqa: E402

STATION_M = 2.0
OFFSET_STEP_M = 0.1
TANGENT_M = 6.0          # tangent from the line this far before and after the station
RMS_CAP_M = 0.15
EDGE_M = 1.5
EDGE_CAP = 0.6
W_RMS = 1.0 / 0.03       # 3 cm of band RMS costs one unit
W_EDGE = 2.0 / EDGE_CAP  # a full break on one side earns two units
W_PRIOR = 1.0            # cost at the edge of the search range
ANCHOR_TOL_M = 0.3
CORE_M = 1.5             # centre out to here, on each side, sets the cross line of that side
KNEE_M = 1.0             # length of the local slopes either side of a candidate edge
HALF_MIN_M = 1.2         # narrowest half width searched
HALF_MAX_EXTRA_M = 2.5   # widest half width searched: GIP half width + this
WIDTH_JUMP_M = 0.10      # jump sigma of a half width between stations
WIDTH_JUMP_CAP = 3.0     # most a width jump can cost: widenings, lay-bys and junctions stay possible
WIDTH_LC_M = 30.0        # smoothing wavelength of the half widths
KNEE_FULL = 0.4          # slope change at which an edge counts fully in the width smoother
W_UNSEEN = 0.15          # weight of the GIP half width where the edge is not seen
TUKEY_WIDTH_M = 0.6      # a half width further than this off the smooth curve stops counting
ABUT_M = 6.0             # a bridge is widened by this at both ends for the width (wing walls, abutments)
APPROACH_N = 10          # stations either side of a bridge whose median width the deck takes
BRIDGE_DZ_M = -1.0       # DGM this far below the line through the road on both sides: a bridge
TUBE_DZ_M = 2.0          # DGM this far above it: a tunnel or gallery (the DGM is the slope over the roof)
SUPPORT_N = 15           # stations on the ground (check_grade ok) each side that carry that line
SUPPORT_GAP_M = 60.0     # the nearest of them at most this far from the gap
MIN_STRUCT_N = 2         # shorter structure runs are dropped, shorter ground holes inside a structure filled
KNEE_MIN = 0.15          # slope change at a found edge below which the terrain does not show that edge
W_WIDTH_PRIOR = 1.0      # pull of a half width to half the GIP width, at the edge of the range
SECOND_APART_M = 0.6

# rank: name, search range (m), jump sigma per station (m), smoothing wavelength (m), pull to the GIP line
RANKS = {
    1: ("B", 2.0, 0.05, 60.0, W_PRIOR * float(os.environ.get("LATFIX_PRIOR_B", "4"))),
    2: ("L", 4.0, 0.10, 40.0, W_PRIOR),
    3: ("Gemeinde", 6.0, 0.15, 25.0, W_PRIOR),
    4: ("intern", 8.0, 0.20, 20.0, W_PRIOR),
}


def rank_of(row) -> int | None:
    cat = str(row.edge_cat or "").upper()
    fc = int(row.fc) if row.fc == row.fc else 99
    if fc >= 10 and fc != 99:
        return None
    if cat in ("A", "B", "H", "HA", "HB"):
        return 1
    if cat == "L" or fc <= 4:
        return 2
    if fc <= 6:
        return 3
    if fc <= 8 or fc == 99:
        return 4
    return None


class Dgm:
    def __init__(self, path: Path):
        with rasterio.open(path) as src:
            self.z = src.read(1).astype(np.float32)
            self.inv = ~src.transform

    def sample(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        col, row = self.inv * (x, y)
        return map_coordinates(self.z, [np.asarray(row) - 0.5, np.asarray(col) - 0.5], order=1, cval=np.nan)


# --- chains --------------------------------------------------------------------------------------

def _end_dir(geom: LineString, at_start: bool) -> np.ndarray:
    c = np.asarray(geom.coords)[:, :2]
    p = c[0] if at_start else c[-1]
    q = geom.interpolate(min(10.0, geom.length), normalized=False) if at_start else \
        geom.interpolate(max(0.0, geom.length - 10.0))
    v = np.array([q.x, q.y]) - p
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def build_chains(links: gpd.GeoDataFrame) -> list[list[tuple[int, bool]]]:
    """Chains of link indices with their direction (True = as digitised)."""
    at = defaultdict(list)            # node -> [(idx, at_start)]
    for i, r in links.iterrows():
        at[r.node_from].append((i, True))
        at[r.node_to].append((i, False))
    partner: dict[tuple[int, str], int] = {}   # (link, node) -> link that continues
    for node, ends in at.items():
        if len(ends) == 2:
            (a, _), (b, _) = ends
            if a != b:
                partner[(a, node)] = b
                partner[(b, node)] = a
            continue
        best = None
        for x in range(len(ends)):
            for y in range(x + 1, len(ends)):
                (a, sa), (b, sb) = ends[x], ends[y]
                if a == b:
                    continue
                ca, cb = links.at[a, "street_code"], links.at[b, "street_code"]
                if not (isinstance(ca, str) and ca == cb):
                    continue
                cos = float(np.dot(_end_dir(links.at[a, "geometry"], sa), _end_dir(links.at[b, "geometry"], sb)))
                if cos < -0.7 and (best is None or cos < best[0]):
                    best = (cos, a, b)
        if best:
            partner[(best[1], node)] = best[2]
            partner[(best[2], node)] = best[1]
    seen, chains = set(), []
    for start in links.index:
        if start in seen:
            continue
        # walk back to an end of the chain
        cur, came = start, links.at[start, "node_from"]
        guard = 0
        while (cur, came) in partner and guard < 10000:
            nxt = partner[(cur, came)]
            if nxt == start:
                break
            came = links.at[nxt, "node_to"] if links.at[nxt, "node_from"] == came else links.at[nxt, "node_from"]
            cur = nxt
            guard += 1
        # now walk forward from cur, leaving through the node opposite to `came`
        chain = []
        node_in = came
        while cur not in seen:
            seen.add(cur)
            fwd = links.at[cur, "node_from"] == node_in
            chain.append((cur, fwd))
            node_out = links.at[cur, "node_to"] if fwd else links.at[cur, "node_from"]
            if (cur, node_out) not in partner:
                break
            nxt = partner[(cur, node_out)]
            node_in, cur = node_out, nxt
        chains.append(chain)
    return chains


# --- per chain ------------------------------------------------------------------------------------

def chain_line(links, chain):
    coords, owner, cuts = [], [], [0.0]
    for idx, fwd in chain:
        c = np.asarray(links.at[idx, "geometry"].coords)[:, :2]
        if not fwd:
            c = c[::-1]
        if coords:
            c = c[1:]
        coords.extend(c.tolist())
        cuts.append(LineString(coords).length if len(coords) > 1 else 0.0)
    return LineString(coords), cuts


def stations(line: LineString):
    n = max(2, int(round(line.length / STATION_M)) + 1)
    s = np.linspace(0.0, line.length, n)
    pts = np.array([[p.x, p.y] for p in (line.interpolate(v) for v in s)])
    a = np.array([[p.x, p.y] for p in (line.interpolate(max(0.0, v - TANGENT_M)) for v in s)])
    b = np.array([[p.x, p.y] for p in (line.interpolate(min(line.length, v + TANGENT_M)) for v in s)])
    t = b - a
    t /= np.maximum(np.linalg.norm(t, axis=1, keepdims=True), 1e-9)
    normal = np.stack([-t[:, 1], t[:, 0]], axis=1)       # left of travel
    return s, pts, normal


def data_cost(dgm: Dgm, pts, normal, width, search, mask, prior):
    offs = np.arange(-search, search + 1e-9, OFFSET_STEP_M)
    half = width / 2.0
    reach = search + half.max() + EDGE_M + 0.5
    tt = np.arange(-reach, reach + 1e-9, 0.25)
    X = pts[:, 0:1] + normal[:, 0:1] * tt[None, :]
    Y = pts[:, 1:2] + normal[:, 1:2] * tt[None, :]
    Z = dgm.sample(X, Y)
    cost = np.zeros((len(pts), len(offs)))
    for i in range(len(pts)):
        if mask[i] or not np.isfinite(Z[i]).any():
            # no terrain to score (a structure, or the link runs off the DGM): only the pull to the GIP line,
            # so equal costs do not send the path to the edge of the search range
            cost[i] = prior * (offs / search) ** 2
            continue
        z, h = Z[i], half[i]
        for k, o in enumerate(offs):
            inner = np.abs(tt - o) <= 0.8 * h
            zz, t0 = z[inner], tt[inner]
            ok = np.isfinite(zz)
            if ok.sum() < 6:
                cost[i, k] = W_RMS * RMS_CAP_M
                continue
            A = np.vstack([t0[ok], np.ones(ok.sum())]).T
            coef, *_ = np.linalg.lstsq(A, zz[ok], rcond=None)
            rms = math.sqrt(float(np.mean((A @ coef - zz[ok]) ** 2)))
            slope = coef[0]
            brk = 0.0
            for side in (-1, 1):
                e = o + side * h
                sel = (side * (tt - e) >= 0) & (side * (tt - e) <= EDGE_M)
                zs, ts = z[sel], tt[sel]
                if np.isfinite(zs).sum() >= 3:
                    so = np.polyfit(ts[np.isfinite(zs)], zs[np.isfinite(zs)], 1)[0]
                    brk += min(abs(so - slope), EDGE_CAP)
            cost[i, k] = W_RMS * min(rms, RMS_CAP_M) - W_EDGE * brk + prior * (o / search) ** 2
        cost[i] -= cost[i].min()
    return offs, cost


def viterbi(cost, offs, jump_sigma, lock, jump_cap=None):
    n, k = cost.shape
    trans = ((offs[:, None] - offs[None, :]) / jump_sigma) ** 2 * 0.5
    if jump_cap is not None:
        trans = np.minimum(trans, jump_cap)   # a real change of width is allowed at a fixed price
    big = 1e9
    acc = cost[0].copy()
    if lock[0] is not None:
        acc[np.abs(offs - lock[0]) > ANCHOR_TOL_M] = big
    back = np.zeros((n, k), dtype=np.int32)
    for i in range(1, n):
        m = acc[None, :] + trans            # [to, from]
        back[i] = np.argmin(m, axis=1)
        acc = m[np.arange(k), back[i]] + cost[i]
    if lock[1] is not None:
        acc[np.abs(offs - lock[1]) > ANCHOR_TOL_M] = big
    path = np.zeros(n, dtype=np.int32)
    path[-1] = int(np.argmin(acc))
    for i in range(n - 1, 0, -1):
        path[i - 1] = back[i, path[i]]
    return offs[path]


def whittaker(y, w, lc, lock):
    n = len(y)
    if n < 4:
        return y
    lam = (lc / (2 * math.pi * STATION_M)) ** 4
    W = w.copy()
    z0 = y.copy()
    for j, v in ((0, lock[0]), (n - 1, lock[1])):
        if v is not None:
            W[j] = 1e6
            z0[j] = v
    D = sp.diags([1.0, -2.0, 1.0], [0, 1, 2], shape=(n - 2, n))
    A = sp.diags(W) + lam * (D.T @ D)
    return spla.spsolve(A.tocsc(), W * z0)


def side_width_cost(dgm: Dgm, pts, normal, off, half_gip, mask, side, prior):
    """Cost of every half width on one side (+1 left, -1 right), with the centre fixed at `off`.

    A line is fitted over the core of this half of the band (centre out to CORE_M). Going out from the core,
    the profile should stay on that line up to the edge (RMS of the residual) and break away at it
    (local slope over KNEE_M just inside against just outside). An eroded edge rounds the knee, so the edge tends to sit at its inner start.
    """
    hs = np.arange(HALF_MIN_M, HALF_MAX_EXTRA_M + float(np.max(half_gip)) + 1e-9, OFFSET_STEP_M)
    reach = float(np.max(np.abs(off))) + hs[-1] + EDGE_M + 0.5
    tt = np.arange(-reach, reach + 1e-9, 0.25)
    X = pts[:, 0:1] + normal[:, 0:1] * tt[None, :]
    Y = pts[:, 1:2] + normal[:, 1:2] * tt[None, :]
    Z = dgm.sample(X, Y)
    span = hs[-1] - hs[0]
    cost = np.zeros((len(pts), len(hs)))
    knee = np.zeros((len(pts), len(hs)))
    for i in range(len(pts)):
        pr = prior * ((hs - half_gip[i]) / span) ** 2
        z = Z[i]
        if mask[i] or not np.isfinite(z).any():
            cost[i] = pr
            continue
        u = side * (tt - off[i])                      # distance from the centre towards this side
        # one line per side: a crowned two-lane road breaks at the centre, a line across both halves would not fit
        core = (u >= -0.25) & (u <= CORE_M) & np.isfinite(z)
        if core.sum() < 5:
            cost[i] = pr
            continue
        b, a = np.polyfit(tt[core], z[core], 1)
        res = z - (a + b * tt)
        for k, h in enumerate(hs):
            inside = (u > CORE_M) & (u <= h) & np.isfinite(z)
            rms = math.sqrt(float(np.mean(res[inside] ** 2))) if inside.sum() >= 2 else 0.0
            # the knee itself: local slope just inside against just outside, so the edge sits on the break
            before = (u >= h - KNEE_M) & (u <= h) & np.isfinite(z)
            beyond = (u >= h) & (u <= h + KNEE_M) & np.isfinite(z)
            brk = 0.0
            if beyond.sum() >= 3 and before.sum() >= 3:
                s_in = np.polyfit(tt[before], z[before], 1)[0]
                s_out = np.polyfit(tt[beyond], z[beyond], 1)[0]
                brk = min(abs(s_out - s_in), EDGE_CAP)
            knee[i, k] = brk
            cost[i, k] = W_RMS * min(rms, RMS_CAP_M) - W_EDGE * brk + pr[k]
        cost[i] -= cost[i].min()
    return hs, cost, knee


def measure_widths(dgm, pts, normal, off, half_gip, mask, bridge, prior):
    """Left and right half width per station.

    DP over the width (capped jump price), then a robust smoother of WIDTH_LC_M: each station weighs by how
    clearly the terrain shows its edge, and Tukey weights drop the stations that stay far off the curve.
    Bridges (bridge, already widened by ABUT_M over the abutments) take the width of their approaches,
    never less than the GIP width: the terrain under a deck says nothing about the deck.
    """
    out, src = [], []
    for side in (1, -1):
        hs, cost, knee = side_width_cost(dgm, pts, normal, off, half_gip, mask, side, prior)
        path = viterbi(cost, hs, WIDTH_JUMP_M, (None, None), jump_cap=WIDTH_JUMP_CAP)
        k = np.clip(np.searchsorted(hs, path), 0, len(hs) - 1)
        kv = knee[np.arange(len(path)), k]
        seen = (kv >= KNEE_MIN) & ~mask
        # an edge the terrain does not show (asphalt beside a flat verge) keeps half the GIP width
        h = np.where(seen, path, half_gip)
        w0 = np.where(mask, 0.0, np.where(seen, np.clip(kv / KNEE_FULL, 0.25, 1.0), W_UNSEEN))
        w = w0.copy()
        for _ in range(3):
            fit = whittaker(h, w, WIDTH_LC_M, (None, None))
            r = np.abs(h - fit) / TUKEY_WIDTH_M
            w = w0 * np.where(r < 1.0, (1 - r ** 2) ** 2, 0.0)
        fit = whittaker(h, np.maximum(w, 1e-6 * (w0 > 0)), WIDTH_LC_M, (None, None))
        out.append(_bridge_fill(fit, bridge, mask, half_gip))
        src.append(seen)
    return out[0], out[1], src[0], src[1]


def _runs(flag):
    idx = np.flatnonzero(flag)
    return np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1) if len(idx) else []


def _bridge_fill(h, bridge, mask, half_gip):
    """Width across each bridge run: straight between the approach medians, at least the GIP half width."""
    h = h.copy()
    n = len(h)
    for run in _runs(bridge):
        a, b = run[0], run[-1]
        before = [i for i in range(max(0, a - APPROACH_N), a) if not mask[i]]
        after = [i for i in range(b + 1, min(n, b + 1 + APPROACH_N)) if not mask[i]]
        v0 = np.median(h[before]) if before else np.nan
        v1 = np.median(h[after]) if after else np.nan
        if np.isnan(v0) and np.isnan(v1):
            v0 = v1 = float(np.median(half_gip[run]))
        v0 = v1 if np.isnan(v0) else v0
        v1 = v0 if np.isnan(v1) else v1
        t = np.linspace(0.0, 1.0, len(run))
        h[run] = np.maximum(v0 + (v1 - v0) * t, half_gip[run])
    return h


def confidence(cost, offs, chosen):
    out = np.zeros(len(chosen))
    for i, o in enumerate(chosen):
        k = int(np.argmin(np.abs(offs - o)))
        far = np.abs(offs - o) > SECOND_APART_M
        out[i] = float(cost[i, far].min() - cost[i, k]) if far.any() else 0.0
    return out


TYP_NAME = {"bridge": "Brücke", "tunnel": "Tunnel", "gallery": "Galerie", "tube": "Tunnel/Galerie", "": ""}


def load_manual_structures(site_path: Path) -> dict[str, list[tuple[float, float, str]]]:
    """Hand assignments beside the site yaml.

    <site>_structures.yaml:
        structures:
          - link_id: <GIP link id>
            from_pct: 10        # optional, along the link as digitised; whole link without
            to_pct: 40
            typ: tunnel         # bridge | tunnel | gallery | ground
    <site>_manual_invalid.txt (score_check_grade's list) counts as whole-link bridges.
    """
    import yaml

    out: dict[str, list[tuple[float, float, str]]] = defaultdict(list)
    path = site_path.with_name(site_path.stem + "_structures.yaml")
    if path.is_file():
        for row in (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("structures") or []:
            typ = str(row.get("typ") or "").strip().lower()
            if typ not in ("bridge", "tunnel", "gallery", "ground"):
                raise SystemExit(f"{path.name}: typ must be bridge, tunnel, gallery or ground, got {typ!r}")
            a = float(row.get("from_pct", 0.0)) / 100.0
            b = float(row.get("to_pct", 100.0)) / 100.0
            out[str(row["link_id"])].append((min(a, b), max(a, b), typ))
        print(f"manual structures {sum(len(v) for v in out.values())} from {path.name}", flush=True)
    old = site_path.with_name(site_path.stem + "_manual_invalid.txt")
    if old.is_file():
        for line in old.read_text(encoding="utf-8").splitlines():
            lid = line.split("#", 1)[0].strip()
            if lid:
                out[lid].append((0.0, 1.0, "bridge"))
    return out


def load_ground(proc: Path) -> dict[str, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]]:
    """check_grade intervals per link: s0, s1 (along the link as digitised), ok, recovered."""
    path = proc / "link_surface" / "check_grade" / "check_grade.gpkg"
    if not path.is_file():
        print(f"no {path.name}: structures come from GIP and the manual file only", flush=True)
        return {}
    iv = gpd.read_file(path, layer="interval", columns=["link_id", "s0_m", "s1_m", "ok", "recovered"], ignore_geometry=True)
    out = {}
    for lid, g in iv.groupby("link_id"):
        g = g.sort_values("s0_m")
        out[str(lid)] = (g.s0_m.to_numpy(), g.s1_m.to_numpy(), g.ok.to_numpy() == 1, g.recovered.to_numpy() == 1)
    return out


def classify_structures(dgm, s, pts, chain, cuts, links, gip, manual, ground):
    """Structure type per station: manual > terrain against the line through the road > GIP label.

    Stations check_grade found on the ground (ok) are road on the ground, except inside a GIP label, where only
    the intervals check_grade recovered onto its spline count. Between two runs of ground stations, a cubic
    through up to SUPPORT_N of them on each side is the road; the DGM below it by more than BRIDGE_DZ_M is
    a bridge, above it by more than TUBE_DZ_M a tunnel or gallery (which one: from the GIP label or the manual
    file; else "tube"). A gap with ground on one side only keeps the GIP label.
    """
    n = len(s)
    gip_typ = np.array([""] * n, dtype=object)
    man_typ = np.array([None] * n, dtype=object)
    ok = np.zeros(n, bool)
    rec = np.zeros(n, bool)
    known = np.zeros(n, bool)
    for j, (idx, fwd) in enumerate(chain):
        L = links.at[idx, "geometry"].length
        lid = str(links.at[idx, "link_id"])
        on_link = (s >= cuts[j] - 1e-6) & (s <= cuts[j + 1] + 1e-6)
        sl = np.where(fwd, s - cuts[j], L - (s - cuts[j]))           # station along the link as digitised
        for a, b, typ in gip.get(lid, []):
            gip_typ[on_link & (sl >= a * L - 1.0) & (sl <= b * L + 1.0)] = typ
        for a, b, typ in manual.get(lid, []):
            man_typ[on_link & (sl >= a * L) & (sl <= b * L)] = typ
        if lid in ground:
            s0, s1, gok, grec = ground[lid]
            k = np.clip(np.searchsorted(s0, sl, side="right") - 1, 0, len(s0) - 1)
            hit = on_link & (sl >= s0[k] - 1e-6) & (sl <= s1[k] + 1e-6)
            ok[hit] = gok[k[hit]]
            rec[hit] = grec[k[hit]]
            known |= hit
    z = dgm.sample(pts[:, 0], pts[:, 1])
    support = ok & np.isfinite(z) & ((gip_typ == "") | rec)
    typ = np.where(support, "", gip_typ).astype(object)
    src = np.where(support, "dgm", np.where(gip_typ != "", "gip", "")).astype(object)
    dz = np.full(n, np.nan)
    for run in _runs(~support & known):
        a, b = run[0], run[-1]
        left = np.flatnonzero(support[:a])[-SUPPORT_N:]
        right = a + 1 + np.flatnonzero(support[a + 1:])
        right = right[right > b][:SUPPORT_N]
        if len(left) < 4 or len(right) < 4 or s[a] - s[left[-1]] > SUPPORT_GAP_M or s[right[0]] - s[b] > SUPPORT_GAP_M:
            continue
        knots = np.concatenate([left, right])
        x0 = s[knots].mean()
        coef = np.polyfit(s[knots] - x0, z[knots], 3 if len(knots) >= 8 else 2)
        d = z[run] - np.polyval(coef, s[run] - x0)
        dz[run] = d
        t = np.where(d < BRIDGE_DZ_M, "bridge", np.where(d > TUBE_DZ_M, "tube", ""))
        # a tube inside a GIP tunnel or gallery keeps that name
        g = gip_typ[run]
        t = np.where((t == "tube") & np.isin(g, ["tunnel", "gallery"]), g, t)
        typ[run] = t
        src[run] = "dgm"
    # tidy: fill short ground holes inside a structure, drop very short structures
    for kind in ("bridge", "tube", "tunnel", "gallery"):
        for hole in _runs(typ == ""):
            if len(hole) < MIN_STRUCT_N + 1 and hole[0] > 0 and hole[-1] < n - 1 \
                    and typ[hole[0] - 1] == kind and typ[hole[-1] + 1] == kind:
                typ[hole] = kind
        for r in _runs(typ == kind):
            if len(r) < MIN_STRUCT_N:
                typ[r] = ""
    has_man = np.array([m is not None for m in man_typ])
    typ[has_man] = np.where(man_typ[has_man] == "ground", "", man_typ[has_man])
    src[has_man] = "manual"
    src[typ == ""] = np.where(src[typ == ""] == "manual", "manual", "")
    return typ, src, dz


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dgm", help="0.5 m DGM in the site CRS (default data/raw/dgm_<site>_mixed05.tif)")
    args = ap.parse_args()
    site = load_site()
    proc = processed_dir(site)
    dgm_path = Path(args.dgm) if args.dgm else ROOT / "data" / "raw" / f"dgm_{site_slug(site)}_mixed05.tif"
    dgm = Dgm(dgm_path)
    print(f"DGM {dgm_path.name} {dgm.z.shape[1]}x{dgm.z.shape[0]}", flush=True)

    gp = gpkg_path(site)
    links = gpd.read_file(gp, layer="link")
    nodes = gpd.read_file(gp, layer="node").set_index("node_id")
    refs = gpd.read_file(gp, layer="bridge_ref")
    # node ids per link from the end coordinates
    npos = {nid: (g.x, g.y) for nid, g in nodes.geometry.items()}
    tree = shapely.STRtree(list(nodes.geometry))
    ids = list(nodes.index)

    def nearest_node(x, y):
        return ids[int(tree.nearest(Point(x, y)))]

    links["node_from"] = [nearest_node(*g.coords[0][:2]) for g in links.geometry]
    links["node_to"] = [nearest_node(*g.coords[-1][:2]) for g in links.geometry]
    links["rank"] = [rank_of(r) for r in links.itertuples()]
    gip_struct: dict[str, list[tuple[float, float, str]]] = defaultdict(list)
    typ_of = {"Brücke": "bridge", "Tunnel": "tunnel", "Galerie": "gallery"}
    for r in refs.itertuples():
        gip_struct[str(r.link_id)].append((min(r.from_pct, r.to_pct) / 100.0, max(r.from_pct, r.to_pct) / 100.0,
                                           typ_of.get(str(r.typ or ""), "tube")))
    from site_coords import resolve_site_path

    manual = load_manual_structures(resolve_site_path())
    ground = load_ground(proc)
    struct_rows = []

    moved: dict = {}                     # node -> new (x, y)
    new_coords: dict[int, np.ndarray] = {}
    st_rows, ch_rows, surf_rows = [], [], []
    for rank in sorted(RANKS):
        name, search, jump, lc, prior = RANKS[rank]
        sub = links[links["rank"] == rank]
        if sub.empty:
            continue
        chains = build_chains(sub)
        chains.sort(key=lambda c: -sum(links.at[i, "geometry"].length for i, _ in c))
        print(f"rank {name}: {len(sub)} links, {len(chains)} chains", flush=True)
        for ci, chain in enumerate(chains):
            line, cuts = chain_line(links, chain)
            if line.length < 2 * STATION_M:
                continue
            s, pts, normal = stations(line)
            li = np.clip(np.searchsorted(cuts, s, side="right") - 1, 0, len(chain) - 1)
            width = np.array([links.at[chain[j][0], "width_m"] for j in li], dtype=float)
            width = np.where(np.isfinite(width), width, 5.0)
            styp, ssrc, sdz = classify_structures(dgm, s, pts, chain, cuts, links, gip_struct, manual, ground)
            mask = styp != ""
            # widths: every structure, widened over its abutments / portals, takes its approaches' width
            grow = int(round(ABUT_M / STATION_M))
            struct_w = mask.copy()
            for d in range(1, grow + 1):
                struct_w[d:] |= mask[:-d]
                struct_w[:-d] |= mask[d:]
            for run in _runs(mask):
                kinds = [k for k in styp[run] if k]
                kind = max(set(kinds), key=kinds.count)
                d = sdz[run]
                struct_rows.append({"rank": name, "chain": f"{rank}-{ci}", "typ": TYP_NAME[kind],
                                    "src": ",".join(sorted(set(ssrc[run]) - {""})),
                                    "length_m": round(float(s[run[-1]] - s[run[0]]) + STATION_M, 1),
                                    "dz_min_m": round(float(np.nanmin(d)), 1) if np.isfinite(d).any() else None,
                                    "dz_median_m": round(float(np.nanmedian(d)), 1) if np.isfinite(d).any() else None,
                                    "dz_max_m": round(float(np.nanmax(d)), 1) if np.isfinite(d).any() else None,
                                    "link_ids": ",".join(dict.fromkeys(str(links.at[chain[li[i]][0], "link_id"]) for i in run)),
                                    "geometry": LineString(pts[run]) if len(run) > 1 else Point(pts[run[0]]).buffer(1.0)})
            first, last = chain[0], chain[-1]
            n_start = links.at[first[0], "node_from"] if first[1] else links.at[first[0], "node_to"]
            n_end = links.at[last[0], "node_to"] if last[1] else links.at[last[0], "node_from"]
            lock = [None, None]
            for j, (nid, i) in enumerate(((n_start, 0), (n_end, len(s) - 1))):
                if nid in moved:
                    d = np.array(moved[nid]) - np.array(npos[nid])
                    lock[j] = float(np.clip(d @ normal[i], -search, search))
            offs, cost = data_cost(dgm, pts, normal, width, search, mask, prior)
            path = viterbi(cost, offs, jump, lock)
            w = np.where(mask, 0.0, 1.0)
            off = whittaker(path, w, lc, lock)
            conf = confidence(cost, offs, off)
            hl, hr, seen_l, seen_r = measure_widths(dgm, pts, normal, off, width / 2.0, mask | struct_w, struct_w,
                                                   W_WIDTH_PRIOR)
            newp = pts + normal * off[:, None]
            left = pts + normal * (off + hl)[:, None]
            right = pts + normal * (off - hr)[:, None]
            for nid, i in ((n_start, 0), (n_end, len(s) - 1)):
                if nid in moved:
                    newp[i] = moved[nid]
                else:
                    moved[nid] = tuple(newp[i])
            # inner chain nodes move with the chain
            for j in range(1, len(chain)):
                idx, fwd = chain[j]
                nid = links.at[idx, "node_from"] if fwd else links.at[idx, "node_to"]
                i = int(np.argmin(np.abs(s - cuts[j])))
                moved.setdefault(nid, tuple(newp[i]))
            for j, (idx, fwd) in enumerate(chain):
                sel = (s >= cuts[j] - 1e-6) & (s <= cuts[j + 1] + 1e-6)
                c = newp[sel]
                if not fwd:
                    c = c[::-1]
                a_node = links.at[idx, "node_from"]
                b_node = links.at[idx, "node_to"]
                c = np.vstack([moved.get(a_node, c[0]), c[1:-1], moved.get(b_node, c[-1])]) if len(c) >= 2 else c
                new_coords[idx] = c
            for i in range(len(s)):
                st_rows.append({"rank": name, "chain": f"{rank}-{ci}", "link_id": links.at[chain[li[i]][0], "link_id"],
                                "s": round(float(s[i]), 1), "offset_m": round(float(off[i]), 2),
                                "dp_offset_m": round(float(path[i]), 2), "confidence": round(float(conf[i]), 2),
                                "width_gip_m": round(float(width[i]), 2), "half_left_m": round(float(hl[i]), 2),
                                "half_right_m": round(float(hr[i]), 2), "width_m": round(float(hl[i] + hr[i]), 2),
                                "edge_left_seen": int(seen_l[i]), "edge_right_seen": int(seen_r[i]),
                                "structure": int(mask[i]), "structure_typ": TYP_NAME[styp[i]], "structure_src": ssrc[i],
                                "dz_m": None if not np.isfinite(sdz[i]) else round(float(sdz[i]), 2),
                                "geometry": Point(newp[i])})
            ring = np.vstack([left, right[::-1], left[:1]])
            surf = shapely.make_valid(shapely.Polygon(ring))
            surf_rows.append({"rank": name, "chain": f"{rank}-{ci}",
                              "width_median_m": round(float(np.median((hl + hr)[~mask])) if (~mask).any() else float("nan"), 2),
                              "width_gip_m": round(float(np.median(width)), 2), "geometry": surf})
            ch_rows.append({"rank": name, "chain": f"{rank}-{ci}", "n_link": len(chain),
                            "length_m": round(line.length, 1),
                            "mean_offset_m": round(float(np.mean(off[~mask])) if (~mask).any() else 0.0, 2),
                            "max_abs_offset_m": round(float(np.max(np.abs(off))), 2),
                            "low_conf_share": round(float(np.mean(conf[~mask] < 0.5)) if (~mask).any() else 1.0, 2),
                            "anchored_ends": int(lock[0] is not None) + int(lock[1] is not None),
                            "geometry": LineString(newp)})
    # links that were not corrected: ends follow their nodes, tapered over 30 m
    out_geom = []
    for idx, r in links.iterrows():
        if idx in new_coords and len(new_coords[idx]) >= 2:
            out_geom.append(LineString(new_coords[idx]).simplify(0.05))
            continue
        c = np.asarray(r.geometry.coords)[:, :2].copy()
        cum = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(c, axis=0).T))])
        for nid, at_start in ((r.node_from, True), (r.node_to, False)):
            if nid in moved:
                d = np.array(moved[nid]) - np.array(npos[nid])
                dist = cum if at_start else cum[-1] - cum
                c += d[None, :] * np.clip(1 - dist / 30.0, 0, 1)[:, None]
        out_geom.append(LineString(c))
    out = links.drop(columns=["geometry"]).copy()
    out["corrected"] = [int(i in new_coords) for i in links.index]
    out = gpd.GeoDataFrame(out, geometry=out_geom, crs=links.crs)
    nd = gpd.GeoDataFrame({"node_id": list(moved), "dx": [moved[n][0] - npos[n][0] for n in moved],
                           "dy": [moved[n][1] - npos[n][1] for n in moved]},
                          geometry=[Point(moved[n]) for n in moved], crs=links.crs)
    dest = proc / "lateral_fix" / os.environ.get("LATFIX_OUT", "lateral_fix.gpkg")
    dest.parent.mkdir(parents=True, exist_ok=True)
    out.to_file(dest, layer="link")
    gpd.GeoDataFrame(st_rows, crs=links.crs).to_file(dest, layer="station")
    gpd.GeoDataFrame(ch_rows, crs=links.crs).to_file(dest, layer="chain")
    gpd.GeoDataFrame(surf_rows, crs=links.crs).to_file(dest, layer="carriageway")
    sg = gpd.GeoDataFrame(struct_rows, crs=links.crs)
    sg.to_file(dest, layer="structures")
    print("structures:", sg.groupby(["typ", "src"]).size().to_dict() if len(sg) else 0)
    nd.to_file(dest, layer="node")
    st = gpd.GeoDataFrame(st_rows)
    for name in [RANKS[r][0] for r in sorted(RANKS)]:
        x = st[(st["rank"] == name) & (st.structure == 0)]
        if len(x):
            print(f"{name}: stations {len(x)} |offset| median {x.offset_m.abs().median():.2f} p90 "
                  f"{x.offset_m.abs().quantile(0.9):.2f} m; width median {x.width_m.median():.2f} m "
                  f"(GIP {x.width_gip_m.median():.2f}), p10-p90 {x.width_m.quantile(0.1):.2f}-{x.width_m.quantile(0.9):.2f} m")
    print(f"(open the following in qgis or similar tool to check: {dest})")


if __name__ == "__main__":
    main()
