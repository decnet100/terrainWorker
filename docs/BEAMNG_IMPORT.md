# BeamNG Import — Smoke-Test M28

Die Option heißt **nicht** „Neues Level → Heightmap importieren“ (das war zu knapp formuliert). Es sind **zwei Schritte**:

## 1. Neues Level anlegen

1. BeamNG starten  
2. **F11** → World Editor  
3. **File → New Level** (oder **Ctrl+N**)  
4. Ordner unter `levels/` wählen, z.B. `autoroad_m28_test`  
5. Editor kopiert das Template und öffnet das Level  

Dokumentation: [Terrain / heightmaps](https://docs.beamng.com/modding/levels/level_creation/section2/)

## 2. Heightmap importieren (separat)

**Wichtig:** `theTerrain` → Inspector → `terrainFile` muss  
`/levels/autoroad_m28_test/theTerrain.ter` sein (**nicht** `/levels/template/...`).  
Sonst speichert der Import nicht dauerhaft (Template ist schreibgeschützt).

1. Toolbar → Set **Landscape**/**Default** → **Terrain Tools** → **Import Terrain**  
2. Dialog:  
   - Terrain name: `theTerrain`  
   - Heightmap: `...\autoroad_m28_test\import\heightmap_512.png`  
   - **Meters per Pixel:** `1.0`  
   - **Max Height:** `254.75`  
   - Position: **`0, 0, 0`** (nicht Template −512/−512/100)  
3. **Import** → **File → Save Level**  
4. Template-**ocean** löschen/deaktivieren (sonst Wasser bei Z≈116)

Danach ist das Gelände oft schwarz → **automatische Layer-Masken** nutzen:

### Terrain-Materials aus Masken

In 0.39 heißen die Opacity-/Layer-Maps im Dialog **„Texture Maps“** — die Liste ist anfangs **leer**.

**Einfachster Weg (Preset):**
1. `python tools\build_terrain_masks.py` (sync’t nach `levels/autoroad_m28_test/import/`)
2. Import-Dialog → Menüleiste **Load...** (oder **Recent**)
3. `terrainPreset.json` wählen → Heightmap + **4** Layer werden eingetragen
4. Materials prüfen: Grass / dirt_rocky_large / rock / **Asphalt**, Channel **R**
5. Groundmodels sollten `GRASS` / `DIRT_ROCKY_LARGE` / `ROCK` / `ASPHALT` sein
6. Position `0,0,0` → **Import** → Level speichern

**Manuell:** Unter Texture Maps auf **Add Texture Map** nacheinander:
- `layerMap_0_Grass.png`
- `layerMap_1_dirt_rocky_large.png`
- `layerMap_2_rock.png`
- `layerMap_3_Asphalt.png`

Fahrbahnbreite kommt aus OSM (`~7 m` × `road_width_scale`) plus Dirt-Bankett (`shoulder_m`, Default 1,5 m je Seite) — konfigurierbar in `config/site.yaml`.

Wichtig: BeamNG-Dateidialog sieht nur den **Spiel-VFS** (`/levels/...`), nicht `C:\temp\...`. Dateien müssen im Level-Ordner `import\` liegen.

Nach dem Build: Vorschau `preview_terrain_materials.png` (dunkel = Asphalt, braun = Bankett, grün = Gras, grau = Fels).

Heightmap muss **quadratisch**, **2er-Potenz**, **16-bit PNG** sein — unser Export ist 512×512, 16-bit.

## Leitplanken

```powershell
python tools\build_guardrails.py
```

Schreibt TSStatics nach  
`...\levels\autoroad_m28_test\main\MissionGroup\level_objects\guardrails\items.level.json`  
Mesh: `italy_guardrails_common_section` (Italy-Kit), Segmente Stoß-an-Stoß entlang dem Straßenrand.

Level **neu laden** (kein Terrain-Import nötig). Config: `beamng.guardrails` in `site.yaml`  
(`sides`, `lateral_extra_m`, `section_length_m`, `abut_overlap_m`, `align_pitch`, `snap_to_heightmap`, `pivot_ground_offset_m`, `enabled`).

- `align_pitch: true` — Segmente folgen der Steigung (Pitch über die Sektionslänge)
- `snap_to_heightmap: true` — Z am Gelände unter der Schiene
- `section_length_m: 4.2` — AT-übliche Gerade (Joint-Abstand)
- `abut_overlap_m: 0.08` — leichte Überlappung, damit Enden optisch schließen
- Enge Kurven (R≤15 / R≤10): **kurze Sehnen** statt echter gebogener Profile (gibt es im Stock-BeamNG nicht); Länge folgt der **Rand**-Sehne (außen länger)
- `yaw_flip_right` / `face_y_outward` — Orientierung W-Profil zur Fahrbahn
- Zu hoch/tief: `pivot_ground_offset_m` (±0.1…0.5) oder `z_lift_m`
- Weiter in die Straße: `lateral_extra_m` erhöhen

## Wenn New Level am Ladebildschirm hängt

Das ist ein bekanntes BeamNG-Problem und oft **kein** Zeichen, dass Heightmaps „nicht gehen“.

1. Spiel **hart beenden** (Task-Manager → BeamNG.drive / BeamNG.drive.exe).
2. **Nicht** erneut „New Level“ mit leerem Ordner versuchen.
3. Stattdessen:
   - Offizielle Map **`smallgrid`** oder **`gridmap_v2`** laden und prüfen, dass das Spiel normal startet.
   - Dann **F11** → World Editor.
   - Oder Template manuell kopieren (zuverlässiger):

```powershell
# User-Levels liegen bei dir typisch unter AppData (nicht nur Documents):
# C:\Users\chdem\AppData\Local\BeamNG.drive\<version>\levels\
$ver = "0.36"   # ggf. anpassen
$dst = "$env:LOCALAPPDATA\BeamNG.drive\$ver\levels\autoroad_m28_test"
$zip = "C:\Program Files (x86)\Steam\steamapps\common\BeamNG.drive\content\levels\template.zip"
New-Item -ItemType Directory -Force -Path $dst | Out-Null
Expand-Archive -Path $zip -DestinationPath $dst -Force
```

4. Spiel neu starten → Level `autoroad_m28_test` laden → F11 → **Terrain Tools → Import terrain**.
5. Während dem Hänger: Konsole mit **`` ` ``** (Taste unter Esc) öffnen und Fehler notieren; Log:  
   `%LOCALAPPDATA%\BeamNG.drive\<version>\beamng.log`
6. Mods kurz deaktivieren (besonders Multiplayer/UI-Mods), Cache leeren, nochmal testen.

**Wichtig:** Deine User-Daten liegen unter  
`C:\Users\chdem\AppData\Local\BeamNG.drive\`  
(`Documents\BeamNG.drive` zeigt bei dir auf einen älteren Ordner). Levels gehören in die **aktuelle Versionsnummer**-`levels\`-Ordner.


## Neu bauen

```powershell
cd C:\temp\beamng_autoroad
python tools\fetch_osm.py
python tools\build_smoke.py
```


## BeamNG 0.39 User-Ordner

Ab 0.39 liegt der User-Ordner hier (nicht mehr BeamNG.drive\0.36):

C:\Users\chdem\AppData\Local\BeamNG\BeamNG.drive\current\

Level:
...\current\levels\autoroad_m28_test\

