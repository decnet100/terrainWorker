# Heightmap compose (DGM + layers)

State 2026-09-16, Fernpass Mega. Code: `tools/heightmap_layers.py`, CLI: `tools/compose_heightmap.py`.

Goal: **one immutable DGM**, thin **proposals** on top (layers), from that **one** composed heightmap. `build_*` must no longer overwrite the full PNG against each other.

Related: [BEAMNG_IMPORT.md](BEAMNG_IMPORT.md), [ROADS.md](ROADS.md), [GIP.md](GIP.md).

---

## Files (`data/processed/<site>/`)

| File | Role |
|------|------|
| `heightmap_<N>.png` | **DGM**, once from `build_smoke`. Never overwrite. |
| `heightmap_<N>_composed.png` | mixer result. This goes to `import/`. |
| `heightmap_layers/manifest.json` | which layers exist |
| `heightmap_layers/water_{dz,w}.png` | add: Δz (signed mm, bias 32768) + opacity |
| `heightmap_layers/road_bed_{z,w}.png` | replace: target Z + opacity |
| `heightmap_layers/span_<part>_{z,w}.png` | replace parts of the span layer (`bridge`, `gallery`) |
| `heightmap_layers/steps/` | optional dump per mix step (`--dump-steps`) |

Layer order lives **only** in `LAYER_SPECS` (`heightmap_layers.py`), not in the builders:

```text
water     add      priority 10
road_bed  replace  priority 40
span      replace  priority 55   # parts: bridge + gallery (tunnel = gallery)
```

Mixer, pixel by pixel. **Opacity** (0..1) is the mix — soft edges belong here, not as extra falloff in every builder:

1. Start = DGM
2. Add: `z += dz * opacity`
3. Replace (higher priority later): `z = (1-opacity)*z + opacity*z_target`
4. Clip to `[0, max_height_m]`

Span parts are merged **before** replace (`max(opacity)`; on a tie `gallery` wins over `bridge`). A gallery rebuild must not delete the bridge part.

Because **span is later than road-bed**, a gallery side-cut overwrites a ramp’s road-bed at the plate edge. `beamng.roads.side_cut_preserve` skips those pixels (see [STRUCTURES.md](STRUCTURES.md)). Under the slab, `force_deck_z` still owns the mix.

### Step dump

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/fernpass_mega.yaml"; python tools\compose_heightmap.py --dump-steps
```

Or in any `build_*` that calls compose:

```powershell
$env:AUTOROAD_COMPOSE_DUMP = "1"
```

Under `heightmap_layers/steps/` (see `index.json`):

| Suffix | Content |
|--------|---------|
| `_z.png` | absolute, I;16 metres (0..max_height) |
| `_dz.png` | relative to DGM (layer) or to the previous mix (`*_applied_dz`), I;16 signed mm + 32768 |
| `_opacity.png` | mix 0..255, 8-bit — the only soft edge the mixer knows |
| `*_after_z.png` | heightmap after this layer |

---

## Who writes, who reads

Writers call `compose()` **themselves** at the end. Extra CLI only to re-mix without a rebuild:

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/fernpass_mega.yaml"; python tools\compose_heightmap.py
```

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/fernpass_mega.yaml"; python tools\compose_heightmap.py --dump-steps
```

| Tool | Writes | Reads heightmap |
|------|--------|-----------------|
| `build_water.py` | `water` (add vs DGM) | DGM |
| `build_decal_roads.py` | `road_bed` (replace vs DGM; skip bridge decks) | snap: **composed** |
| `build_bridges.py` | `span/bridge` | conform vs DGM |
| `build_galleries.py` | `span/gallery` or `drop_span_part("gallery")` | bake vs DGM |
| `build_guardrails.py` | — | composed (Z on deck/DGM) |
| `build_terrain_masks.py` | — | composed first |

Windows: save layer PNGs via `layers_dir(proc) / filename` — `proc / "heightmap_layers/file.png"` creates a file with a slash in the name on Windows.

Stdout is often cp1252: no `→` / `≈` in `print()`.

---

## BeamNG: `import/` ≠ `theTerrain.ter`

`compose` writes the composed map to

`%LOCALAPPDATA%\BeamNG\BeamNG.drive\current\levels\<level>\import\heightmap_<N>.png`

plus `terrainPreset.json`. The **TerrainBlock** still reads `theTerrain.ter`.

- **Reload without terrain import:** new MeshRoads / decals / water, **old** heightmap. That was the Fernpass variant that looked good.
- **Terrain Tools → Import `terrainPreset.json`:** composed goes into the `.ter`.
- Saving without import locks objects + the old `.ter`. The PNG in `import/` is still the composed version and waits for the next import.

---

## What went wrong on 2026-09-16

Compose is not computing the wrong thing. `approach_conform` + `approach_conform_deck_raise_m: 16` (default on every bridge) stamped terrain up to deck Z under the **whole** MeshRoad slab, weight often ~0.6–0.8 (four strips overwrite each other).

Visible after terrain import, at **every** bridge:

1. **Carriageway bump / step** at the abutment: decal sits on the raised terrain, MeshRoad on construction Z. Centreline kinks, guardrail jumps.
2. **No clearance** under the slab (notch filled in).

Without import: MeshRoad over unchanged DGM, abutments fit, surface smooth.

Deck decals already run with `overObjects` on the MeshRoad (`clip_bridges` + `deck_decals`). The gorge does **not** need filling for that. Heightmap belongs at the **abutments**, not under the slab.

A/B without rebuild (then re-import terrain, comparison only):

```python
import heightmap_layers as hml
hml.drop_span_part(proc, "bridge")
hml.compose(proc, size=8192, max_h=max_h, level_name=level_name)
```

Or: leave `span/bridge` in the manifest and set only `deck_raise` to 0 + rebuild `build_bridges` — that is the content fix, not compose.

---

## Next cut (not parameter thrash)

Bridge / gallery / tunnel as **one span principle**: MeshRoad slab, decals on the slab, DGM in the opening, terrain only at the joins. Shared layer part, fewer YAML dialects. Road-bed and water stay their own layers.

A “road iron” (carriageway + neighbour terrain + decals as a last pass) is not built yet.
