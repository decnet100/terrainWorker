r"""Match road ends across an administrative validity edge.

Writes ``data/processed/<site>/border_links.json`` for unambiguous pairs and
``border_links_review.json`` when three or more ends share one group.

```powershell
cd C:\temp\beamng_autoroad; python tools\match_border_roads.py
```
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from authorities import write_border_links  # noqa: E402
from site_coords import load_site, processed_dir  # noqa: E402


def main() -> None:
    site = load_site()
    if not site.get("authorities"):
        raise SystemExit("authorities fehlt in der Site-Steuerung")
    write_border_links(site, processed_dir(site))


if __name__ == "__main__":
    main()
