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


def _already_bridge(oid: int, feature_props_by_oid: dict) -> bool:
    """True if cached GIP feature is already classified as a bridge."""
    try:
        import build_bridges as bb  # noqa: WPS433
    except Exception:
        bb = None  # type: ignore
    props = feature_props_by_oid.get(int(oid)) or {}
    if not isinstance(props, dict):
        return False
    if props.get("_autoroad_gip_extra_kind") == "bridge":
        return True
    if bb is not None:
        try:
            return bb._structure_kind(props) == "bridge"  # noqa: SLF001
        except Exception:
            pass
    name = str(props.get("KUNSTBAUTEN") or "").lower()
    bez = str(props.get("OBJEKTBEZEICHNUNG") or "").lower()
    return ("brücke" in name or "bruecke" in name or "brücke" in bez or "bruecke" in bez)


def _load_feature_props_by_oid(site: dict) -> dict[int, dict]:
    """Cached GIP geojson feature properties, keyed by OBJECTID."""
    try:
        import build_bridges as bb  # noqa: WPS433
    except ImportError:
        return {}
    path = bb.find_gip_geojson(site)
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[int, dict] = {}
    for f in data.get("features") or []:
        props = f.get("properties") or {}
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
        "--only-str-code",
        default="",
        help="Restrict to one STR_CODE (e.g. B179). Empty = all named corridors.",
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

    step = max(0.25, float(args.step))
    dip_m = max(0.05, float(args.dip))
    solid_run = max(step, float(args.solid_run))
    win = max(1, int(round(solid_run / step)))
    min_len = max(step, float(args.min_len))
    max_len = max(min_len, float(args.max_len))

    only_code = str(args.only_str_code or "").strip()

    hits: list[dict] = []
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
            if oid is not None and _already_bridge(int(oid), props_by_oid):
                continue

            hits.append(
                {
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
                    "oid_candidates": [{"objectid": int(o), "dist_m": round(float(d), 3)} for o, d in scored],
                    "heightmap": hm_label,
                }
            )

    # Stable ordering: by STR_CODE then corridor then station.
    hits.sort(key=lambda r: (str(r.get("str_code") or ""), str(r.get("corridor_id") or ""), float((r.get("span_s") or [0])[0])))

    out_json = Path(args.out_json) if args.out_json else (proc / "diag_missing_bridges.json")
    out_yaml = Path(args.out_yaml) if args.out_yaml else (proc / "diag_missing_bridges_suggestions.yaml")
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
            "only_str_code": only_code or None,
        },
        "corridors_scanned": scanned,
        "candidates": hits,
        "note": (
            "objectid is a best-effort spatial pick. "
            "Copy YAML suggestions into beamng.bridges.gip_extra; "
            "optionally pin abutment_s under beamng.bridges.items."
        ),
    }
    out_json.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    out_yaml.write_text(_yaml_snippets([h for h in hits if h.get("objectid") is not None]), encoding="utf-8")

    print(
        f"diag_missing_bridges: corridors_scanned={scanned} candidates={len(hits)} "
        f"(dip>={dip_m:.2f}m len={min_len:.1f}..{max_len:.1f}m step={step:.2f}m) "
        f"heightmap={hm_label}"
    )
    print(f"  JSON: {out_json.relative_to(ROOT)}")
    print(f"  YAML: {out_yaml.relative_to(ROOT)}")
    for r in hits[:40]:
        print(
            f"  {r.get('str_code')} {r.get('corridor_id')} "
            f"s={r.get('span_s')} len={r.get('span_len_m')}m "
            f"drop={r.get('max_drop_m')}m sag={r.get('max_sag_m')}m "
            f"oid={r.get('objectid')}"
        )
    if len(hits) > 40:
        print(f"  … {len(hits) - 40} more")


if __name__ == "__main__":
    main()

