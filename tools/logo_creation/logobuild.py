import gi
gi.require_version('Gimp', '3.0')
gi.require_version('GimpUi', '3.0')
gi.require_version('Gegl', '0.4')
from gi.repository import Gimp, GimpUi, Gegl, GObject

# 1. Bild erstellen (800x450 px, 16:9 Format)
width, height = 800, 450
image = Gimp.Image.new(width, height, Gimp.ImageBaseType.RGB)

# 2. Ebenen anlegen

# --- Ebene: Hintergrund (Screenshot) ---
bg_layer = Gimp.Layer.new(
    image, "Hintergrund Screenshot", width, height,
    Gimp.ImageType.RGB_IMAGE, 100.0, Gimp.LayerMode.NORMAL
)
image.insert_layer(bg_layer, None, 0)

# --- Ebene: Dunkler Verlauf ---
gradient_layer = Gimp.Layer.new(
    image, "Dunkler Verlauf unten", width, height,
    Gimp.ImageType.RGBA_IMAGE, 85.0, Gimp.LayerMode.NORMAL
)
image.insert_layer(gradient_layer, None, 0)

# --- Ebene: Streckenname (Text) ---
title_layer = Gimp.text_fontname(
    image, None, 35, height - 130, "FERNPASS", 0, True, 72.0, Gimp.Unit.pixel(), "Sans Bold"
)
if title_layer:
    title_layer.set_name("Streckenname")

# --- Ebene: Untertitel (Text) ---
sub_layer = Gimp.text_fontname(
    image, None, 35, height - 45, "TIROL • AUSTRIA • 1.210 M", 0, True, 24.0, Gimp.Unit.pixel(), "Sans"
)
if sub_layer:
    sub_layer.set_name("Untertitel")




# --- Ebene: Wappen Platzhalter ---
wappen_layer = Gimp.Layer.new(
    image, "Wappen (Hier PNG einfügen)", 160, 200,
    Gimp.ImageType.RGBA_IMAGE, 100.0, Gimp.LayerMode.NORMAL
)
image.insert_layer(wappen_layer, None, 0)
wappen_layer.set_offsets(25, 20)

# 3. Anzeige in GIMP 3 aktualisieren
display = Gimp.Display.new(image)