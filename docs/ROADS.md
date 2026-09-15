# Straßen: Breite, Decals, Road-Bed

Stand nach Mega-Fernpass-Feinschliff (Lanes→Breite, Decal-Übergänge, Leitpoller).

Verwandt: [BEAMNG_IMPORT.md](BEAMNG_IMPORT.md), [GIP.md](GIP.md), `tools/road_width.py`.

---

## Fahrbahnbreite (Lanes → Meter)

**Default:** `default_lanes × lane_width_m` = **2 × 3.75 m = 7.5 m**  
(`beamng.lane_width_m` / `beamng.default_lanes` in der Site-YAML).

### Wo steckt was?

| Ort | Inhalt |
|-----|--------|
| Site-YAML | Formel / Defaults (`lane_width_m`, `default_lanes`, optional `lanes_by_highway`) |
| `data/raw/osm_roads_*.json` | OSM-Rohdaten (`lanes`, `width`, Geometrie) — Cache von Overpass |
| `data/processed/.../roads_beamng.json` | **Fertige** Node-Breiten `[x,y,z,width]` — das nutzen Decals/Masken/Guardrails |

YAML speichert **nicht** die Breite jeder Straße; die wird beim Smoke aus OSM + Formel gerechnet und in `roads_beamng.json` abgelegt.

### Auslese-Reihenfolge (`tools/road_width.py` → `build_smoke`)

1. OSM-Tag `width` (m), sonst  
2. OSM **`lanes`** (Gesamtzahl) × `lane_width_m`, sonst  
3. Nur wenn `lanes` fehlt **und** beide `lanes:forward` **und** `lanes:backward` gesetzt: Summe × `lane_width_m`, sonst  
4. `lanes_by_highway[highway]` bzw. `default_lanes` × `lane_width_m`

**Wichtig (OSM):** `lanes=` ist die **Gesamtzahl**. Bei Zweirichtungsstraßen ist `lanes=2` = eine Spur je Richtung.  
Nur `lanes:backward=1` neben `lanes=2` darf das Total **nicht** überschreiben (häufiges Fernpass-Tagging) — sonst entstehen fälschlich 3.75 m-Abschnitte.

### Neu rechnen

```powershell
cd C:\temp\beamng_autoroad
$env:AUTOROAD_SITE = "config/sites/fernpass_mega.yaml"
# OSM-Cache nur nötig, wenn Overpass-Rohdaten veraltet sind:
# Remove-Item -ErrorAction SilentlyContinue data\raw\osm_roads_tirol-fernpass-8192.json
python tools\build_smoke.py          # schreibt roads_beamng (+ Masken/Guardrails)
python tools\build_decal_roads.py    # Decals mit neuen Breiten
```

---

## Decal-Übergänge (Stitch + Width-Smooth)

OSM zerlegt die B179 in viele kurze Ways. Ohne Merge entstehen Lücken; mit Merge bleiben kurze schmale Stubs als **Taillen** (2–1–3…).

`build_decal_roads`:

1. **`stitch_abutting`** — Degree-2 End-an-End mergen (`stitch_tol_m`)  
2. **`width_fill_dip_m`** (Default 40) — kurze schmale Dips morphologisch schließen  
3. **`width_blend_m`** (Default 25) — verbleibende Lane-Sprünge weich auslaufen  
4. optional Galerie-Clip; `terrain_roof: keep_asphalt` wird **nicht** weggeschnitten (z. B. Tunnel 8082)

```powershell
python tools\build_decal_roads.py --skip-road-bed   # nur Decal-JSON
python tools\build_decal_roads.py                   # inkl. Road-Bed-Heightmap
```

---

## Road-Bed (Terrain unter der Straße)

**Was:** Geglättetes Höhenband unter der Fahrbahn in der Heightmap — nicht die Decal-Textur. Ziel: DGM-Mikro-Zacken dämpfen, Decal und Terrain auf einer gemeinsamen Grade.

**Wann aktiv:** `beamng.decal_roads.road_bed_conform: true` und Build **ohne** `--skip-road-bed`.

**Ausgabe:** `data/processed/<site>/heightmap_<size>_road_bed.png` (+ Sync nach Level-`import/`).

**Wichtige Knöpfe:**

| Key | Rolle |
|-----|--------|
| `road_bed_smooth_m` | Längsfenster für Ziel-Z (größer = glattere Grade) |
| `road_bed_pad_m` / `falloff_m` | Breite des gestempelten Bandes |
| `road_bed_sink_m` | Bett etwas unter Decal-Z |
| `road_bed_max_raise_m` / `max_cut_m` | Clamp gegen DGM |

Decals: `snap_to_heightmap: true` → Node-Z vom Road-Bed (falls vorhanden).

**„Neu im Terrain“:** PNG schreiben reicht nicht. Im World Editor Heightmap / `terrainPreset.json` **neu importieren** und Level speichern — sonst fährt man weiter auf der alten Terrain-Geometrie. Micro-Bumps trotz Smooth = oft vergessener Re-Import.

---

## Leitpoller / Guardrails

Default Mega-Fernpass: `style: posts`, Mesh `reflector` (vendored inkl. Materialien), `post_scale: 0.7` (~1 m), `spacing_m: 25`.

```powershell
# Alles entfernen (items.level.json leeren)
python tools\build_guardrails.py --clear
# Level neu laden (nicht speichern, falls noch Doppelte aus alter Session)

# Neu erzeugen (wipe + write, eindeutige Namen, Dedup)
python tools\build_guardrails.py
# Level erneut laden
```

Italy-Schienen: `style: sections` + `italy_guardrails_common_section`.

---

## Terrain-Masken-Cache

`build_terrain_masks` cached Tirol-Landcover + DGM-Slope unter  
`processed/<site>/cache/terrain_masks/`. Roads/Brücken/Galerien werden immer neu gerechnet.

```powershell
python tools\build_terrain_masks.py          # Cache nutzen
python tools\build_terrain_masks.py --force  # neu rasterisieren
```
