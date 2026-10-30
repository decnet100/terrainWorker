"""swissTLM3D roads inside a bbox given in EPSG:25832, from the national shapefile.

The package is read with HTTP range requests. Only TLM_STRASSE records whose box
meets the bbox are kept. Output is a GeoPackage in EPSG:25832, one line per part,
with the deck height on bridge vertices and KUNSTBAUTE set.

    python tools\\fetch_swisstlm3d_roads.py data\\raw\\swiss\\tlm_strasse.gpkg xmin ymin xmax ymax
"""
from __future__ import annotations

import io
import struct
import sys
import zipfile
import urllib.request

import geopandas as gpd
import numpy as np
from pyproj import Transformer
from shapely.geometry import LineString

URL = ("https://data.geo.admin.ch/ch.swisstopo.swisstlm3d/swisstlm3d_2026-02/"
       "swisstlm3d_2026-02_2056_5728.shp.zip")
STEM = "TLM_STRASSEN/swissTLM3D_TLM_STRASSE"
KEEP = ("UUID", "OBJEKTART", "KUNSTBAUTE", "BELAGSART", "STUFE", "RICHTUNGSG",
        "KREISEL", "NAME", "STRNAME", "BEFAHRBARK")


class RangeFile(io.RawIOBase):
    def __init__(self, url: str):
        self.url, self.pos = url, 0
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=60) as response:
            self.size = int(response.headers["Content-Length"])

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        if whence == 0:
            self.pos = off
        elif whence == 1:
            self.pos += off
        else:
            self.pos = self.size + off
        return self.pos

    def readinto(self, buf):
        if self.pos >= self.size:
            return 0
        end = min(self.pos + len(buf), self.size) - 1
        req = urllib.request.Request(self.url, headers={"Range": f"bytes={self.pos}-{end}"})
        with urllib.request.urlopen(req, timeout=180) as response:
            data = response.read()
        buf[:len(data)] = data
        self.pos += len(data)
        return len(data)


def read_exact(handle, count: int) -> bytes:
    out = bytearray()
    while len(out) < count:
        chunk = handle.read(count - len(out))
        if not chunk:
            raise EOFError
        out += chunk
    return bytes(out)


def shp_records(handle, bbox):
    head = read_exact(handle, 100)
    total = struct.unpack(">i", head[24:28])[0] * 2
    pos, index = 100, 0
    e0, n0, e1, n1 = bbox
    while pos < total:
        record = read_exact(handle, 8)
        length = struct.unpack(">i", record[4:8])[0] * 2
        content = read_exact(handle, length)
        pos += 8 + length
        shape = struct.unpack("<i", content[:4])[0]
        if shape in (3, 13):
            xmin, ymin, xmax, ymax = struct.unpack("<4d", content[4:36])
            if xmax >= e0 and xmin <= e1 and ymax >= n0 and ymin <= n1:
                npart, npt = struct.unpack("<2i", content[36:44])
                parts = struct.unpack(f"<{npart}i", content[44:44 + 4 * npart])
                offset = 44 + 4 * npart
                xy = np.frombuffer(content, "<f8", 2 * npt, offset).reshape(npt, 2).copy()
                offset += 16 * npt
                if shape == 13 and len(content) >= offset + 16 + 8 * npt:
                    zz = np.frombuffer(content, "<f8", npt, offset + 16).copy()
                else:
                    zz = np.full(npt, np.nan)
                bounds = list(parts) + [npt]
                yield index, [np.column_stack([xy[a:b], zz[a:b]]) for a, b in zip(bounds, bounds[1:])]
        index += 1
        if index % 200000 == 0:
            print(f"shp records {index}", flush=True)


def dbf_records(handle, keep: set[int]):
    header = read_exact(handle, 32)
    nrec, hlen, rlen = struct.unpack("<IHH", header[4:12])
    fields = []
    rest = read_exact(handle, hlen - 32)
    for k in range(0, len(rest) - 1, 32):
        item = rest[k:k + 32]
        if item[0] == 0x0D:
            break
        fields.append((item[:11].split(b"\0")[0].decode("latin-1"), chr(item[11]), item[16]))
    wanted = {name for name in KEEP}
    out = {}
    for i in range(nrec):
        row = read_exact(handle, rlen)
        if i not in keep:
            continue
        offset, rec = 1, {}
        for name, typ, length in fields:
            raw = row[offset:offset + length]
            offset += length
            if name not in wanted:
                continue
            rec[name] = raw.decode("utf-8", "replace").strip()
        out[i] = rec
        if i and i % 200000 == 0:
            print(f"dbf records {i}", flush=True)
    return out


def main() -> None:
    dest, *bb = sys.argv[1:]
    xmin, ymin, xmax, ymax = map(float, bb)
    to_lv = Transformer.from_crs("EPSG:25832", "EPSG:2056", always_xy=True)
    xs, ys = to_lv.transform([xmin, xmax, xmin, xmax], [ymin, ymin, ymax, ymax])
    bbox = (min(xs), min(ys), max(xs), max(ys))
    print("LV95 bbox", [round(v) for v in bbox], flush=True)
    archive = zipfile.ZipFile(io.BufferedReader(RangeFile(URL), buffer_size=1 << 22))
    names = set(archive.namelist())
    if STEM + ".shp" not in names:
        raise SystemExit("TLM_STRASSE is not in the package")
    with archive.open(STEM + ".shp") as handle:
        geoms = dict(shp_records(io.BufferedReader(handle, 1 << 22), bbox))
    print(f"roads in the box {len(geoms)}", flush=True)
    with archive.open(STEM + ".dbf") as handle:
        props = dbf_records(io.BufferedReader(handle, 1 << 22), set(geoms))
    to_work = Transformer.from_crs("EPSG:2056", "EPSG:25832", always_xy=True)
    rows = []
    for index, parts in geoms.items():
        attr = props.get(index, {})
        for part in parts:
            if len(part) < 2:
                continue
            xy = np.column_stack(to_work.transform(part[:, 0], part[:, 1]))
            line = LineString(np.column_stack([xy, part[:, 2]]))
            rows.append({**attr, "geometry": line})
    frame = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:25832")
    frame.to_file(dest, layer="tlm_strasse")
    print(f"wrote {len(frame)} lines to {dest}", flush=True)


if __name__ == "__main__":
    main()
