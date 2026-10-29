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
Bridge, tunnel and gallery ranges of bridge_ref score nothing; the smoothness carries the line over them.
A dynamic programme (Viterbi) picks the offset path with the least data cost plus a jump cost between
stations, then a Whittaker smoother of the class wavelength (SMOOTH_LC_M) removes the 0.1 m steps.
Confidence per station is how much worse the best offset more than 0.6 m away scores.

Writes data/processed/<site>/lateral_fix/lateral_fix.gpkg (LATFIX_OUT renames it): link (moved geometry),
station, node, chain. Nothing else reads it yet.

Pfunds-Spiss, Swiss part, distance to the middle of the cadastral carriageway (AV LCSF Strasse_Weg),
6194 stations: L median 0.70 -> 0.45 m, p90 1.75 -> 1.15 m, within 1.5 m 86 -> 94 %; B unchanged (0.35-0.40 m).
Rank Gemeinde and intern have no Swiss stations; in Austria they were only looked at on the orthophoto.
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


def viterbi(cost, offs, jump_sigma, lock):
    n, k = cost.shape
    trans = ((offs[:, None] - offs[None, :]) / jump_sigma) ** 2 * 0.5
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


def confidence(cost, offs, chosen):
    out = np.zeros(len(chosen))
    for i, o in enumerate(chosen):
        k = int(np.argmin(np.abs(offs - o)))
        far = np.abs(offs - o) > SECOND_APART_M
        out[i] = float(cost[i, far].min() - cost[i, k]) if far.any() else 0.0
    return out


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
    struct = defaultdict(list)
    for r in refs.itertuples():
        struct[str(r.link_id)].append((min(r.from_pct, r.to_pct) / 100.0, max(r.from_pct, r.to_pct) / 100.0))

    moved: dict = {}                     # node -> new (x, y)
    new_coords: dict[int, np.ndarray] = {}
    st_rows, ch_rows = [], []
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
            mask = np.zeros(len(s), bool)
            for j, (idx, fwd) in enumerate(chain):
                L = links.at[idx, "geometry"].length
                for a, b in struct.get(str(links.at[idx, "link_id"]), []):
                    lo, hi = (a, b) if fwd else (1 - b, 1 - a)
                    mask |= (s >= cuts[j] + lo * L - 1.0) & (s <= cuts[j] + hi * L + 1.0)
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
            newp = pts + normal * off[:, None]
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
                                "structure": int(mask[i]), "geometry": Point(newp[i])})
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
    nd.to_file(dest, layer="node")
    st = gpd.GeoDataFrame(st_rows)
    for name in [RANKS[r][0] for r in sorted(RANKS)]:
        x = st[(st["rank"] == name) & (st.structure == 0)]
        if len(x):
            print(f"{name}: stations {len(x)} |offset| median {x.offset_m.abs().median():.2f} p90 "
                  f"{x.offset_m.abs().quantile(0.9):.2f} m, low confidence {np.mean(x.confidence < 0.5) * 100:.0f} %")
    print(f"(open the following in qgis or similar tool to check: {dest})")


if __name__ == "__main__":
    main()
