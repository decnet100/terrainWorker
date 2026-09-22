"""Detect likely missing GIP bridges from longitudinal terrain dips on named roads.

Problem (Imst and similar):
  Some bridge OBJECTIDs exist as Verkehrswege polylines but are not marked as
  Kunstbau (no KUNSTBAUTEN / OBJEKTBEZEICHNUNG "Brücke"). Then the road band
  follows the DGM notch → a 1–2 m "graben" in the driving surface.

This diagnostic scans **named** GIP corridors (STR_CODE set) and finds short,
deep dips that look like a bridge span. For each candidate it:
  - estimates a gap window (s_g0..s_g1)
  - runs the existing 5-line abutment heuristic (road_span_profile.find_abutments)
    to propose abutment stationing (abutment_s)
  - maps the dip to the most likely GIP OBJECTID (nearest member polyline)
  - writes a JSON report + YAML snippets:
      * beamng.bridges.gip_extra (to label the OBJECTID as a bridge)
      * optional beamng.bridges.items[].abutment_s (to pin the abutments)
  - is conservative: candidates that map onto tunnel/gallery/other structures
    *or sit next to an existing Kunstbau* are not suggested as bridges, but
    are still written out with a reason so they can be adopted intentionally.
    Classification uses both ``KUNSTBAUTEN`` and GIP ``OBJEKT`` (S-AT / S-AB…).

Run (PowerShell, single line):
  cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/<site>.yaml"; python tools\\diag_missing_bridges.py
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from site_coords import load_site, processed_dir  # noqa: E402


def _load_heightmap_z(proc: Path, *, size: int) -> tuple[callable, str, float]:
    """Return (z_at(bx,by), label, extent_m).

    Prefers the latest composed heightmap if present; falls back to pristine.
    """
    try:
        from PIL import Image
    except ImportError as e:  # pragma: no cover
        raise SystemExit(f"Missing PIL: {e}") from e

    meta_path = proc / "heightmap_meta.json"
    if not meta_path.is_file():
        raise SystemExit(f"Missing {meta_path} (run build_smoke first)")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    max_h = float(meta.get("max_height_m") or 0.0)
    if max_h <= 0:
        raise SystemExit(f"heightmap_meta max_height_m invalid: {max_h}")
    extent = float(meta.get("terrain_extent_m") or size)

    # Keep it robust when the user already has extra layers; this is a *diagnostic*,
    # so using the current composed terrain is often closer to what they feel in-game.
    candidates = [
        proc / f"heightmap_{size}_composed.png",
        proc / f"heightmap_{size}_road_bed.png",
        proc / f"heightmap_{size}_bridge_conform.png",
        proc / f"heightmap_{size}.png",
    ]
    hm_path = next((p for p in candidates if p.is_file()), None)
    if hm_path is None:
        raise SystemExit(
            f"No processed heightmap found. Tried: {', '.join(p.name for p in candidates)}"
        )

    hm = np.asarray(Image.open(hm_path))
    n = int(hm.shape[0])
    if n < 2:
        raise SystemExit(f"heightmap too small: {hm_path} shape={hm.shape}")

    def z_at(bx: float, by: float) -> float:
        # Bilinear in pixel space; BeamNG origin at lower-left in our convention.
        px = max(0.0, min(float(n - 1), float(bx) / extent * (n - 1)))
        py = max(0.0, min(float(n - 1), (1.0 - float(by) / extent) * (n - 1)))
        x0 = int(math.floor(px))
        y0 = int(math.floor(py))
        x1 = min(x0 + 1, n - 1)
        y1 = min(y0 + 1, n - 1)
        tx = px - x0
        ty = py - y0
        v = (
            float(hm[y0, x0]) * (1.0 - tx) * (1.0 - ty)
            + float(hm[y0, x1]) * tx * (1.0 - ty)
            + float(hm[y1, x0]) * (1.0 - tx) * ty
            + float(hm[y1, x1]) * tx * ty
        )
        return (v / 65535.0) * max_h

    return z_at, hm_path.name, extent


def _rolling_max(z: np.ndarray, win: int) -> np.ndarray:
    out = np.empty_like(z, dtype=float)
    for i in range(len(z)):
        j0 = max(0, i - win + 1)
        out[i] = float(np.max(z[j0 : i + 1]))
    return out


def _unstable_mask(z: np.ndarray, *, dip_m: float, win: int) -> np.ndarray:
    """True where terrain is `dip_m` below a rolling max baseline (both directions)."""
    base_fwd = _rolling_max(z, win)
    u_fwd = (base_fwd - z) >= float(dip_m)
    z_rev = z[::-1]
    base_rev = _rolling_max(z_rev, win)[::-1]
    u_rev = (base_rev - z) >= float(dip_m)
    return u_fwd | u_rev


def _group_runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Return inclusive index runs (i0,i1) where mask is True."""
    runs: list[tuple[int, int]] = []
    i = 0
    n = int(mask.size)
    while i < n:
        if not bool(mask[i]):
            i += 1
            continue
        j = i
        while j + 1 < n and bool(mask[j + 1]):
            j += 1
        runs.append((i, j))
        i = j + 1
    return runs


def _interp_at_s(road: dict, s: float) -> tuple[float, float, float]:
    """Return (x,y,width) on the corridor polyline at chainage s."""
    pts = road.get("pts") or []
    cum = road.get("cum") or []
    if len(pts) < 2 or len(cum) != len(pts):
        raise ValueError("road dict must have pts+cum")
    total = float(cum[-1] or 0.0)
    if total <= 1e-9:
        p = pts[0]
        return float(p[0]), float(p[1]), float(p[3] if len(p) > 3 else 7.5)
    s = max(0.0, min(total, float(s)))
    i = 0
    while i + 1 < len(cum) and float(cum[i + 1]) < s - 1e-9:
        i += 1
    i = min(i, len(pts) - 2)
    a = pts[i]
    b = pts[i + 1]
    seg = float(cum[i + 1]) - float(cum[i])
    t = 0.0 if seg <= 1e-9 else (s - float(cum[i])) / seg
    x = float(a[0]) + t * (float(b[0]) - float(a[0]))
    y = float(a[1]) + t * (float(b[1]) - float(a[1]))
    wa = float(a[3] if len(a) > 3 else 7.5)
    wb = float(b[3] if len(b) > 3 else 7.5)
    w = wa + t * (wb - wa)
    return x, y, w


def _dist_point_to_poly_xy(px: float, py: float, xy: list[tuple[float, float]]) -> float:
    best = float("inf")
    for a, b in zip(xy, xy[1:]):
        ax, ay = float(a[0]), float(a[1])
        bx, by = float(b[0]), float(b[1])
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        if seg2 <= 1e-12:
            d = math.hypot(px - ax, py - ay)
        else:
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
            qx = ax + t * dx
            qy = ay + t * dy
            d = math.hypot(px - qx, py - qy)
        if d < best:
            best = d
    return best


def _pick_oid_for_span(
    roads_by_oid: dict,
    *,
    candidate_oids: list[int],
    str_code: str,
    x: float,
    y: float,
) -> tuple[int | None, list[tuple[int, float]]]:
    """Return (best_oid, scored[:N]). Searches member oids first, then same STR_CODE."""
    scored: list[tuple[int, float]] = []

    def add_oid(oid: int) -> None:
        r = roads_by_oid.get(str(oid))
        if not r:
            return
        nodes = r.get("nodes") or []
        xy = [(float(n[0]), float(n[1])) for n in nodes if len(n) >= 2]
        if len(xy) < 2:
            return
        d = _dist_point_to_poly_xy(x, y, xy)
        scored.append((int(oid), float(d)))

    # First: members of the corridor chain.
    for oid in candidate_oids:
        add_oid(int(oid))
    # Fallback: any piece with the same STR_CODE.
    if not scored and str_code:
        for k, r in (roads_by_oid or {}).items():
            try:
                oid = int(k)
            except (TypeError, ValueError):
                continue
            if str(r.get("str_code") or "").strip() != str_code:
                continue
            add_oid(oid)

    if not scored:
        return None, []
    scored.sort(key=lambda t: t[1])
    best = scored[0][0]
    return best, scored[:8]


# GIP OBJEKT is stronger than an empty KUNSTBAUTEN field (see docs/GIP.md).
_OBJEKT_KIND = {
    "S-AT": "tunnel",
    "S-BT": "tunnel",
    "S-LT": "tunnel",
    "S-BG": "gallery",
    "S-AB": "bridge",
    "S-BB": "bridge",
}


def _kind_from_props(props: dict | None, road: dict | None = None) -> str | None:
    """Classify a GIP piece: YAML extra, KUNSTBAUTEN, then OBJEKT code."""
    props = props if isinstance(props, dict) else {}
    extra = str(props.get("_autoroad_gip_extra_kind") or "").strip().lower()
    if extra in {"bridge", "gallery", "tunnel", "culvert", "structure"}:
        return extra
    try:
        import build_bridges as bb  # noqa: WPS433

        kind = bb._structure_kind(props)  # noqa: SLF001
        if kind:
            return kind
    except Exception:
        pass
    name = str(props.get("KUNSTBAUTEN") or "").lower()
    bez = str(props.get("OBJEKTBEZEICHNUNG") or "").lower()
    if "galerie" in name:
        return "gallery"
    if "tunnel" in name or "unterführung" in name or "unterfuehrung" in name or "tunnel" in bez:
        return "tunnel"
    if "brücke" in name or "bruecke" in name or "brücke" in bez or "bruecke" in bez:
        return "bridge"
    if "durchlass" in name:
        return "culvert"
    if name and name != "none":
        return "structure"
    objekt = str(
        props.get("OBJEKT") or (road or {}).get("objekt") or ""
    ).upper().strip()
    return _OBJEKT_KIND.get(objekt)


def _structure_kind(oid: int, feature_props_by_oid: dict, roads_by_oid: dict | None = None) -> str | None:
    """Best-effort structure kind for this OBJECTID (bridge/tunnel/gallery/…)."""
    props = feature_props_by_oid.get(int(oid)) or {}
    road = None
    if roads_by_oid:
        road = roads_by_oid.get(str(int(oid)))
    return _kind_from_props(props, road)


def _already_bridge(
    oid: int, feature_props_by_oid: dict, roads_by_oid: dict | None = None
) -> bool:
    return _structure_kind(oid, feature_props_by_oid, roads_by_oid) == "bridge"


def _road_xy(road: dict) -> list[tuple[float, float]]:
    nodes = road.get("nodes") or []
    return [(float(n[0]), float(n[1])) for n in nodes if len(n) >= 2]


def _collect_existing_structures(
    roads_by_oid: dict, feature_props_by_oid: dict
) -> list[dict]:
    """Known Kunstbauten already in GIP (by name or OBJEKT)."""
    out: list[dict] = []
    for key, road in (roads_by_oid or {}).items():
        try:
            oid = int(key)
        except (TypeError, ValueError):
            continue
        kind = _kind_from_props(feature_props_by_oid.get(oid), road)
        if kind is None:
            continue
        xy = _road_xy(road)
        if len(xy) < 2:
            continue
        out.append(
            {
                "objectid": oid,
                "kind": kind,
                "xy": xy,
                "str_code": str(road.get("str_code") or "").strip() or None,
            }
        )
    return out


def _project_structure_on_corridor(
    struct: dict,
    corridor: dict,
    *,
    max_off_m: float,
) -> tuple[float, float] | None:
    """Station window of a structure on this corridor, or None if it is off-axis."""
    import road_span_profile as rsp  # noqa: WPS433

    s_vals: list[float] = []
    max_d = 0.0
    for x, y in struct.get("xy") or []:
        s, px, py = rsp.project_xy(corridor, float(x), float(y))
        max_d = max(max_d, math.hypot(float(x) - px, float(y) - py))
        s_vals.append(float(s))
    if not s_vals or max_d > float(max_off_m):
        return None
    return min(s_vals), max(s_vals)


def _adjacent_structure(
    *,
    s0: float,
    s1: float,
    samples_xy: list[tuple[float, float]],
    structures: list[dict],
    corridor_stations: dict[int, tuple[float, float]],
    keepout_m: float,
    skip_oid: int | None,
) -> tuple[dict | None, str | None]:
    """Return (structure, reason) if the candidate sits on/next to a Kunstbau."""
    keepout = max(0.0, float(keepout_m))
    lo, hi = min(s0, s1), max(s0, s1)
    for st in structures:
        oid = int(st["objectid"])
        if skip_oid is not None and oid == int(skip_oid):
            continue
        kind = str(st.get("kind") or "structure")
        win = corridor_stations.get(oid)
        if win is not None:
            a0, a1 = win
            if hi + keepout >= a0 and lo - keepout <= a1:
                return st, f"adjacent_{kind}_oid_{oid}"
        for x, y in samples_xy:
            if _dist_point_to_poly_xy(float(x), float(y), st["xy"]) <= keepout:
                return st, f"near_{kind}_oid_{oid}"
    return None, None


def _load_feature_props_by_oid(site: dict) -> dict[int, dict]:
    """Cached GIP geojson feature properties, keyed by OBJECTID."""
    try:
        import build_bridges as bb  # noqa: WPS433
    except ImportError:
        return {}
    from authorities import stamp_gip_props  # noqa: WPS433

    path = bb.find_gip_geojson(site)
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[int, dict] = {}
    for f in data.get("features") or []:
        props = stamp_gip_props(site, f.get("properties") or {})
        oid = props.get("OBJECTID")
        if oid is None:
            continue
        try:
            out[int(oid)] = dict(props)
        except (TypeError, ValueError):
            continue
    return out


def _yaml_snippets(rows: list[dict]) -> str:
    """Small YAML chunk meant to be copy-pasted into a site file."""
    lines: list[str] = []
    lines.append("# Suggested additions (copy into your site YAML):")
    lines.append("beamng:")
    lines.append("  bridges:")
    lines.append("    gip_extra:")
    if not rows:
        lines.append("      []")
        return "\n".join(lines) + "\n"
    for r in rows:
        oid = r.get("objectid")
        name = r.get("name") or (f"Brücke {oid}" if oid is not None else "Brücke")
        lines.append(f"      - objectid: {int(oid)}")
        lines.append("        kind: bridge")
        lines.append(f"        name: {name}")
    lines.append("")
    lines.append("  # Optional: pin auto-abutments (meters along the picked corridor)")
    lines.append("  # bridges:")
    lines.append("  #   items:")
    for r in rows:
        ab = r.get("abutment_s")
        oid = r.get("objectid")
        if oid is None or not ab:
            continue
        s0, s1 = float(ab[0]), float(ab[1])
        lines.append(f"  #     - match: {{ objectid: {int(oid)} }}")
        lines.append(f"  #       abutment_s: [{s0:.2f}, {s1:.2f}]")
    return "\n".join(lines) + "\n"

def _yaml_filtered_snippets(rows: list[dict]) -> str:
    """Commented-out candidate blocks with filter reasons."""
    lines: list[str] = []
    lines.append("# Filtered candidates (NOT suggested automatically).")
    lines.append("# Copy individual blocks into your site YAML if you decide they are needed.")
    if not rows:
        lines.append("# (none)")
        return "\n".join(lines) + "\n"
    for r in rows:
        oid = r.get("objectid")
        reason = str(r.get("filter_reason") or "filtered").strip()
        lines.append(f"# - objectid: {oid}   # {reason}")
        ab = r.get("abutment_s")
        if ab:
            lines.append(f"#   abutment_s: [{float(ab[0]):.2f}, {float(ab[1]):.2f}]")
        lines.append("#")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--step", type=float, default=1.0, help="Sampling step along corridor (m)")
    ap.add_argument(
        "--dip",
        type=float,
        default=1.0,
        help="Dip depth (m) that marks a trench candidate (default 1.0)",
    )
    ap.add_argument(
        "--solid-run",
        type=float,
        default=4.0,
        help="Meters of solid terrain for the rolling baseline window (default 4)",
    )
    ap.add_argument("--min-len", type=float, default=6.0, help="Min candidate length (m)")
    ap.add_argument("--max-len", type=float, default=160.0, help="Max candidate length (m)")
    ap.add_argument(
        "--keepout",
        type=float,
        default=15.0,
        help="Meters of station/XY buffer around existing Kunstbauten (default 15)",
    )
    ap.add_argument(
        "--only-str-code",
        default="",
        help="Restrict to one STR_CODE (e.g. B189). Empty = all named corridors.",
    )
    ap.add_argument(
        "--out-json",
        type=Path,
        default=None,
        help="Report JSON output (default: processed/<site>/diag_missing_bridges.json)",
    )
    ap.add_argument(
        "--out-yaml",
        type=Path,
        default=None,
        help="YAML snippet output (default: processed/<site>/diag_missing_bridges_suggestions.yaml)",
    )
    args = ap.parse_args()

    site = load_site()
    bng = site.get("beamng") or {}
    proc = processed_dir(site)

    size = int(bng.get("mask_size") or 512)
    z_at, hm_label, _extent = _load_heightmap_z(proc, size=size)

    from gip_road_segments import load_gip_corridors, load_gip_road_segments  # noqa: WPS433
    import road_span_profile as rsp  # noqa: WPS433

    corridors = load_gip_corridors(site)
    roads_by_oid = load_gip_road_segments(site)
    props_by_oid = _load_feature_props_by_oid(site)
    existing = _collect_existing_structures(roads_by_oid, props_by_oid)

    step = max(0.25, float(args.step))
    dip_m = max(0.05, float(args.dip))
    solid_run = max(step, float(args.solid_run))
    win = max(1, int(round(solid_run / step)))
    min_len = max(step, float(args.min_len))
    max_len = max(min_len, float(args.max_len))
    keepout_m = max(0.0, float(args.keepout))

    only_code = str(args.only_str_code or "").strip()

    suggested: list[dict] = []
    filtered: list[dict] = []
    all_rows: list[dict] = []
    scanned = 0
    for c in corridors:
        code = str(c.get("str_code") or "").strip()
        if not code:
            continue
        if only_code and code != only_code:
            continue
        scanned += 1
        road = c
        length = float(road.get("length") or 0.0)
        if length < 5.0:
            continue
        corridor_stations: dict[int, tuple[float, float]] = {}
        for st in existing:
            win_s = _project_structure_on_corridor(st, road, max_off_m=max(25.0, keepout_m))
            if win_s is not None:
                corridor_stations[int(st["objectid"])] = win_s
        # Even chainage samples.
        s_vals = list(np.arange(0.0, length + 0.5 * step, step))
        if abs(s_vals[-1] - length) > 0.01:
            s_vals.append(length)
        zc = np.zeros(len(s_vals), dtype=float)
        widths = np.zeros(len(s_vals), dtype=float)
        xys: list[tuple[float, float]] = []
        for i, s in enumerate(s_vals):
            x, y, w = _interp_at_s(road, float(s))
            xys.append((x, y))
            widths[i] = float(w)
            zc[i] = float(z_at(x, y))

        unstable = _unstable_mask(zc, dip_m=dip_m, win=win)
        for i0, i1 in _group_runs(unstable):
            s0 = float(s_vals[max(0, i0 - win)])
            s1 = float(s_vals[min(len(s_vals) - 1, i1 + win)])
            span_len = max(0.0, s1 - s0)
            if span_len < min_len or span_len > max_len:
                continue
            z_min = float(np.min(zc[i0 : i1 + 1]))
            z_ref = float(np.max(zc[max(0, i0 - win) : i0 + 1]))
            max_drop = float(z_ref - z_min)
            if max_drop < dip_m - 1e-6:
                continue

            # Abutments: reuse the proven terrain heuristic (5 lateral lines).
            s_ab0, s_ab1, ab_info = rsp.find_abutments(
                road,
                z_at,
                s0,
                s1,
                dip_m=float(dip_m),
                search_m=float(max(20.0, span_len)),
                step_m=float(step),
                solid_run_m=float(solid_run),
                width=float(np.median(widths)),
                abutment_s=None,
            )
            s_ab0, s_ab1 = float(min(s_ab0, s_ab1)), float(max(s_ab0, s_ab1))

            # How deep is the "graben" if we replace it with a straight deck?
            x0, y0, _w0 = _interp_at_s(road, s_ab0)
            x1, y1, _w1 = _interp_at_s(road, s_ab1)
            z0 = float(z_at(x0, y0))
            z1 = float(z_at(x1, y1))
            if s_ab1 - s_ab0 <= 1e-6:
                sag = 0.0
            else:
                sag = 0.0
                for s, (x, y) in zip(s_vals, xys):
                    if s < s_ab0 - 1e-9 or s > s_ab1 + 1e-9:
                        continue
                    t = (float(s) - s_ab0) / (s_ab1 - s_ab0)
                    z_lin = z0 + t * (z1 - z0)
                    sag = max(sag, float(z_lin - float(z_at(x, y))))

            # Map to an OBJECTID: nearest member polyline at mid-span.
            s_mid = 0.5 * (s0 + s1)
            xm, ym, _wm = _interp_at_s(road, s_mid)
            cand_oids = [int(x) for x in (road.get("objectids") or []) if x is not None]
            oid, scored = _pick_oid_for_span(
                roads_by_oid,
                candidate_oids=cand_oids,
                str_code=code,
                x=float(xm),
                y=float(ym),
            )
            row = {
                "str_code": code,
                "corridor_id": str(road.get("id") or ""),
                "corridor_kind": str(road.get("corridor_kind") or ""),
                "objectid": None if oid is None else int(oid),
                "name": None if oid is None else f"Brücke {int(oid)}",
                "span_s": [round(s0, 2), round(s1, 2)],
                "span_len_m": round(span_len, 2),
                "abutment_s": [round(s_ab0, 2), round(s_ab1, 2)],
                "abut_info": ab_info,
                "max_drop_m": round(max_drop, 3),
                "max_sag_m": round(float(sag), 3),
                "oid_candidates": [
                    {"objectid": int(o), "dist_m": round(float(d), 3)} for o, d in scored
                ],
                "heightmap": hm_label,
            }

            status = "suggested"
            reason = None
            if oid is None:
                status = "filtered"
                reason = "no_objectid_match"
            else:
                kind = _structure_kind(int(oid), props_by_oid, roads_by_oid)
                row["object_kind"] = kind
                if kind is not None and kind != "bridge":
                    status = "filtered"
                    reason = f"object_is_{kind}"
                elif _already_bridge(int(oid), props_by_oid, roads_by_oid):
                    status = "filtered"
                    reason = "already_bridge"
            if status == "suggested":
                span_xy = [
                    xy
                    for s, xy in zip(s_vals, xys)
                    if float(s) >= min(s0, s1) - 1e-9 and float(s) <= max(s0, s1) + 1e-9
                ]
                hit, why = _adjacent_structure(
                    s0=s0,
                    s1=s1,
                    samples_xy=span_xy or [(xm, ym)],
                    structures=existing,
                    corridor_stations=corridor_stations,
                    keepout_m=keepout_m,
                    skip_oid=None if oid is None else int(oid),
                )
                if hit is not None and why:
                    status = "filtered"
                    reason = why
                    row["adjacent"] = {
                        "objectid": int(hit["objectid"]),
                        "kind": hit.get("kind"),
                    }

            row["status"] = status
            if reason:
                row["filter_reason"] = reason
            all_rows.append(row)
            if status == "suggested":
                suggested.append(row)
            else:
                filtered.append(row)

    # Stable ordering: by STR_CODE then corridor then station.
    def _key(r: dict) -> tuple:
        return (
            str(r.get("str_code") or ""),
            str(r.get("corridor_id") or ""),
            float((r.get("span_s") or [0])[0]),
        )

    all_rows.sort(key=_key)
    suggested.sort(key=_key)
    filtered.sort(key=_key)

    out_json = Path(args.out_json) if args.out_json else (proc / "diag_missing_bridges.json")
    out_yaml = Path(args.out_yaml) if args.out_yaml else (proc / "diag_missing_bridges_suggestions.yaml")
    out_filtered_json = proc / "diag_missing_bridges_filtered.json"
    out_filtered_yaml = proc / "diag_missing_bridges_filtered_suggestions.yaml"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_yaml.parent.mkdir(parents=True, exist_ok=True)

    report = {
        "site": str(site.get("id") or site.get("name") or ""),
        "heightmap": hm_label,
        "params": {
            "step_m": step,
            "dip_m": dip_m,
            "solid_run_m": solid_run,
            "min_len_m": min_len,
            "max_len_m": max_len,
            "keepout_m": keepout_m,
            "only_str_code": only_code or None,
            "existing_structures": len(existing),
        },
        "corridors_scanned": scanned,
        "suggested": suggested,
        "filtered": filtered,
        "candidates": all_rows,
        "note": (
            "This tool does not modify the central GIP store (data/roads/*). "
            "objectid is a best-effort spatial pick. "
            "Suggested entries skip known Kunstbauten (KUNSTBAUTEN + OBJEKT) and "
            "neighbors within keepout_m; filtered entries keep a reason for override."
        ),
    }
    out_json.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    out_yaml.write_text(
        _yaml_snippets([h for h in suggested if h.get("objectid") is not None]),
        encoding="utf-8",
    )
    out_filtered_json.write_text(
        json.dumps({"site": report["site"], "heightmap": hm_label, "filtered": filtered}, indent=2, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    out_filtered_yaml.write_text(_yaml_filtered_snippets(filtered), encoding="utf-8")

    print(
        f"diag_missing_bridges: corridors_scanned={scanned} "
        f"suggested={len(suggested)} filtered={len(filtered)} total={len(all_rows)} "
        f"(dip>={dip_m:.2f}m len={min_len:.1f}..{max_len:.1f}m keepout={keepout_m:.1f}m "
        f"step={step:.2f}m existing={len(existing)}) "
        f"heightmap={hm_label}"
    )
    print(f"  JSON: {out_json.relative_to(ROOT)}")
    print(f"  YAML: {out_yaml.relative_to(ROOT)}")
    print(f"  FILTERED JSON: {out_filtered_json.relative_to(ROOT)}")
    print(f"  FILTERED YAML: {out_filtered_yaml.relative_to(ROOT)}")
    for r in suggested[:40]:
        print(
            f"  {r.get('str_code')} {r.get('corridor_id')} "
            f"s={r.get('span_s')} len={r.get('span_len_m')}m "
            f"drop={r.get('max_drop_m')}m sag={r.get('max_sag_m')}m "
            f"oid={r.get('objectid')}"
        )
    if len(suggested) > 40:
        print(f"  … {len(suggested) - 40} more suggested")


if __name__ == "__main__":
    main()

