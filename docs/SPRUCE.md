# Fichte

Eigene Fichte aus `C:\temp\assets\trees\fichte2.blend` (Objekt `tree`) für den Wald. Das Nahmesh bleibt das Mesh. Dahinter keine weiteren Nadel-LODs, sondern Karten und ein kleiner Kamera-Billboard.

Stand der Kette: Pack `data/assets/trees/spruce/`. Ins Level kopiert `vendor_spruce_pack` in `tools/build_forest.py` (`shape_pack: spruce`). Nach Material, DAE oder DDS BeamNG ganz beenden. Den World Editor nicht speichern, wenn der Inject die Quelle ist.

## Stufen

| Node | Pixel | Inhalt |
|------|-------|--------|
| `tree_spruce_large_a2500` | ab 2500 | Nahmesh, Rinde + Nadeln |
| `tree_spruce_large_a80` | 80 bis 2500 | acht einseitige Ansichtskarten, je 45° |
| `bb_autobillboard25` | 25 bis 80 | eine Fläche, die zur Kamera zeigt |
| — | unter 25 | nichts |

Die Pixelgröße ist die Torque-Detailgröße. Die größte Stufe, die noch unter die projizierte Größe passt, wird gezeichnet. Das Mesh ist damit nur nah sichtbar.

`tree_spruce_large.dae.asset.json`: `equatorSteps` 16, `polarSteps` 0, `includePoles` false, `dimension` 256, `detailLevel` 0, `size` 25. `detailLevel` 0 ist das einzige Mesh, falls die Engine neu backt. `size` muss kleiner sein als jede andere Stufe. Deshalb gibt es keine Kartenstufe `_a25`.

Die Imposter-DDS muss neuer sein als das DAE. Sonst verwirft die Engine die Datei und fotografiert das Mesh neu (`Rebuilding imposter`).

## Erstellung

1. Aufnahmen, Blender 5.2, Skript `tools/blender/tree_imposter_capture.py` im Blend `fichte2.blend`. Schreibt nach `C:\temp\assets\trees\out\spruce\cells\`:
   - `albedo_00`…`15` — unbeleuchtetes Albedo, 16 Seitenblicke, keine Pole
   - `normal_00`…`15` — Weltraumnormalen
   - `ao_00`…`15` — Verdeckung, nur mit `--ao`. Reichweite 0,45 m, nur das Objekt selbst
2. Nahmesh, `tools/blender/tree_lod_spruce.py`. Exportiert nur LOD0 als `_a2500` und ruft danach `install_spruce_far_lod` auf.
3. `install_spruce_far_lod` in `tools/tree_imposter_lib.py` hängt die acht Karten und den Billboard an, packt die DDS und setzt die Materialien.

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" "C:\temp\assets\trees\fichte2.blend" --background --python "C:\temp\beamng_autoroad\tools\blender\tree_imposter_capture.py"
```

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" "C:\temp\assets\trees\fichte2.blend" --background --python "C:\temp\beamng_autoroad\tools\blender\tree_imposter_capture.py" -- --ao
```

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" "C:\temp\assets\trees\fichte2.blend" --background --python "C:\temp\beamng_autoroad\tools\blender\tree_lod_spruce.py"
```

## Acht Karten

Eine Aufnahme pro Blick, Atlas `spruce_mid.png`, Deckkraft `spruce_mid_o.png` (RGB, jeder Kanal = Alpha). BeamNG liest beim Alpha-Test die Farbkanäle der Opacity-Map, nicht das PNG-Alpha. `alphaRef` 65.

Die Kartenfarbe ist das rohe Albedo, ohne Verdeckung. Die Verdeckung in diesen Karten hat das Kroneninnere fest abgedunkelt, genau dort, wo die acht Ebenen zusammenlaufen. Das soll die Normal Map machen, nicht die Farbe.

`SpruceMid`: eine Normal Map `spruce_mid_n.png`, Stärke 1, `doubleSided` true, `invertBackFaceNormals` true, `castShadows` false, Specular 0, Rauheit 1, kein Subsurface. Die Karte ist DirectX-Tangentraum der jeweiligen Ansicht. Nadeln, die von der Kamera wegzeigen, bleiben wegzeigend. Sie werden nicht zur Kamera geklappt. Das hat bei seitlicher Sonne die ganze Krone ausgebleicht.

Rückseite: `shaders/common/material/shadergen/defaultMat.hlsl`, nur in der doppelseitigen Variante (`MFT_DoubleSided`). Auf der Rückseite wird die Z-Komponente der Normal Map umgedreht, X und Y bleiben. Eine zweite Normal Map gibt es nicht. Der Materialeditor nennt das „Double Sided“ und „Invert backface normals“. Die Douglasien-Nadeln stehen genauso. Ohne `doubleSided` läuft diese Zeile nicht, die Rückseite behält die Normale der Vorderseite.

## Billboard

Eine kamera-zugewandte Fläche, keine Rückseite. Der Shader `imposter.hlsl` wählt eine der 16 Atlaszellen aus dem Azimut und schaltet einen halben Schritt vor dem Aufnahmewinkel. Die Flächennormale zeigt immer zur Kamera.

Farbe `tree_spruce_large.dae.imposter.dds` (DXT3), Normalen `…imposter_normals.dds` (DXT5, X in Alpha, Y in Grün). 1024×1024, 10 Mip-Stufen, Zellenränder werden nicht mitgemischt. Aufnahme 0 liegt auf Atlasplatz 8, dann 9…15, 0…7. Die Normalen liegen im Tangentenraum dieser Kamerafläche, nicht im Weltraum.

Die Billboard-Farbe wird mit der Verdeckung multipliziert, wenn `ao_XX.png` neben dem Albedo liegt. Die Maske einer Ansicht wird so gestreckt, dass die hellen Spitzen beim Albedo bleiben und das Innere bis auf 0,45 fällt. Eine rohe Multiplikation macht die Krone fast schwarz, weil die Nadelkarten im Tracer zu viel verdecken. Keine Sonnenrichtung in der Farbe.

Der Billboard über den ganzen Mittelgrund (alles unter 2500 px) hat geflackert und bei flacher Sonne lange Schatten geworfen. Dieselbe Fläche erst unter 80 px ist klein genug. Unsere Materialien werfen keinen Schatten (`castShadows` false). Empfangen lässt sich bei einer ausgestanzten Fläche nicht abschalten.

## Nahmesh

Nadeltextur ohne `baseColorFactor`. Der Faktor 0,55 / 1 / 0,4 hat Rot und Blau gedrückt und ein einfarbiges Grün erzeugt. `alphaRef` 75, doppelseitig, `invertBackFaceNormals`. Keine Nadel-Normal-Map, kein Specular, kein Subsurface: das hat die Füllrate gekostet und die Farbe nicht gerettet.

## Was nicht wiederholen

- Eigenen 16-Blick-Atlas als einzige Fernstufe. Mit den Stock-DDS auf demselben Mesh war das Flackern fast weg, mit den eigenen Aufnahmen nicht.
- Globales Abdunkeln der Albedo-Farbe.
- Normal Map auf weltfesten Karten, deren Rückseiten-Nadeln zur Kamera geklappt sind.
- Billboard-DDS älter als das DAE.
