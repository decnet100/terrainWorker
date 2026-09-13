# Site-Profile

| Datei | Gebiet | Level | Größe |
|-------|--------|-------|--------|
| [`hahntennjoch.yaml`](hahntennjoch.yaml) | Hahntennjoch (Leitplanken/Gelände) | `autoroad_m28_test` | ~500×510 → 512² |
| [`l13_kuehtai.yaml`](l13_kuehtai.yaml) | L13 Sellraintal / Kühtai (Galerien) | `autoroad_galerie_2048` | 2048² |
| [`l13_splining.yaml`](l13_splining.yaml) | L13 Splining-Test (Strassennetz + 4 MeshRoads) | `autoroad_l13_splining` | 2048² |
| [`fernpass.yaml`](fernpass.yaml) | B179 Fernpass | `autoroad_fernpass_4096` | 4096² |

## Aktiv wählen

**Default:** `config/site.yaml` (lokal, gitignore) = Kopie von Hahntennjoch.

```powershell
# Hahntennjoch (Default)
Copy-Item config\sites\hahntennjoch.yaml config\site.yaml -Force
# oder Env zurücksetzen:
Remove-Item Env:AUTOROAD_SITE -ErrorAction SilentlyContinue

# L13 Kühtai
$env:AUTOROAD_SITE = "config/sites/l13_kuehtai.yaml"

# Fernpass 4096
$env:AUTOROAD_SITE = "config/sites/fernpass.yaml"
python tools\fetch_dgm.py
python tools\build_smoke.py
```

Processed-Ausgaben liegen getrennt unter `data/processed/<site.name>/`.
