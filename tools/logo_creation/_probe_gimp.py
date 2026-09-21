import os

import gi

gi.require_version("Gimp", "3.0")
from gi.repository import Gimp

out = r"C:\temp\beamng_autoroad\tools\logo_creation\_probe_out.txt"
names = []
for font in Gimp.fonts_get_list(None):
    name = font.get_name()
    low = name.lower()
    if "sans" in low or "arial" in low or low.startswith("dejavu"):
        names.append(name)
trials = ["Sans", "Sans Bold", "Sans-serif", "Sans-serif Bold", "Arial", "Arial Bold"]
lines = ["count=" + str(len(names))]
lines.extend(sorted(names)[:80])
lines.append("--- trials ---")
for trial in trials:
    found = Gimp.Font.get_by_name(trial)
    lines.append(trial + "=" + ("None" if found is None else found.get_name()))
with open(out, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines) + "\n")
os._exit(0)
