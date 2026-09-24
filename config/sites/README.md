# Site profiles

| File | Area | Level | Size |
|------|------|-------|------|
| [`hahntennjoch.yaml`](hahntennjoch.yaml) | Hahntennjoch (guardrails / terrain) | `autoroad_m28_test` | ~500×510 → 512² |
| [`l13_kuehtai.yaml`](l13_kuehtai.yaml) | L13 Sellraintal / Kühtai (galleries) | `autoroad_galerie_2048` | 2048² |
| [`l13_splining.yaml`](l13_splining.yaml) | L13 splining test (road network + MeshRoads) | `autoroad_l13_splining` | 2048² |
| [`fernpass.yaml`](fernpass.yaml) | B179 Fernpass | `autoroad_fernpass_4096` | 4096² |
| [`fernpass_mega.yaml`](fernpass_mega.yaml) | B179 Fernpass (flagship) | `autoroad_fernpass_8192` | 8192² |
| [`testarena.yaml`](testarena.yaml) | Specimen arena (crop, GIP 2304 first) | `autoroad_testarena` | 512² |
| [`reschen.yaml`](reschen.yaml) | B180 Reschenstraße (Prutz / Kaunertal) | `autoroad_reschen_8192` | 8000×8000 → 8192² |
| [`imst.yaml`](imst.yaml) | B189 Imst / Tarrenz (defaults check) | `autoroad_imst_8192` | 8000×8000 → 8192² |
| [`oetz.yaml`](oetz.yaml) | Draft copy of the site template | `autoroad_test` | — |

A new profile needs `crs` + `bbox`, then `authorities` from that crop
([AUTHORITIES.md](../../docs/AUTHORITIES.md)):

```powershell
cd C:\temp\beamng_autoroad; python tools\fill_site_authorities.py --site config\sites\NEU.yaml --write
```

Or pick the map in the step window and use **Authorities**.

## Choose the active site

Usual path: pick the map in the step window. Core steps = `build_level.py`; extras (GIP, decals, structures, rails) are the later cards.

```powershell
cd C:\temp\beamng_autoroad; python tools\pipeline_gui.py
```

**CLI default:** `config/site.yaml` (local, gitignore) = copy of Hahntennjoch.

```powershell
Copy-Item config\sites\hahntennjoch.yaml config\site.yaml -Force
```

```powershell
Remove-Item Env:AUTOROAD_SITE -ErrorAction SilentlyContinue
```

```powershell
$env:AUTOROAD_SITE = "config/sites/l13_kuehtai.yaml"
```

```powershell
$env:AUTOROAD_SITE = "config/sites/fernpass.yaml"
```

```powershell
$env:AUTOROAD_SITE = "config/sites/fernpass_mega.yaml"
```

```powershell
$env:AUTOROAD_SITE = "config/sites/testarena.yaml"
```

```powershell
$env:AUTOROAD_SITE = "config/sites/reschen.yaml"
```

```powershell
$env:AUTOROAD_SITE = "config/sites/imst.yaml"
```

Same Core steps without the window:

```powershell
cd C:\temp\beamng_autoroad; python tools\build_level.py --site config/sites/fernpass_mega.yaml
```

That runs DGM/DOM/BEV → TWI/snow → masks → compose biomes → `dry_meadow`/`ForestFloor` materials → forest scatter.
Terrain materials are created by `tools/ensure_terrain_materials.py` (Grass+dirt base / 50·50 mudgrass mix) — no World Editor required.

Or step-by-step:

```powershell
cd C:\temp\beamng_autoroad; python tools\fetch_dgm.py
```

```powershell
cd C:\temp\beamng_autoroad; python tools\build_smoke.py
```

Processed output is split under `data/processed/<site.name>/`.

Bridges, tunnels, galleries, ramps beside a deck: [docs/STRUCTURES.md](../docs/STRUCTURES.md).
