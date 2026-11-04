"""Chains of structures that follow one another on the road (gallery - tunnel - gallery), and the height checks at
every transition.

A tunnel between two galleries has no daylight portal of its own: the road at its ends runs under the gallery roofs,
and the DGM there is the roof (laser-scan filtering keeps it as ground), several metres above the road. Taking a
portal height from the DGM at such a joint puts the tube on the roof. So a chain is built as one: heights only where
it meets the open road at its two outer ends, a straight line between them, and every joint takes its height from
that line.

``check_transitions`` is the yardstick for every portal and joint: no step in height, no grade break above a limit,
from the inside of the structure over the portal to the road outside.
"""
from __future__ import annotations

import math

import numpy as np


def _d(p, q) -> float:
    return math.hypot(float(p[0]) - float(q[0]), float(p[1]) - float(q[1]))


def find_chains(feats: list[dict], *, tol_m: float = 5.0) -> list[dict]:
    """Structures whose ends meet end to end, in road order.

    Two ends of different features within ``tol_m`` are linked. Two features linked at both ends are parallel tubes
    sharing mouths, not a chain; an end close to more than one other end is ambiguous; both are left unlinked (and
    printed). Returns chains of two or more: ``{"members": [{"idx", "rev"}], "joints": [(x, y)]}``; ``rev`` means the
    member's own ``xy`` runs against the chain.
    """
    ends = []  # (feature index, 0 = first point / 1 = last point, xy)
    for i, f in enumerate(feats):
        xy = f.get("xy") or []
        if len(xy) >= 2:
            ends.append((i, 0, xy[0]))
            ends.append((i, 1, xy[-1]))
    links: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for a in range(len(ends)):
        for b in range(a + 1, len(ends)):
            ia, ea, pa = ends[a]
            ib, eb, pb = ends[b]
            if ia == ib or _d(pa, pb) > tol_m:
                continue
            links.setdefault((ia, ea), []).append((ib, eb))
            links.setdefault((ib, eb), []).append((ia, ea))
    for key, other in list(links.items()):
        if len(other) > 1:
            print(f"chain: end {key[1]} of {feats[key[0]].get('name')!r} meets {len(other)} ends - left unlinked")
    link = {k: v[0] for k, v in links.items() if len(v) == 1 and len(links.get(v[0]) or []) == 1}
    for (i, e), (j, f_) in list(link.items()):
        if (i, 1 - e) in link and link[(i, 1 - e)][0] == j:
            print(f"chain: {feats[i].get('name')!r} and {feats[j].get('name')!r} meet at both ends (parallel) - unlinked")
            for k in ((i, e), (i, 1 - e), (j, 0), (j, 1)):
                link.pop(k, None)
    seen: set[int] = set()
    chains: list[dict] = []
    for start in range(len(feats)):
        if start in seen or len(feats[start].get("xy") or []) < 2:
            continue
        free = [e for e in (0, 1) if (start, e) not in link]
        if not free:
            continue  # inside a chain (or a ring): reached from an end
        # walk from the free end
        members = []
        joints = []
        i, enter = start, free[0]
        while True:
            seen.add(i)
            members.append({"idx": i, "rev": enter == 1})
            exit_end = 1 - enter
            nxt = link.get((i, exit_end))
            if nxt is None or nxt[0] in seen:
                break
            joints.append(tuple(feats[i]["xy"][-1 if exit_end == 1 else 0]))
            i, enter = nxt
        if len(members) >= 2:
            chains.append({"members": members, "joints": joints})
    return chains


def member_xy(feat: dict, rev: bool) -> list[tuple[float, float]]:
    xy = [tuple(p) for p in feat["xy"]]
    return list(reversed(xy)) if rev else xy


def poly_len(xy) -> float:
    return sum(_d(a, b) for a, b in zip(xy, xy[1:]))


# ---------------------------------------------------------------------------------------------------------------
# transition check


def _profile(nodes: list[dict]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    ns = sorted(nodes, key=lambda n: float(n["s"]))
    s = np.array([float(n["s"]) for n in ns])
    z = np.array([float(n["z_road"]) for n in ns])
    xy = np.array([(float(n["x"]), float(n["y"])) for n in ns])
    keep = np.r_[True, np.diff(s) > 1e-6]
    return s[keep], z[keep], xy[keep]


def _grade(s: np.ndarray, z: np.ndarray, a: float, b: float) -> float | None:
    """Mean grade over [a, b] (dz/ds along +s); None if the profile does not cover it."""
    lo, hi = min(a, b), max(a, b)
    if lo < s[0] - 1e-6 or hi > s[-1] + 1e-6 or hi - lo < 0.5:
        return None
    return float((np.interp(hi, s, z) - np.interp(lo, s, z)) / (hi - lo))


def max_kink(s: np.ndarray, z: np.ndarray, *, s_lo: float, s_hi: float, window_m: float) -> tuple[float, float]:
    """Largest grade break in [s_lo, s_hi]: grade over the next ``window_m`` minus grade over the last ``window_m``.

    Returns (break, s). A grade break shorter than the window is still found in full: a step of height h shows as a
    break of 2 h / window.
    """
    best, at = 0.0, s_lo
    st = s_lo
    while st <= s_hi + 1e-9:
        g_b = _grade(s, z, st - window_m, st)
        g_a = _grade(s, z, st, st + window_m)
        if g_b is not None and g_a is not None and abs(g_a - g_b) > abs(best):
            best, at = g_a - g_b, st
        st += 0.5
    return best, at


def check_portal(
    nodes: list[dict],
    *,
    s_portal: float,
    out: float,
    ground_z,
    window_m: float = 5.0,
    inside_m: float = 20.0,
) -> dict:
    """Portal to open road: grade break along the deck from ``inside_m`` in to the deck end, and at the deck end the
    step and grade break onto the ground (``ground_z(x, y)``, the road or DGM) along the exit direction."""
    s, z, xy = _profile(nodes)
    s_in = s_portal - out * inside_m
    s_end = s[-1] if out > 0 else s[0]
    lo, hi = sorted((s_in, s_end))
    lo, hi = max(lo, s[0] + window_m), min(hi, s[-1] - window_m)
    kink, kink_s = max_kink(s, z, s_lo=lo, s_hi=hi, window_m=window_m) if hi > lo else (0.0, s_portal)
    k_end = -1 if out > 0 else 0
    p_end = xy[k_end]
    p_prev = xy[-2] if out > 0 else xy[1]
    t = p_end - p_prev
    t = t / max(float(np.hypot(*t)), 1e-9)
    z_end = float(z[k_end])
    g_deck = _grade(s, z, s_end - out * window_m, s_end)
    g_deck = None if g_deck is None else g_deck * out  # along the exit
    res = {"s": round(s_portal, 2), "deck_end_s": round(float(s_end), 2), "kink": round(kink, 4),
           "kink_s": round(kink_s, 2), "step_m": None, "kink_end": None}
    res["grade_deck_end"] = None if g_deck is None else round(g_deck, 4)
    if ground_z is not None:
        zg0 = ground_z(*p_end)
        zg1 = ground_z(*(p_end + t * window_m))
        if zg0 is not None:
            res["step_m"] = round(float(zg0) - z_end, 3)
        if zg0 is not None and zg1 is not None and g_deck is not None:
            g_ground = (float(zg1) - float(zg0)) / window_m
            res["kink_end"] = round(g_ground - g_deck, 4)
            res["grade_ground"] = round(g_ground, 4)
    return res


def check_joint(
    nodes_a: list[dict], nodes_b: list[dict], xy: tuple[float, float], *, window_m: float = 5.0
) -> dict:
    """Joint between two members: height step and grade break where their decks meet (nearest deck nodes)."""

    def near(nodes):
        s, z, pxy = _profile(nodes)
        d = np.hypot(pxy[:, 0] - xy[0], pxy[:, 1] - xy[1])
        k = int(np.argmin(d))
        # grade pointing away from the joint into this member
        inward = 1.0 if k < len(s) / 2 else -1.0
        g = _grade(s, z, s[k], s[k] + inward * window_m)
        return float(z[k]), float(d[k]), (None if g is None else g * inward), float(s[k])

    za, da, ga, sa = near(nodes_a)
    zb, db, gb, sb = near(nodes_b)
    kink = None if ga is None or gb is None else ga + gb  # both point away from the joint: a straight line sums to 0
    return {"x": round(float(xy[0]), 2), "y": round(float(xy[1]), 2), "z_a": round(za, 3), "z_b": round(zb, 3),
            "step_m": round(zb - za, 3), "kink": None if kink is None else round(kink, 4),
            "gap_m": round(max(da, db), 2)}


def judge(res: dict, *, step_max: float, kink_warn: float, kink_max: float) -> str:
    """'ok', 'warn' or 'fail' for one transition."""
    level = "ok"
    v = res.get("step_m")
    if v is not None and abs(v) > step_max:
        return "fail"
    for key in ("kink", "kink_end"):
        v = res.get(key)
        if v is None:
            continue
        if abs(v) > kink_max:
            return "fail"
        if abs(v) > kink_warn:
            level = "warn"
    return level
