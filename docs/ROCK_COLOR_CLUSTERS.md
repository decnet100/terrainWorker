# Rock color clusters (Orthophoto → representative RGB classes)

Ziel: Aus einem Orthofoto im Spielbereich die **Felsfarben** (inkl. Helligkeit) in wenige RGB-Klassen clustern, damit man später Felsmaterialien farblich in diese Richtung „trimmen“ kann (zweiter Schritt).

Die Einschränkung „nur Fels“ erfolgt über eine Maske (typisch: `data/processed/<site>/mask_rock.png` aus `tools/build_terrain_masks.py`).

## Script

`tools/cluster_rock_colors.py`

- **Input**: Ortho RGB (`--ortho`) + Rock-Maske (`--mask`)
- **Clustering**: in CIE Lab (perzeptuell), Start \(k=3\), iterativ bis die Streuung klein genug ist
- **Stop-Kriterium**: Für jede Klasse muss \(ΔE_{76}\) im \(p\)-Perzentil <= Schwellwert sein
- **Output**: JSON + Debug-PNGs

## Outputs

Schreibt nach `data/processed/<site>/rock_color_clusters/` (oder `--out`):

- `rock_colors_kK.json`: Cluster-Zentren + Streuungsmetriken (Lab und RGB)
- `rock_colors_kK.json` enthält zusätzlich eine robuste „Normalfarbe“:
  - `rock_stats.normal_color.global_median_rgb`: globaler Median über alle Fels-Pixel
  - `rock_stats.normal_color.trimmed_mean_rgb`: Mittelwert nur im mittleren Helligkeitsbereich (L-P20…L-P80)
  - `rock_stats.normal_color.rep_cluster_rgb`: repräsentativer Cluster (Center-L nahe L-Median)
- `rock_palette_kK.png`: Palette (Swatches)
- `rock_palette_kK_labeled.png`: Palette mit Häufigkeiten (`id`, `%`, `n_full`)
- `rock_quantized_kK.png`: Quantisierte Vorschau (nur Fels-Pixel ersetzt)
- `rock_class_XX_kK.png`: Klassenmasken (weiß = Pixel dieser Klasse)

## Beispiele (PowerShell)

### Mit Site-Defaults (wenn vorhanden)

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/fernpass_mega.yaml"; python tools\cluster_rock_colors.py
```

### Explizite Pfade

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/fernpass_mega.yaml"; python tools\cluster_rock_colors.py --ortho "data\processed\fernpass-mega\backdrop_near_ortho.png" --mask "data\processed\fernpass-mega\mask_rock.png"
```

### Strenger / lockerer clustern

- mehr Klassen (früher splitten): kleinerer `--threshold`
- weniger Klassen (mehr Toleranz): größerer `--threshold`

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/fernpass_mega.yaml"; python tools\cluster_rock_colors.py --threshold 8.0 --k-max 16
```

