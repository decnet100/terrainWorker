# BeamNG Import

**File → New Level** im World Editor ist bei dir unzuverlässig.  
**Standardweg:** `python tools\setup_beamng_level.py <name>` — Template entpacken, Pfade umschreiben, Ocean/Props raus, Import-Assets kopieren.

User-Daten (BeamNG **0.39+**):

```text
C:\Users\<user>\AppData\Local\BeamNG\BeamNG.drive\current\levels\
```

| Site | Level-Ordner | Heightmap | Max Height | mpp |
|------|--------------|-----------|------------|-----|
| Hahntennjoch | `autoroad_m28_test` | `heightmap_512.png` | aus `heightmap_meta.json` (~254.75) | 1.0 |
| L13 Kühtai | `autoroad_galerie_test` | `heightmap_1024.png` | aus `heightmap_meta.json` (~424.63) | 1.0 |

Genauwerte immer aus `data/processed/<site>/heightmap_meta.json` nehmen.

---

## Fahrbahnbreite (Lanes)

Default-Modell: **`default_lanes` × `lane_width_m` = 2 × 3.75 m = 7.5 m** (`beamng` in Site-YAML).

Details, OSM-Fallen (`lanes` vs. `lanes:backward`), Decal-Übergänge und **Road-Bed**: [ROADS.md](ROADS.md).

Kurz: `tools/build_smoke.py` → `roads_beamng.json` (Node-`width`); danach Decals/Masken neu.

---

## 1. Level aus Template anlegen (Skript)

**Nicht** File → New Level und **nicht** den alten Expand-Archive-One-Liner — Zip-Struktur und Pfade `/levels/template/...` machen das leicht kaputt.

Spiel **schließen**, dann:

```powershell
cd C:\temp\beamng_autoroad

# L13 (Name aus Site-YAML, Import-Assets werden mitkopiert):
python tools\setup_beamng_level.py autoroad_galerie_test --site config/sites/l13_kuehtai.yaml

# Hahntennjoch:
python tools\setup_beamng_level.py autoroad_m28_test --site config/sites/hahntennjoch.yaml

# Oder nur Namen tippen (Prompt):
python tools\setup_beamng_level.py
```

Bestehenden Ordner ersetzen: `--force`.

Das Skript:

- entpackt `content\levels\template.zip` nach  
  `%LOCALAPPDATA%\BeamNG\BeamNG.drive\current\levels\<name>\`
- schreibt alle `/levels/template` → `/levels/<name>` um (wichtig!)
- setzt `info.json` (Titel, `isAuxiliary=false`)
- entfernt Ocean/WaterPlane + Template-Backdrop/Groundcover
- setzt `theTerrain.terrainFile` schreibbar, Position `0,0,0`, `maxHeight` aus Site-Meta
- legt Spawn nahe Ursprung
- kopiert `import/` aus `data/processed/...` wenn Site passt

Am Ende steht `READY: ...` mit den nächsten BeamNG-Schritten.

Danach:

1. BeamNG → Freeroam → Level laden (nicht „New Level“).
2. **F11** → Abschnitt **2. Heightmap importieren**.
3. Nach Import: `python tools\build_smoke.py` (mit passendem `AUTOROAD_SITE`) für Masken-Sync + Guardrails.

---

## 2. Heightmap importieren

**Wichtig:** `theTerrain` → Inspector → `terrainFile` muss  
`/levels/<dein_level>/theTerrain.ter` sein (**nicht** `/levels/template/...`).  
Sonst speichert der Import nicht dauerhaft (Template-Pfad ist schreibgeschützt).

1. Toolbar → Landscape/Default → **Terrain Tools** → **Import Terrain**
2. Dialog:
   - Terrain name: `theTerrain`
   - Heightmap: `/levels/<level>/import/heightmap_*.png`  
     (oder Dateien aus `data/processed/...` vorher nach `import\` kopieren)
   - **Meters per Pixel:** `1.0`
   - **Max Height:** Wert aus `heightmap_meta.json` → `max_height_m`
   - Position: **`0, 0, 0`** (nicht Template −512/−512/100)
3. **Import** → **File → Save Level**
4. Template-**ocean** löschen/deaktivieren (sonst Wasser bei Z≈116)

Danach oft schwarz → Layer-Masken (nächster Abschnitt).

### Terrain-Materials aus Masken

In 0.39 heißen Opacity-/Layer-Maps **„Texture Maps“** — die Liste ist anfangs **leer**.

**Preset (einfachster Weg):**

1. `python tools\build_terrain_masks.py` (mit passendem `AUTOROAD_SITE`) — sync’t nach `levels/<level>/import/`
2. Import-Dialog → **Load...** → `terrainPreset.json`
3. Materials: Grass / dirt_rocky_large / rock / **Asphalt**, Channel **R**
4. Groundmodels: `GRASS` / `DIRT_ROCKY_LARGE` / `ROCK` / `ASPHALT`
5. Position `0,0,0` → **Import** → speichern

**Manuell:** Texture Maps → **Add Texture Map**:

- `layerMap_0_Grass.png`
- `layerMap_1_dirt_rocky_large.png`
- `layerMap_2_rock.png`
- `layerMap_3_Asphalt.png`

Fahrbahnbreite: OSM (~7 m × `road_width_scale`) + Dirt-Bankett (`shoulder_m`).

Der Dateidialog sieht nur den **Spiel-VFS** (`/levels/...`), nicht `C:\temp\...`. Dateien müssen unter `levels\<level>\import\` liegen.

Heightmap: **quadratisch**, **2er-Potenz**, **16-bit PNG** (512 oder 1024 je Site).

Vorschau nach Build: `preview_terrain_materials.png`.

Offizielle Doku: [Terrain / heightmaps](https://docs.beamng.com/modding/levels/level_creation/section2/)

---

## 3. Leitplanken

Details: [ANNOTATIONS.md](ANNOTATIONS.md)

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/l13_kuehtai.yaml"; python tools\seed_annotations.py; python tools\build_guardrails.py
```

Schreibt TSStatics nach  
`...\levels\<level>\main\MissionGroup\level_objects\guardrails\items.level.json`

Default: **Leitpoller** (`style: posts`, Mesh `reflector.dae` ≈1.43 m nativ, `post_scale: 0.7` ≈1.0 m, Abstand `spacing_m` 25 oder 50).  
Build vendored Mesh + `main.materials.json` nach `art/shapes/objects/` (sonst fehlen Materialien bei Cross-Level-Refs).  
Alternativ kontinuierliche Italy-Schienen: `style: sections` + `italy_guardrails_common_section`.

**Doppelte entfernen / neu setzen:**

```powershell
python tools\build_guardrails.py --clear    # leert items.level.json
# Level neu laden (nicht speichern)
python tools\build_guardrails.py            # wipe + neu schreiben
# Level erneut laden
```

Level **neu laden**. Config: `beamng.guardrails` + `annotations.guardrail_source` (`auto`|`gpkg`|`heuristic`).

- `auto`: nicht-leerer `guardrail`-Layer → GPKG, sonst OSM-Heuristik
- `style` / `spacing_m` / `post_scale` — Leitpoller
- `--clear` — nur SimGroup leeren
- `align_pitch` / `snap_to_heightmap` / `section_length_m` / `abut_overlap_m` — siehe Site-YAML (`sections`)
- `yaw_flip_right` / `face_y_outward` — W-Profil / Reflektor zur Fahrbahn
- Höhe: `pivot_ground_offset_m` / `z_lift_m`; Abstand Heuristik: `lateral_extra_m`

---

## Wenn Straße weg / Spawn falsch

**Spawn:** oft noch Template `[32,32,5]` (Ecke). Freeroam braucht zusätzlich einen **benannten** Spawn + `info.json`.

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/l13_kuehtai.yaml"; python tools\fix_beamng_level.py
```

Erwarteter Spawn ca. Mitte L13 `(573, 498, ~83)`, Objektname `spawns_default`. BeamNG vorher schliessen.

**Spawn selbst im World Editor setzen (empfohlen):**
1. Level laden → F11
2. Scene Tree: `MissionGroup` → `PlayerDropPoints` → Objekt **`spawns_default`**
3. Mit Move-Tool (W) auf die Straße ziehen; Z etwas über dem Boden
4. File → Save Level
5. Freeroam neu starten (oder Fahrzeug neu spawnen) — `info.json` zeigt schon auf `spawns_default`

Skript-Auswahl bisher: längste/höchste OSM-Klasse (`secondary` L13), Punkt möglichst nahe Kartenmitte, Z = Heightmap + Lift.

**Keine Asphalt-Oberfläche nach Re-Import:** Meist Heightmap **ohne** Texture Maps importiert.  
`theTerrain.terrain.json` enthält dann z. B. noch `BeachSand` statt `Asphalt`.

1. Terrain Tools → Import Terrain → **Load…** →  
   `/levels/autoroad_galerie_test/import/terrainPreset.json`
2. Prüfen: vier Texture Maps, letzte = `layerMap_3_Asphalt.png`, Material **Asphalt**, Channel **R**
3. Position `0,0,0` → Import → Save Level → Level neu laden

Nur Heightmap erneut zu importieren löscht die Layer-Zuordnung. Vorschau: `import/preview_terrain_materials.png` (dunkel = Asphalt).

Wenn `main/.../terrain/items.level.json` leer ist (nach Save passiert das manchmal): `fix_beamng_level.py` stellt den TerrainBlock wieder her.


1. Spiel hart beenden (Task-Manager).
2. **Nicht** erneut File → New Level mit leerem Ordner.
3. Offizielle Map (`smallgrid` / `gridmap_v2`) laden — wenn die geht, Template-Pfad prüfen und Abschnitt 1 wiederholen.
4. Konsole `` ` `` (unter Esc); Log:  
   `%LOCALAPPDATA%\BeamNG\BeamNG.drive\current\beamng.log`  
   (ältere Installationen ggf. `%LOCALAPPDATA%\BeamNG.drive\<version>\`)
5. Mods kurz deaktivieren, nochmal testen.

**Nicht** Levels nach `Documents\BeamNG.drive` oder `BeamNG.drive\0.36\` legen — die Pipeline synct nach `BeamNG\BeamNG.drive\current\levels\`.

---

## Neu bauen

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/l13_kuehtai.yaml"; python tools\fetch_dgm.py; python tools\build_smoke.py
```
