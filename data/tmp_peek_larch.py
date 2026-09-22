from pathlib import Path
import re

p = Path("data/assets/larch_Export")
print("files:")
for f in sorted(p.rglob("*")):
    if f.is_file():
        print(f"  {f.relative_to(p)}  {f.stat().st_size}")

dae = (p / "larch.dae").read_text(encoding="utf-8", errors="replace")
print("dae_bytes", len(dae))
m = re.search(r"<up_axis>([^<]+)</up_axis>", dae)
print("up_axis", m.group(1) if m else None)
mats = re.findall(r'<material[^>]*id="([^"]+)"', dae)
print("materials", mats[:20])
imgs = re.findall(r"<init_from>([^<]+)</init_from>", dae)
print("images", imgs[:20])
m = re.search(r'id="[^"]*positions-array"[^>]*>([^<]+)', dae)
if not m:
    m = re.search(r"positions-array[^>]*>([^<]+)", dae)
if m:
    vals = [float(x) for x in m.group(1).split()]
    zs = vals[2::3]
    print(
        "Z",
        round(min(zs), 3),
        round(max(zs), 3),
        "height",
        round(max(zs) - min(zs), 3),
        "nverts",
        len(zs),
    )
else:
    print("no positions array")
tris = [int(x) for x in re.findall(r'<triangles[^>]*count="(\d+)"', dae)]
print("tri counts", tris, "sum", sum(tris))
