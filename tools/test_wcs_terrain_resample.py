"""Unit checks for native DGM pad/snap and cubicspline resample."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from wcs_terrain import (  # noqa: E402
    expand_and_snap_bbox,
    resample_elevation_cubicspline,
    target_grid_shape,
)


def test_snap_with_suedtirol_origin() -> None:
    # Civis DTM-2.5m: cell centre (0,0) ≈ 605000 / 5220800
    origin = (605000.0000008999, 5220799.999878633)
    bbox = [660346.0, 5193436.0, 662394.0, 5195484.0]
    fetch, w, h = expand_and_snap_bbox(
        bbox, native_res=2.5, pad_cells=4, grid_origin=origin
    )
    assert w == h == 829
    assert abs((fetch[2] - fetch[0]) / 2.5 - w) < 1e-6
    assert fetch[0] < bbox[0] and fetch[2] > bbox[2]
    assert fetch[1] < bbox[1] and fetch[3] > bbox[3]


def test_cubicspline_preserves_plane() -> None:
    # Native 2.5 m plane z = 1000 + 0.02*e + 0.01*n over a small box.
    nres = 2.5
    native_bbox = [0.0, 0.0, 50.0, 50.0]
    nw, nh = 20, 20
    xs = 0.0 + (np.arange(nw) + 0.5) * nres
    ys = 50.0 - (np.arange(nh) + 0.5) * nres
    xx, yy = np.meshgrid(xs, ys)
    native = (1000.0 + 0.02 * xx + 0.01 * yy).astype(np.float32)
    target_bbox = [5.0, 5.0, 45.0, 45.0]
    tres = 1.0
    out = resample_elevation_cubicspline(
        native,
        native_bbox=native_bbox,
        native_res=nres,
        target_bbox=target_bbox,
        target_res=tres,
    )
    tw, th = target_grid_shape(target_bbox, tres)
    assert out.shape == (th, tw)
    tx = target_bbox[0] + (np.arange(tw) + 0.5) * tres
    ty = target_bbox[3] - (np.arange(th) + 0.5) * tres
    xx2, yy2 = np.meshgrid(tx, ty)
    expect = 1000.0 + 0.02 * xx2 + 0.01 * yy2
    err = np.nanmax(np.abs(out - expect))
    assert err < 0.05, f"plane error {err}"


def test_half_metre_to_one_metre_shape() -> None:
    native = np.arange(100, dtype=np.float32).reshape(10, 10)
    out = resample_elevation_cubicspline(
        native,
        native_bbox=[0.0, 0.0, 5.0, 5.0],
        native_res=0.5,
        target_bbox=[0.0, 0.0, 5.0, 5.0],
        target_res=1.0,
    )
    assert out.shape == (5, 5)
    assert np.isfinite(out).all()


if __name__ == "__main__":
    test_snap_with_suedtirol_origin()
    test_cubicspline_preserves_plane()
    test_half_metre_to_one_metre_shape()
    print("wcs_terrain resample checks ok")
