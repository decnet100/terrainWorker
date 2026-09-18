# Heightmap-Compose (DGM + Layer)

Stand 2026-09-16, Mega-Fernpass. Code: `tools/heightmap_layers.py`, CLI: `tools/compose_heightmap.py`.

Ziel: **ein unveränderliches DGM**, darauf **dünne Vorschläge** (Layer), daraus **eine** composed Heightmap. `build_*` dürfen sich nicht mehr gegenseitig die volle PNG überschreiben.

Verwandt: [BEAMNG_IMPORT.md](BEAMNG_IMPORT.md), [ROADS.md](ROADS.md), [GIP.md](GIP.md).

---

## Dateien (`data/processed/<site>/`)

| Datei | Rolle |
|-------|--------|
| `heightmap_<N>.png` | **DGM**, einmal aus `build_smoke`. Niemals überschreiben. |
| `heightmap_<N>_composed.png` | Mixer-Ergebnis. Das geht nach `import/`. |
| `heightmap_layers/manifest.json` | Welche Layer existieren. |
| `heightmap_layers/water_{dz,w}.png` | Add: Δz (signed mm, Bias 32768) + Opacity |
| `heightmap_layers/road_bed_{z,w}.png` | Replace: Ziel-Z + Opacity |
| `heightmap_layers/span_<part>_{z,w}.png` | Replace-Teile der Span-Layer (`bridge`, `gallery`). |
| `heightmap_layers/steps/` | Optionaler Dump je Mix-Schritt (`--dump-steps`) |

Layer-Reihenfolge steht **nur** in `LAYER_SPECS` (`heightmap_layers.py`), nicht in den Buildern:

```text
water     add      priority 10
road_bed  replace  priority 40
span      replace  priority 55   # Teile: bridge + gallery (Tunnel = gallery)
```

Mixer, Pixel für Pixel. **Opacity** (0..1) ist der Mix — weiche Ränder gehören hierher, nicht als Extra-Falloff in jedem Builder:

1. Start = DGM
2. Add: `z += dz * opacity`
3. Replace (höhere Priority später): `z = (1-opacity)*z + opacity*z_target`
4. Clip auf `[0, max_height_m]`

Span-Teile werden **vor** dem Replace vereinigt (`max(opacity)`, bei Gleichstand gewinnt `gallery` nach `bridge`). Ein Gallery-Rebuild darf die Brücken-Part nicht löschen.

### Step-Dump

```powershell
$env:AUTOROAD_SITE = "config/sites/fernpass_mega.yaml"
python tools\compose_heightmap.py --dump-steps
# oder in jedem build_*, der compose aufruft:
$env:AUTOROAD_COMPOSE_DUMP = "1"
```

Unter `heightmap_layers/steps/` (siehe `index.json`):

| Suffix | Inhalt |
|--------|--------|
| `_z.png` | Absolut, I;16 Meter (0..max_height) |
| `_dz.png` | Relativ zum DGM (Layer) bzw. zum vorigen Mix (`*_applied_dz`), I;16 signed mm + 32768 |
| `_opacity.png` | Mix 0..255, 8-bit — das ist die einzige weiche Kante, die der Mixer kennt |
| `*_after_z.png` | Heightmap nach diesem Layer |

---

## Wer schreibt, wer liest

Writer rufen `compose()` **selbst** am Ende auf. Extra-CLI nur zum Neu-Mischen ohne Rebuild:

```powershell
$env:AUTOROAD_SITE = "config/sites/fernpass_mega.yaml"
python tools\compose_heightmap.py
python tools\compose_heightmap.py --dump-steps
```

| Tool | Schreibt | Liest Heightmap |
|------|----------|-----------------|
| `build_water.py` | `water` (add vs DGM) | DGM |
| `build_decal_roads.py` | `road_bed` (replace vs DGM; Brückendecks skippen) | Snap: **composed** |
| `build_bridges.py` | `span/bridge` | Conform vs DGM |
| `build_galleries.py` | `span/gallery` oder `drop_span_part("gallery")` | Bake vs DGM |
| `build_guardrails.py` | — | composed (Z auf Deck/DGM) |
| `build_terrain_masks.py` | — | composed zuerst |

Windows: Layer-PNGs über `layers_dir(proc) / filename` speichern — `proc / "heightmap_layers/file.png"` legt unter Windows eine Datei mit Slash im Namen an.

Stdout ist oft cp1252: keine `→`/`≈` in `print()`.

---

## BeamNG: `import/` ≠ `theTerrain.ter`

`compose` schreibt die composed-Karte nach

`%LOCALAPPDATA%\BeamNG\BeamNG.drive\current\levels\<level>\import\heightmap_<N>.png`

plus `terrainPreset.json`. Das **TerrainBlock** liest weiter `theTerrain.ter`.

- **Reload ohne Terrain-Import:** neue MeshRoads/Decals/Water, **alte** Heightmap. Das war am Fernpass die Variante, die gut aussah.
- **Terrain Tools → Import `terrainPreset.json`:** composed geht ins `.ter`.
- Speichern ohne Import schreibt Objekte + alte `.ter` fest. Die PNG in `import/` bleibt trotzdem die composed-Version und wartet auf den nächsten Import.

---

## Was 2026-09-16 schiefging

Compose rechnet nicht falsch. `approach_conform` + `approach_conform_deck_raise_m: 16` (Default alle Brücken) stempelte unter der **ganzen** MeshRoad-Platte Terrain auf Deck-Z, Gewicht oft ~0.6–0.8 (vier Streifen überschreiben sich).

Sichtbar nach Terrain-Import, an **jeder** Brücke:

1. **Fahrbahn-Beule / Stufe** am Widerlager: Decal liegt auf dem angehobenen Terrain, MeshRoad auf Konstruktions-Z. Mittellinie knickt, Leitplanke springt.
2. **Kein Freiraum** unter der Platte (Kerbe zugeschüttet).

Ohne Import: MeshRoad über unverändertem DGM, Widerlager passen, Oberfläche glatt.

Deck-Decals laufen bereits mit `overObjects` auf der MeshRoad (`clip_bridges` + `deck_decals`). Dafür muss die Schlucht **nicht** aufgefüllt werden. Heightmap gehört an die **Widerlager**, nicht unter die Platte.

A/B ohne Rebuild (dann Terrain neu importieren, nur zum Vergleich):

```python
import heightmap_layers as hml
hml.drop_span_part(proc, "bridge")
hml.compose(proc, size=8192, max_h=max_h, level_name=level_name)
```

Oder: `span/bridge` im Manifest lassen und nur `deck_raise` auf 0 setzen + `build_bridges` neu — das ist der inhaltliche Fix, nicht Compose.

---

## Nächster Schnitt (nicht Parameter-Wut)

Brücke / Galerie / Tunnel als **ein Span-Prinzip**: MeshRoad-Platte, Decals auf der Platte, DGM in der Öffnung, Terrain nur an den Anschlüssen. Gemeinsame Layer-Part, weniger YAML-Dialekte. Road-Bed und Water bleiben eigene Layer.

„Straßen-Bügeleisen“ (Fahrbahn + Nachbarterrain + Decals als letzter Pass) ist noch nicht gebaut.
