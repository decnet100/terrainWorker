"""Merge the own network (Austria) with swissTLM3D and the Swiss cadastral carriageway at the national border.

The border is the BEV national boundary (INSPIRE AU, unit "Staat", EPSG:3416 -> site CRS). Own pieces
(lateral_fix/kunstbaute.gpkg) keep their parts in Austria, swissTLM3D lines (fetch_swisstlm3d_roads.py) and
AV road polygons (sources.av_landcover) their parts outside Austria. On Pfunds-Spiss the BEV line lies 0.4 m
(median, max 0.74 m) from the swissBOUNDARIES3D line.

Where the two networks meet:
* <site>_border.yaml beside the site yaml decides (ids only; all geometry is computed here):
  - join: link, node, tlm - the own link ends at the border, the TLM line goes on. Both ends meet at the point
    where the TLM line crosses the border.
  - replace: drop (own links), use (TLM lines), at (own node) - the TLM lines take over in Austria up to `at`;
    their loose ends there are set onto the node. The own road ends at `at`: no width transition, the TLM
    branches (customs lanes) are separate roads.
  - unite: node + links + tlm, or link + cut_m + tlm - gravel ways: the TLM end is joined to the own node (or to
    the own link end left after cutting cut_m off its border end) by a straight connector.
  - keep_tlm: TLM lines kept whole, also their part in Austria (a bridge or way a unite needs).
* Contacts the file does not name are paired automatically when exactly one own end and one TLM end lie within
  PAIR_M of each other near the border and point the same way (HEADING_DEG); they become joins. Everything else
  is written to <site>_border_proposal.yaml in the output folder, for a decision in the site file.

Carriageway polygons: own ribbons from the lateral_fix stations (half widths), clipped to Austria plus the sliver
up to the Swiss AV edge; AV road polygons (Strasse_Weg, Trottoir, Verkehrsinsel) clipped to outside Austria.
At a join the own half widths go over linearly to the AV half widths measured just inside Switzerland, over
TRANSITION_PER_M x the difference (100 x: 0.2 m -> 20 m), at least TRANSITION_MIN_M. Tunnel and gallery pieces get
no polygon (Magliasina skips them). TLM branches of a replace get a strip of their class width in Austria.

Writes data/processed/<site>/border_merge/border_merge.gpkg (line, node, carriageway, contact) and
<site>_border_proposal.yaml there.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/pfunds_spiess.yaml"; python tools\\merge_border_network.py
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import shapely
import shapely.ops
import yaml
from shapely.geometry import LineString, Point, Polygon

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from gip_routing_load import gpkg_path  # noqa: E402
from site_coords import load_site, processed_dir, resolve_site_path, site_slug  # noqa: E402

BEV_GPKG = ROOT / "data" / "raw" / "borders" / "AT_INSPIRE_BEV_AU_AB_epsg3416_20260402.gpkg"
CONTACT_M = 10.0          # an end this close to the border is a border contact
PAIR_M = 8.0              # an own and a TLM end this close can pair automatically
HEADING_DEG = 30.0        # ... when their directions agree this well
TRANSITION_PER_M = 100.0  # transition length per metre of half-width difference
TRANSITION_MIN_M = 5.0
TRANSITION_MAX_M = 80.0
AV_PROBE_M = 1.0          # the AV width is measured from this far inside Switzerland ...
AV_PROBE_MAX_M = 40.0     # ... at the first cross-section inside the AV carriageway clear of the border
STUB_M = 3.0              # own remnants this short at the border (rest of a Swiss link) are dropped
LOOSE_TOL_M = 0.05        # an end this close to another line is connected, not a loose end
SLIVER_M = 2.0            # own ribbons may fill the gap to the AV edge this far past the BEV line
AV_ROAD = {"Strasse_Weg": "strada_sentiero", "Trottoir": "marciapiede", "Verkehrsinsel": "spartitraffico"}
TLM_WIDTH = {"8m Strasse": 7.5, "6m Strasse": 6.0, "4m Strasse": 3.5, "3m Strasse": 3.0, "2m Weg": 2.0,
             "1m Weg": 1.2, "1m Wegfragment": 1.2, "Markierte Spur": 2.0}
NO_SURFACE = {"Tunnel", "Galerie"}


def austria(site: dict, around) -> shapely.Geometry:
    au = gpd.read_file(BEV_GPKG, layer="AU_flat_view")
    au = au[au["nationalLevelName"] == "Staat"].to_crs(site.get("crs") or "EPSG:25832")
    return shapely.union_all(list(au.geometry)).intersection(around)


def ends_of(geom: LineString):
    c = np.asarray(geom.coords)[:, :2]
    out = []
    for at_start in (True, False):
        p = c[0] if at_start else c[-1]
        q = c[min(3, len(c) - 1)] if at_start else c[max(-4, -len(c))]
        v = p - q                            # pointing out of the line through its end
        n = np.linalg.norm(v)
        out.append((at_start, Point(p), v / n if n > 0 else v))
    return out


def set_end(geom: LineString, at_start: bool, p: Point) -> LineString:
    c = [tuple(x) for x in geom.coords]
    q = (p.x, p.y) + ((c[0][2] if at_start else c[-1][2],) if geom.has_z else ())
    if at_start:
        c[0] = q
    else:
        c[-1] = q
    return LineString(c)


def angle(u, v) -> float:
    return math.degrees(math.acos(float(np.clip(np.dot(u, v), -1, 1))))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tlm", help="swissTLM3D roads (default data/raw/swiss/tlm_strasse_<site>.gpkg)")
    args = ap.parse_args()
    site = load_site()
    slug = site_slug(site)
    proc = processed_dir(site)
    crs = site.get("crs") or "EPSG:25832"
    box = shapely.box(*map(float, site["bbox"]))
    around = box.buffer(3000.0)
    AT = austria(site, around)
    border = AT.boundary.intersection(box.buffer(500.0))
    outside = around.difference(AT)
    shapely.prepare(AT)

    pieces = gpd.read_file(proc / "lateral_fix" / "kunstbaute.gpkg")
    stations = gpd.read_file(proc / "lateral_fix" / "lateral_fix.gpkg", layer="station")
    moved = gpd.read_file(proc / "lateral_fix" / "lateral_fix.gpkg", layer="node").set_index("node_id").geometry
    gnodes = gpd.read_file(gpkg_path(site), layer="node").set_index("node_id").geometry
    tlm_path = Path(args.tlm) if args.tlm else ROOT / "data" / "raw" / "swiss" / f"tlm_strasse_{slug}.gpkg"
    tlm = gpd.read_file(tlm_path).to_crs(crs)
    avsrc = (site.get("sources") or {}).get("av_landcover") or {}
    av = gpd.read_file(ROOT / avsrc["path"], layer=avsrc.get("layer")).to_crs(crs)
    av = av[av[avsrc.get("attribute", "art")].isin(AV_ROAD)]
    av_cls = av[avsrc.get("attribute", "art")].map(AV_ROAD)
    border_file = resolve_site_path().with_name(resolve_site_path().stem + "_border.yaml")
    rules = yaml.safe_load(border_file.read_text(encoding="utf-8")) if border_file.is_file() else {}
    print(f"border {border.length:.0f} m in the box; rules from {border_file.name if rules else 'none'}", flush=True)

    def node_pos(nid: str) -> Point:
        return moved[nid] if nid in moved.index else gnodes[nid]

    # --- decisions of the site file ---------------------------------------------------------------
    dropped_links = set()
    tlm_whole = set()                  # TLM lines kept whole (also their Austrian part)
    named_links, named_tlm = set(), set()
    joins, replaces, unites = [], [], []
    for row in rules.get("join") or []:
        joins.append(row)
        named_links.add(row["link"])
        named_tlm.add(row["tlm"])
    for row in rules.get("replace") or []:
        replaces.append(row)
        dropped_links |= set(row.get("drop") or [])
        tlm_whole |= set(row.get("use") or [])
        named_links |= set(row.get("drop") or [])
        named_tlm |= set(row.get("use") or [])
    for uuid in rules.get("keep_tlm") or []:
        tlm_whole.add(uuid)
        named_tlm.add(uuid)
    for row in rules.get("unite") or []:
        unites.append(row)
        named_links |= set(row.get("links") or []) | ({row["link"]} if row.get("link") else set())
        named_tlm.add(row["tlm"])
        tlm_whole.add(row["tlm"])

    # --- lines ------------------------------------------------------------------------------------
    own = pieces[~pieces["link_id"].isin(dropped_links)].copy()
    own["geometry"] = own.geometry.intersection(AT)
    own = own.explode(index_parts=False)
    own = own[own.geometry.geom_type == "LineString"]
    own = own[own.length > 0.5]
    stub = (own.length < STUB_M) & (own.geometry.distance(border) < 0.05)
    own = own[~stub].reset_index(drop=True)
    print(f"dropped {int(stub.sum())} own remnants under {STUB_M} m at the border", flush=True)
    sw = tlm.copy()
    keep_whole = sw["UUID"].isin(tlm_whole)
    sw.loc[~keep_whole, "geometry"] = sw.loc[~keep_whole].geometry.intersection(outside)
    sw = sw.explode(index_parts=False)
    sw = sw[sw.geometry.geom_type == "LineString"]
    sw = sw[sw.length > 0.5].reset_index(drop=True)
    print(f"own pieces in Austria {len(own)}, swissTLM3D lines outside {len(sw)}", flush=True)

    # --- contacts ---------------------------------------------------------------------------------
    contacts = []
    all_lines = list(own.geometry) + list(sw.geometry)
    tree = shapely.STRtree(all_lines)
    offset = {"own": 0, "tlm": len(own)}

    def loose(p: Point, self_index: int) -> bool:
        """No other line within LOOSE_TOL_M of this end."""
        for j in tree.query(p.buffer(LOOSE_TOL_M)):
            if j != self_index and all_lines[j].distance(p) <= LOOSE_TOL_M:
                return False
        return True

    for src, df, key in (("own", own, "link_id"), ("tlm", sw, "UUID")):
        for i, r in df.iterrows():
            for at_start, p, v in ends_of(r.geometry):
                d = p.distance(border)
                if d <= CONTACT_M and loose(p, offset[src] + i):
                    contacts.append({"src": src, "row": i, "id": r[key], "at_start": at_start, "pt": p, "dir": v, "d": d})
    own_c = [c for c in contacts if c["src"] == "own"]
    tlm_c = [c for c in contacts if c["src"] == "tlm"]

    connectors, nodes, done = [], [], set()

    def tlm_end(uuid: str, near: Point):
        cand = [c for c in tlm_c if c["id"] == uuid]
        if not cand:
            cand = []
            for i, r in sw[sw["UUID"] == uuid].iterrows():
                for at_start, p, v in ends_of(r.geometry):
                    cand.append({"src": "tlm", "row": i, "id": uuid, "at_start": at_start, "pt": p, "dir": v, "d": p.distance(border)})
        return min(cand, key=lambda c: c["pt"].distance(near)) if cand else None

    def own_end(link: str, near: Point):
        cand = [c for c in own_c if c["id"] == link]
        if not cand:
            cand = []
            for i, r in own[own["link_id"] == link].iterrows():
                for at_start, p, v in ends_of(r.geometry):
                    cand.append({"src": "own", "row": i, "id": link, "at_start": at_start, "pt": p, "dir": v, "d": p.distance(border)})
        return min(cand, key=lambda c: c["pt"].distance(near)) if cand else None

    def weld(o, t, kind):
        """Both ends onto the point where the TLM line meets the border (or the TLM end)."""
        p = t["pt"]                          # the TLM line was cut at the border, so its end lies on it
        own.at[o["row"], "geometry"] = set_end(own.at[o["row"], "geometry"], o["at_start"], p)
        gap = o["pt"].distance(p)
        nodes.append({"kind": kind, "own": o["id"], "tlm": t["id"], "moved_m": round(gap, 2), "geometry": p})
        done.update({("own", o["row"], o["at_start"]), ("tlm", t["row"], t["at_start"])})

    for row in joins:
        t = tlm_end(row["tlm"], border.centroid)
        o = own_end(row["link"], t["pt"]) if t else None
        if t and o:
            weld(o, t, "join")
        else:
            print(f"join {row['link'][:8]}: end not found", flush=True)
    for row in replaces:
        at = node_pos(row["at"])
        for uuid in row.get("use") or []:
            for i, r in sw[sw["UUID"] == uuid].iterrows():
                for at_start, p, v in ends_of(r.geometry):
                    if p.distance(at) <= 5.0:
                        sw.at[i, "geometry"] = set_end(sw.at[i, "geometry"], at_start, at)
                        done.add(("tlm", i, at_start))
        nodes.append({"kind": "replace", "own": ",".join(row.get("drop") or []), "tlm": ",".join(row.get("use") or []),
                      "moved_m": None, "geometry": at})
    for row in unites:
        if row.get("link"):
            # cut the own link cut_m back from its border end
            o = own_end(row["link"], border.centroid)
            g = own.at[o["row"], "geometry"]
            L = g.length
            keep = shapely.ops.substring(g, float(row.get("cut_m", 0.0)), L) if o["at_start"] else \
                shapely.ops.substring(g, 0.0, L - float(row.get("cut_m", 0.0)))
            own.at[o["row"], "geometry"] = keep
            target = Point(keep.coords[0] if o["at_start"] else keep.coords[-1])
            done.add(("own", o["row"], o["at_start"]))
        else:
            target = node_pos(row["node"])
            for l in row.get("links") or []:
                for c in own_c:
                    if c["id"] == l and c["pt"].distance(target) < 0.5:
                        done.add(("own", c["row"], c["at_start"]))
        t = tlm_end(row["tlm"], target)
        if t is None:
            print(f"unite {row['tlm']}: TLM end not found", flush=True)
            continue
        if t["pt"].distance(target) > 0.05:
            connectors.append({"src": "connector", "id": f"{row['tlm']}>{row.get('node') or row.get('link')}",
                               "geometry": LineString([t["pt"], target])})
        nodes.append({"kind": "unite", "own": row.get("node") or row.get("link"), "tlm": row["tlm"],
                      "moved_m": round(t["pt"].distance(target), 2), "geometry": target})
        done.add(("tlm", t["row"], t["at_start"]))

    # automatic pairs among contacts the file does not name
    free_o = [c for c in own_c if ("own", c["row"], c["at_start"]) not in done and c["id"] not in named_links]
    free_t = [c for c in tlm_c if ("tlm", c["row"], c["at_start"]) not in done and c["id"] not in named_tlm]
    auto, prop = [], {"auto_join": [], "own_without_partner": [], "tlm_without_partner": [], "ambiguous": []}
    for o in free_o:
        cand = [t for t in free_t if t["pt"].distance(o["pt"]) <= PAIR_M and angle(o["dir"], -t["dir"]) <= HEADING_DEG]
        back = [x for x in free_o if cand and x is not o and cand[0]["pt"].distance(x["pt"]) <= PAIR_M
                and angle(x["dir"], -cand[0]["dir"]) <= HEADING_DEG]
        if len(cand) == 1 and not back:
            auto.append((o, cand[0]))
        elif len(cand) > 1 or back:
            prop["ambiguous"].append({"link": o["id"], "tlm": [t["id"] for t in cand],
                                      "note": f"{o['d']:.1f} m from the border"})
    paired_t = set()
    for o, t in auto:
        weld(o, t, "auto")
        paired_t.add((t["row"], t["at_start"]))
        prop["auto_join"].append({"link": o["id"], "tlm": t["id"],
                                  "note": f"ends {o['pt'].distance(t['pt']):.1f} m apart, heading {angle(o['dir'], -t['dir']):.0f} deg"})
    for o in free_o:
        if ("own", o["row"], o["at_start"]) not in done and not any(a["link"] == o["id"] for a in prop["ambiguous"]):
            r = own.loc[o["row"]]
            prop["own_without_partner"].append({"link": o["id"], "note": f"{r['fc_name']}, {o['d']:.1f} m from the border"})
    for t in free_t:
        if (t["row"], t["at_start"]) not in paired_t:
            r = sw.loc[t["row"]]
            prop["tlm_without_partner"].append({"tlm": t["id"], "note": f"{r['OBJEKTART']}, {t['d']:.1f} m from the border"})

    # --- carriageway polygons ---------------------------------------------------------------------
    av_out = av.copy()
    av_out["geometry"] = av.geometry.intersection(outside)
    av_out["klass"] = av_cls
    av_out = av_out[~av_out.geometry.is_empty]
    av_road = shapely.union_all(list(av_out[av_out["klass"] == "strada_sentiero"].geometry))
    shapely.prepare(av_road)
    own_zone = AT.union(outside.intersection(border.buffer(SLIVER_M)))
    join_pts = [(n["geometry"], n["own"]) for n in nodes if n["kind"] in ("join", "auto")]
    surf, transitions = [], []
    for chain, g in stations.groupby("chain"):
        g = g.sort_values("s").reset_index(drop=True)
        if len(g) < 2:
            continue
        p = np.array([[q.x, q.y] for q in g.geometry])
        t = np.gradient(p, axis=0)
        t /= np.maximum(np.linalg.norm(t, axis=1, keepdims=True), 1e-9)
        n = np.stack([-t[:, 1], t[:, 0]], axis=1)
        hl, hr = g["half_left_m"].to_numpy().copy(), g["half_right_m"].to_numpy().copy()
        tube = g["structure_typ"].isin(["Tunnel", "Galerie", "Tunnel/Galerie"]).to_numpy()
        # transitions towards the AV width at joins on this chain
        in_at = shapely.contains_xy(AT, p[:, 0], p[:, 1])
        for jp, link in join_pts:
            dist = np.hypot(*(p - [jp.x, jp.y]).T)
            k = int(np.argmin(dist))
            if dist[k] > 5.0 or link not in set(g["link_id"]):
                continue
            # which way along the chain is Switzerland
            ahead = in_at[min(k + 3, len(p) - 1)]
            behind = in_at[max(k - 3, 0)]
            to_ch = 1.0 if (not ahead and behind) or (k == 0 and not in_at[0]) else -1.0
            if ahead and behind:
                to_ch = 1.0 if k == len(p) - 1 else -1.0
            # first cross-section inside the AV carriageway, going into Switzerland (the AV ends at the Swiss line,
            # up to ~0.7 m before the BEV line, and the road may cross the border at a slant)
            probe = None
            for step in np.arange(AV_PROBE_M, AV_PROBE_MAX_M + 1e-9, 0.5):
                q = p[k] + t[k] * to_ch * step
                cross = LineString([q - n[k] * 12.0, q + n[k] * 12.0])
                # the whole cross-section must be clear of the border, where the AV polygon is cut at a slant
                if shapely.contains_xy(av_road, q[0], q[1]) and cross.distance(border) > 1.0:
                    probe = q
                    break
            if probe is None:
                print(f"  join {link[:8]}: no AV carriageway within {AV_PROBE_MAX_M} m, no transition", flush=True)
                continue
            want = []
            for side, h in ((1, hl), (-1, hr)):
                tt = np.arange(0.0, 12.0, 0.05)
                xy = probe + side * n[k] * tt[:, None]
                inside = shapely.contains_xy(av_road, xy[:, 0], xy[:, 1])
                run = np.flatnonzero(~inside)
                want.append(tt[run[0]] if len(run) and run[0] > 0 else h[k])
            along = np.abs(g["s"].to_numpy() - g["s"].iloc[k])
            for side_name, h, w_av in (("left", hl, want[0]), ("right", hr, want[1])):
                h_k = float(h[k])
                L = float(np.clip(TRANSITION_PER_M * abs(w_av - h_k), TRANSITION_MIN_M, TRANSITION_MAX_M))
                f = np.clip(1 - along / L, 0, 1)
                h[:] = h + (w_av - h) * f
                transitions.append({"link": link, "side": side_name, "from_m": round(h_k, 2),
                                    "to_av_m": round(float(w_av), 2), "length_m": round(L, 1)})
        left, right = p + n * hl[:, None], p - n * hr[:, None]
        for i in range(len(g) - 1):
            if tube[i] and tube[i + 1]:
                continue
            quad = Polygon([left[i], left[i + 1], right[i + 1], right[i]])
            if quad.is_valid and quad.area > 0:
                surf.append((g["link_id"].iloc[i], quad))
    own_poly = {}
    for link, quad in surf:
        own_poly.setdefault(link, []).append(quad)
    rows = []
    kept_links = set(own["link_id"])
    for link, quads in own_poly.items():
        if link in dropped_links or link not in kept_links:
            continue                      # replaced by TLM branches, or only a remnant at the border
        geom = shapely.union_all(quads).intersection(own_zone).difference(av_road)
        if not geom.is_empty:
            rows.append({"src": "own", "id": link, "klass": "strada_sentiero", "geometry": geom})
    for u in tlm_whole:
        for _, r in sw[sw["UUID"] == u].iterrows():
            w = TLM_WIDTH.get(r["OBJEKTART"], 3.0)
            g = shapely.force_2d(r.geometry).buffer(w / 2, cap_style="flat").intersection(AT)
            if not g.is_empty and r["OBJEKTART"] not in ("2m Weg", "1m Weg", "1m Wegfragment"):
                rows.append({"src": "tlm", "id": u, "klass": "strada_sentiero", "geometry": g})
    for _, r in av_out.iterrows():
        rows.append({"src": "av", "id": "", "klass": r["klass"], "geometry": r.geometry})

    # --- write ------------------------------------------------------------------------------------
    out_dir = proc / "border_merge"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / "border_merge.gpkg"
    lines = [{"src": "own", "id": r["piece_id"], "link_id": r["link_id"], "kunstbaute": r["kunstbaute"],
              "objektart": r["fc_name"], "width_m": r["width_m"], "belagsart": None, "geometry": r.geometry}
             for _, r in own.iterrows()]
    lines += [{"src": "tlm", "id": r["UUID"], "link_id": "", "kunstbaute": r["KUNSTBAUTE"], "objektart": r["OBJEKTART"],
               "width_m": TLM_WIDTH.get(r["OBJEKTART"]), "belagsart": r.get("BELAGSART"),
               "geometry": shapely.force_2d(r.geometry)} for _, r in sw.iterrows()]
    lines += [{"src": "connector", "id": c["id"], "link_id": "", "kunstbaute": "Keine", "objektart": "connector",
               "width_m": None, "belagsart": None, "geometry": c["geometry"]} for c in connectors]
    gpd.GeoDataFrame(lines, crs=crs).to_file(dest, layer="line")
    gpd.GeoDataFrame(nodes, crs=crs).to_file(dest, layer="node")
    gpd.GeoDataFrame(rows, crs=crs).to_file(dest, layer="carriageway")
    gpd.GeoDataFrame([{"src": c["src"], "id": c["id"], "d_border_m": round(c["d"], 2),
                       "handled": int((c["src"], c["row"], c["at_start"]) in done), "geometry": c["pt"]}
                      for c in contacts], crs=crs).to_file(dest, layer="contact")
    gpd.GeoDataFrame(geometry=[border], crs=crs).to_file(dest, layer="border")
    proposal = out_dir / f"{resolve_site_path().stem}_border_proposal.yaml"
    proposal.write_text("# Border contacts the site file does not name. auto_join was applied; the rest needs a\n"
                        "# decision in the site's _border.yaml (or nothing, when a dead end is right).\n"
                        + yaml.safe_dump(prop, sort_keys=False, allow_unicode=True), encoding="utf-8")
    for tr in transitions:
        print(f"  transition {tr['link'][:8]} {tr['side']}: half width {tr['from_m']} -> AV {tr['to_av_m']} m over {tr['length_m']} m")
    print(f"joins {sum(n['kind'] == 'join' for n in nodes)} (file) + {len(auto)} (auto), replace {len(replaces)}, "
          f"unite {len(unites)}; open: own {len(prop['own_without_partner'])}, tlm {len(prop['tlm_without_partner'])}, "
          f"ambiguous {len(prop['ambiguous'])}")
    print(f"(open the following in qgis or similar tool to check: {dest}, {proposal})")


if __name__ == "__main__":
    main()
