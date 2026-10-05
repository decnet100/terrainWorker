"""Checks for landcover catalog ↔ treatment mapping."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from landcover_catalog import (  # noqa: E402
    TREATMENT_ID,
    apply_treatment_ids,
    landuse_from_treatment,
    load_treatment,
    treatment_masks,
)


def test_corine_groups() -> None:
    treat = load_treatment()
    codes = np.array(
        [
            [312, 324, 231],
            [332, 333, 511],
            [0, 112, 321],
        ],
        dtype=np.uint16,
    )
    ids = apply_treatment_ids(codes, catalog="corine", treatment=treat)
    assert ids[0, 0] == TREATMENT_ID["forest_tall"]
    assert ids[0, 1] == TREATMENT_ID["forest_scrub"]
    assert ids[0, 2] == TREATMENT_ID["meadow"]
    assert ids[1, 0] == TREATMENT_ID["bare"]
    assert ids[1, 1] == TREATMENT_ID["bare"]
    assert ids[1, 2] == TREATMENT_ID["water"]
    assert ids[2, 0] == 0
    assert ids[2, 1] == TREATMENT_ID["building"]
    assert ids[2, 2] == TREATMENT_ID["meadow"]

    masks = treatment_masks(codes, catalog="corine", treatment=treat)
    assert masks["forest_tall"][0, 0]
    assert not masks["forest_tall"][0, 1]
    landuse, extras = landuse_from_treatment(codes, catalog="corine", treatment=treat)
    assert landuse[0, 0] == 4  # CLASS_FOREST
    assert landuse[0, 2] == 7  # CLASS_GRASS
    assert extras["veg_hoch"][0, 0]
    assert extras["bare"][1, 0]


def test_bev_adapter_codes() -> None:
    treat = load_treatment()
    # BEV stored as 1..6 in the catalog adapter
    codes = np.array([[1, 2, 3], [4, 5, 6]], dtype=np.uint16)
    ids = apply_treatment_ids(codes, catalog="bev", treatment=treat)
    assert ids[0, 0] == TREATMENT_ID["forest_tall"]
    assert ids[0, 1] == TREATMENT_ID["forest_scrub"]
    assert ids[0, 2] == TREATMENT_ID["meadow"]
    assert ids[1, 0] == TREATMENT_ID["bare"]
    assert ids[1, 1] == TREATMENT_ID["building"]
    assert ids[1, 2] == TREATMENT_ID["water"]


if __name__ == "__main__":
    test_corine_groups()
    test_bev_adapter_codes()
    print("landcover_catalog checks ok")
